using System.Collections.Concurrent;
using System.Globalization;
using System.Text.Json;
using PredictionBacktester.Engine.LiveExecution;

namespace KalshiEvBot;

/// <summary>Tunables. Every one reads the environment: these are calibration decisions, not constants, and
/// the live values come out of M1's settlement report rather than out of this file.</summary>
public sealed class EvConfig
{
    /// <summary>Minimum EV per contract to call something a signal. 3.5c was the MAKER threshold and admits
    /// 0.9% of windows as a taker — the bot would essentially never fire. 1c admits ~7%; observe there and
    /// set the live value from settlement results.</summary>
    public double EvMin           = Env("EV_MIN", 0.01);
    /// <summary>How far below EvMin a WS-implied EV may sit and still buy a REST call. The WS ask is
    /// optimistic 95% of the time, which makes WS EV an upper bound and pre-screening safe; this slack
    /// covers the other 5%.</summary>
    public double PrescreenSlack  = Env("EV_PRESCREEN_SLACK", 0.02);
    public double MinPrice        = Env("EV_MIN_PRICE", 0.20);
    public double MaxPrice        = Env("EV_MAX_PRICE", 0.80);
    public double MaxTradeFrac    = Env("EV_MAX_TRADE_FRACTION", 0.03);
    public int    CooldownMs      = (int)Env("EV_RECHECK_COOLDOWN_MS", 15_000);
    public int    RestConcurrency = (int)Env("EV_REST_CONCURRENCY", 2);
    public double BankrollFallback= Env("EV_BANKROLL_USD", 0);
    /// <summary>"proportional" (the spec's primary) or "shin". Both are always computed and logged; this
    /// only selects which one drives the decision.</summary>
    public string DeVigMethod     = (Environment.GetEnvironmentVariable("EV_DEVIG") ?? "proportional").ToLowerInvariant();
    /// <summary>Require every Pinnacle leg to be under LIVE WS coverage before a row counts as a signal.
    /// A screening-only quote carries a FRESH timestamp but a DELAYED price, so the age gate cannot see it.
    /// Set 0 to log such rows as signals anyway (observation only — they are still logged either way).</summary>
    public bool RequireWsVerified = Env("EV_REQUIRE_WS_VERIFIED", 1) != 0;
    /// <summary>Largest believable gap between our P_true and Kalshi's own price. Beyond this the row is
    /// logged as IMPLAUSIBLE rather than as a signal: every such case so far has been a pairing fault, and
    /// the measured taker edge distribution puts almost nothing past 3.5c, let alone 20c.</summary>
    public double MaxDisagree     = Env("EV_MAX_DISAGREE", 0.15);
    /// <summary>Only count a row as a signal while the match is actually being PLAYED.
    ///
    /// <para>Measured 2026-08-21/22: all 6 genuine signals were in-play; all 44 phantoms and the one bad
    /// SIGNAL were pre-match. That is not coincidence — a fixture days out carries a wide, unformed
    /// Pinnacle line (vig ~9% against ~4% at kickoff), a thin Kalshi book, and no pressure on either side
    /// to be right yet, so a disagreement means far less. It is also where a mispair survives longest,
    /// because nothing is moving to contradict it.</para>
    ///
    /// <para>It also protects the strategy's premise: the case for this bot is fast capital rollover, and
    /// a bet on Sunday's match ties up the stake until Sunday.</para>
    ///
    /// <para>Pre-match rows are still WRITTEN and still snapshotted — that is where the calibration volume
    /// is (336 fixtures against a handful live), and whether Pinnacle is predictive pre-match is a
    /// question worth answering rather than assuming. They just do not count as things to trade.</para></summary>
    public bool RequireInPlay     = Env("EV_REQUIRE_IN_PLAY", 1) != 0;

    public static double Env(string k, double dflt)
        => double.TryParse(Environment.GetEnvironmentVariable(k), NumberStyles.Any,
                           CultureInfo.InvariantCulture, out var v) ? v : dflt;
}

/// <summary>Running counts for the status line. Nothing here is a decision; it is how the operator tells
/// "found nothing" apart from "never looked".</summary>
public sealed class EvStats
{
    public long Screened, NoQuote, StaleOracle, Suspended, BelowPrescreen, Cooldown,
                RestCalls, RestFailed, Signals, RejectedByRest, FlooredToZero, RateLimited,
                IncompleteBook, ScreeningOnly, Implausible, PreMatch;
}

/// <summary>
/// The decision path: WS says look, Pinnacle says what it is worth, REST says what it costs.
///
/// <para>M0 places no orders. The one thing this class must get right is that the EV it logs is computed
/// from the REST ask — a CSV of WS-priced EV measures a price nobody can get, and M1 would inherit the lie
/// and grade a strategy that was never available.</para>
/// </summary>
public sealed class EvEvaluator
{
    private readonly ConcurrentDictionary<string, EvPair> _byTicker;
    private readonly PinnacleOracle _oracle;
    private readonly KalshiBookFeed _feed;
    private readonly KalshiOrderClient _kalshi;
    private readonly EvTelemetry _telemetry;
    private readonly EvConfig _cfg;
    private readonly SemaphoreSlim _restGate;
    private readonly ConcurrentDictionary<string, long> _cooldownUntil = new(StringComparer.Ordinal);
    private readonly ConcurrentQueue<string> _queue = new();
    private readonly ConcurrentDictionary<string, byte> _queued = new(StringComparer.Ordinal);
    private readonly SemaphoreSlim _work = new(0);

    public readonly EvStats Stats = new();
    /// <summary>Tickers waiting to be screened. Lets --once know when the sweep has drained.</summary>
    public int Pending => _queued.Count;
    public double BankrollUsd { get; set; }
    /// <summary>Fraction of bankroll already at risk. Always 0 in M0 — nothing is ever bought — but the
    /// damping term is computed from it so M2 inherits a sizer that has been exercised, not a new one.</summary>
    public double ActiveExposureFraction { get; set; }
    public bool Verbose;

    public EvEvaluator(IEnumerable<EvPair> pairs, PinnacleOracle oracle, KalshiBookFeed feed,
                       KalshiOrderClient kalshi, EvTelemetry telemetry, EvConfig cfg)
    {
        _byTicker  = new ConcurrentDictionary<string, EvPair>(
            pairs.GroupBy(p => p.KalshiTicker).ToDictionary(g => g.Key, g => g.First()), StringComparer.Ordinal);
        _oracle    = oracle;
        _feed      = feed;
        _kalshi    = kalshi;
        _telemetry = telemetry;
        _cfg       = cfg;
        _restGate  = new SemaphoreSlim(Math.Max(1, cfg.RestConcurrency));
    }

    // ── Triggers ──────────────────────────────────────────────────────────────────────────────────────
    /// <summary>A Kalshi book moved. Coalesced: a ticker already waiting is not queued twice, so a market
    /// ticking fifty times a second still costs one evaluation.</summary>
    public void Nudge(string ticker)
    {
        if (!_byTicker.ContainsKey(ticker)) return;
        if (!_queued.TryAdd(ticker, 0)) return;
        _queue.Enqueue(ticker);
        _work.Release();
    }

    /// <summary>Pinnacle moved. Re-screens everything — a value bet can open with the Kalshi book perfectly
    /// still, and a bot that only woke on Kalshi ticks would never see those. The screen itself is free;
    /// only the ones that survive it cost a REST call.</summary>
    public void SweepAll() { foreach (var t in _byTicker.Keys) Nudge(t); }

    /// <summary>Adds or replaces pairs after a reload of cross_pairs.json. Returns how many are new.
    /// Existing entries are overwritten because the pairing job re-points a market at a new Pinnacle
    /// matchup id when a fixture is re-issued, and the stale id would price against a dead selection.</summary>
    public int UpsertPairs(IEnumerable<EvPair> pairs)
    {
        int added = 0;
        foreach (var p in pairs)
        {
            if (!_byTicker.ContainsKey(p.KalshiTicker)) added++;
            _byTicker[p.KalshiTicker] = p;
        }
        return added;
    }

    /// <summary>REPLACES the watched set with the current pair file. Returns (added, removed).
    /// The sweep runs over every entry on every oracle poll, so a set that only grows turns a fortnight's
    /// daily re-pairs into thousands of finished matches re-screened every three seconds. Settlements for
    /// dropped markets are still banked — the settlement watcher keeps its own archive.</summary>
    public (int Added, int Removed) ReplacePairs(IEnumerable<EvPair> pairs)
    {
        var fresh = pairs.GroupBy(p => p.KalshiTicker)
                         .ToDictionary(g => g.Key, g => g.First(), StringComparer.Ordinal);
        int added = fresh.Keys.Count(k => !_byTicker.ContainsKey(k));
        int removed = 0;
        foreach (var t in _byTicker.Keys.Where(k => !fresh.ContainsKey(k)).ToList())
        {
            if (_byTicker.TryRemove(t, out _)) removed++;
            _cooldownUntil.TryRemove(t, out _);
        }
        foreach (var kv in fresh) _byTicker[kv.Key] = kv.Value;
        return (added, removed);
    }

    public int PairCount => _byTicker.Count;

    /// <summary>Drops every per-ticker cooldown. Used by --verify: the markets it wants to exercise were
    /// just evaluated by the live loop, so without this the sweep makes zero REST calls and the check
    /// reports "nothing near the threshold" — a confident, wrong explanation of its own throttling.</summary>
    public void ClearCooldowns() => _cooldownUntil.Clear();

    /// <summary>Runs <c>EV_REST_CONCURRENCY</c> worker loops, not one. A single loop awaits its REST call
    /// inside the loop body, so the concurrency semaphore could never reach 2 and one slow market stalled
    /// the screening of every other.</summary>
    public Task RunAsync(CancellationToken ct)
        => Task.WhenAll(Enumerable.Range(0, Math.Max(1, _cfg.RestConcurrency)).Select(_ => WorkerAsync(ct)));

    private async Task WorkerAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try { await _work.WaitAsync(ct); } catch (OperationCanceledException) { break; }
            if (!_queue.TryDequeue(out var ticker)) continue;
            _queued.TryRemove(ticker, out _);
            try { await EvaluateAsync(ticker, ct); }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception ex) { Console.WriteLine($"[EVAL] {ticker}: {ex.GetType().Name}: {ex.Message}"); }
        }
    }

    // ── One ticker, both sides ────────────────────────────────────────────────────────────────────────
    private sealed record Screened(string Side, double PTrueProp, double PTrueShin, double PTrueUsed,
                                   double Vig, double ShinZ, double PinMine, double PinOther, double PinSum,
                                   double OracleAgeMs, double OracleDepth, bool InPlay,
                                   decimal WsAsk, decimal WsDepth, double WsBookAge, double EvWs,
                                   int NumLegs, string PinOddsAll, bool WsVerified);

    private async Task EvaluateAsync(string ticker, CancellationToken ct)
    {
        if (!_byTicker.TryGetValue(ticker, out var pair)) return;

        var candidates = new List<Screened>(2);
        foreach (string side in new[] { "YES", "NO" })
        {
            var s = Screen(pair, side);
            if (s is not null) candidates.Add(s);
        }
        if (candidates.Count == 0) return;

        // Cooldown is per TICKER, applied after the screen: one REST read prices both sides, so the cost
        // this is rationing is per market, not per side.
        long now = Environment.TickCount64;
        if (_cooldownUntil.TryGetValue(ticker, out long until) && now < until)
        {
            Interlocked.Increment(ref Stats.Cooldown);
            return;
        }
        _cooldownUntil[ticker] = now + _cfg.CooldownMs;

        decimal restYes, restNo;
        await _restGate.WaitAsync(ct);
        try
        {
            Interlocked.Increment(ref Stats.RestCalls);
            using var doc = await _kalshi.GetMarketAsync(ticker);
            var mkt = doc.RootElement.TryGetProperty("market", out var m) ? m : doc.RootElement;
            restYes = AskDollars(mkt, yes: true);
            restNo  = AskDollars(mkt, yes: false);
        }
        catch (Exception ex)
        {
            Interlocked.Increment(ref Stats.RestFailed);
            if (Verbose) Console.WriteLine($"[REST] {ticker}: {ex.GetType().Name}: {ex.Message}");
            return;
        }
        finally { _restGate.Release(); }

        foreach (var c in candidates)
        {
            decimal restAsk = c.Side == "YES" ? restYes : restNo;
            if (restAsk <= 0m || restAsk >= 1m) continue;      // no REST price = nothing to value
            Record(pair, c, restAsk);
        }
    }

    /// <summary>The free half: Pinnacle fair value against the WS book. Returns null (and counts why) for
    /// anything not worth a REST call.</summary>
    private Screened? Screen(EvPair pair, string side)
    {
        Interlocked.Increment(ref Stats.Screened);
        bool yes = side == "YES";
        if (!pair.LegsUsable) return null;

        // EVERY leg of the matchup must be quotable, open and fresh — not just the two named on this row.
        // On a 1X2 a missing draw price invalidates the home and away legs as well: S is wrong without it,
        // so there is nothing correct to normalise by. Deliberately NOT reading NoToken, which is the true
        // complement on a two-way and merely another leg on a three-way.
        var quotes = new OracleQuote[pair.Legs.Count];
        for (int i = 0; i < pair.Legs.Count; i++)
        {
            var q = _oracle.Get(pair.Legs[i]);
            if (q is null)          { Interlocked.Increment(ref Stats.NoQuote);     return null; }
            if (!q.Open)            { Interlocked.Increment(ref Stats.Suspended);   return null; }
            if (!_oracle.Fresh(q))  { Interlocked.Increment(ref Stats.StaleOracle); return null; }  // a value bet against a stale oracle is a bet against nothing
            quotes[i] = q;
        }

        var odds = quotes.Select(q => q.DecimalOdds).ToArray();
        var prop = DeVig.ProportionalN(odds);
        var shin = DeVig.ShinN(odds);
        if (!prop.Ok || !shin.Ok) return null;

        // ── THE INCOMPLETE-BOOK GUARD ─────────────────────────────────────────────────────────────────
        // A bookmaker never offers a negative margin, so S < 1 does not mean a generous price — it means we
        // are looking at only PART of the outcome set and dividing by a sum that is missing a leg.
        //
        // This is not hypothetical. The sidecar's live-board catalog path hardcodes `three_way=False` and
        // builds its legs from participants, and a soccer matchup exposes only home and away as
        // participants — the draw is a PRICE, not a participant. So an in-play soccer game arrives here
        // looking exactly like a tennis two-way. De-vigging 2.30/3.10 as if it were the whole book gives
        // S = 0.758 and P(home) = 0.574 against a true 0.40: a phantom edge on every leg, in the direction
        // that makes us bet.
        //
        // Deliberately keyed on the arithmetic rather than on the sport or the three_way flag, because the
        // flag is exactly what was wrong. Any future venue or market that loses a leg is caught the same way.
        if (prop.Overround < -0.005)
        {
            Interlocked.Increment(ref Stats.IncompleteBook);
            if (Verbose)
                Console.WriteLine($"      {pair.KalshiTicker} {side}: book sums to {prop.Overround + 1.0:0.000} "
                                + $"(<1) across {odds.Length} leg(s) — INCOMPLETE outcome set, not a free "
                                + "margin. Skipped. A soccer 1X2 catalogued as a two-way looks exactly like this.");
            return null;
        }

        int yi = pair.YesLegIndex;
        var mine = quotes[yi];

        // The Kalshi YES side pays on ITS leg; the NO side pays on everything else. Taking the complement as
        // 1 - P(yes) is correct for ANY number of legs — on a 1X2 it is exactly P(other team) + P(draw),
        // without needing to know which leg is which.
        double propYes = prop.PTrue[yi], shinYes = shin.PTrue[yi];
        if (propYes <= 0 || propYes >= 1) return null;
        double pProp = yes ? propYes : 1.0 - propYes;
        double pShin = yes ? shinYes : 1.0 - shinYes;
        double pTrue = _cfg.DeVigMethod == "shin" ? pShin : pProp;

        var top = _feed.Top(pair.KalshiTicker);
        if (!top.HasSnapshot) return null;
        decimal wsAsk = yes ? top.YesAsk : top.NoAsk;
        if (wsAsk <= 0m || wsAsk >= 1m) return null;

        // WS EV is an UPPER BOUND (the ask reads low), so a candidate that fails here cannot pass at REST.
        double evWs = EvMath.Ev(pTrue, (double)wsAsk);
        if (evWs < _cfg.EvMin - _cfg.PrescreenSlack)
        { Interlocked.Increment(ref Stats.BelowPrescreen); return null; }

        return new Screened(side, pProp, pShin, pTrue, prop.Overround, shin.ShinZ,
                            mine.DecimalOdds,
                            odds.Length == 2 ? odds[1 - yi] : double.NaN,   // meaningful only on a two-way
                            prop.Overround + 1.0,
                            _oracle.AgeMs(mine), mine.MaxContracts, mine.Live,
                            wsAsk, yes ? top.YesAskDepth : top.NoAskDepth, top.AgeMs, evWs,
                            odds.Length,
                            string.Join(";", odds.Select(o => o.ToString("0.####", CultureInfo.InvariantCulture))),
                            quotes.All(q => q.WsVerified));
    }

    /// <summary>Values a screened candidate at the REST ask, sizes it, and logs it — signal or not.
    /// A row where the WS said +2c and REST said −2c is the most informative row in the file: it is the
    /// phantom being measured, and it is the reason both prices are columns.</summary>
    private void Record(EvPair pair, Screened c, decimal restAsk)
    {
        double px   = (double)restAsk;
        double fee  = EvMath.FeePerContract(px);
        double cost = EvMath.CostPerContract(px);
        double ev      = c.PTrueUsed  - cost;
        double evProp  = c.PTrueProp  - cost;
        double evShin  = c.PTrueShin  - cost;
        double limit   = EvMath.BreakEvenLimit(c.PTrueUsed, _cfg.EvMin);
        bool   inWin   = px >= _cfg.MinPrice && px <= _cfg.MaxPrice;
        var    size    = EvMath.Size(c.PTrueUsed, px, c.Vig, BankrollUsd, ActiveExposureFraction, _cfg.MaxTradeFrac);
        // CAPACITY: how many contracts the book actually offers at or below the break-even limit, and what
        // that is worth. This is the answer to "how big could this bet be" — the Kelly size says what we
        // would WANT, this says what is THERE, and only the smaller of the two is achievable.
        double depthToLimit = (double)_feed.DepthAtOrBetter(pair.KalshiTicker, c.Side == "YES", (decimal)limit);
        double capacityUsd  = depthToLimit * px;
        bool   clears  = ev >= _cfg.EvMin;

        // ── THE SCREENING-ONLY GATE ───────────────────────────────────────────────────────────────────
        // `wv` is the sidecar saying whether this selection is under LIVE WS coverage or is a SCREENING-ONLY
        // re-seed of an untabbed league. A screening-only quote carries a FRESH timestamp and a DELAYED
        // price, so the freshness gate cannot see it — it is fresh by every measure we had, and wrong.
        //
        // That is not theoretical. With the reader parked on the live soccer page, tennis leagues go
        // untabbed and re-seed from the delayed feed; observed 2026-08-21, Pinnacle read 0.378 on a
        // challenger match whose Kalshi book had already moved to 0.17 — a 20c "edge" that was simply an old
        // price. The size of the disagreement is the tell: on a complete book, twenty cents against a sharp
        // book is far likelier to be a stale oracle than an edge.
        //
        // The row is still WRITTEN — M0 exists to observe, and comparing the calibration of verified against
        // unverified rows is exactly how M1 measures what this costs. It just does not count as a signal.
        // ── THE IMPLAUSIBILITY BAND ───────────────────────────────────────────────────────────────────
        // A sharp book and a liquid prediction market do not disagree by twenty points about a football
        // match. When they appear to, the cause has been a data fault every single time: swapped team legs,
        // a wrong-game match, a stale price. Never once an edge.
        //
        // The measured taker EV distribution is the argument. Median −2.09c; only 0.9% of windows exceed
        // 3.5c. A 20c edge is not the tail of that distribution, it is a different distribution — and the
        // pairing's own price gate cannot be relied on to catch it, because its tolerance (0.25) was set to
        // reject GROSS mispairs and a 0.23 disagreement passes while still being worth 18c of phantom EV.
        //
        // Checked HERE rather than at pairing time because this uses the CURRENT prices on both sides: a
        // pair that validated cleanly at 03:00 can still be reading a frozen price at 19:00.
        //
        // The row is still WRITTEN, with its own decision. M0 exists to observe, and if these ever do settle
        // in our favour that is something M1 must be able to see rather than something we quietly deleted.
        double disagree = Math.Abs(c.PTrueUsed - px);
        bool implausible = disagree > _cfg.MaxDisagree;
        if (implausible && clears) Interlocked.Increment(ref Stats.Implausible);

        bool prematch   = _cfg.RequireInPlay && !c.InPlay;
        bool unverified = _cfg.RequireWsVerified && !c.WsVerified;
        bool signal     = clears && !unverified && !implausible && !prematch;
        if (unverified && clears) Interlocked.Increment(ref Stats.ScreeningOnly);
        if (prematch && clears)   Interlocked.Increment(ref Stats.PreMatch);

        if (signal) Interlocked.Increment(ref Stats.Signals);
        else        Interlocked.Increment(ref Stats.RejectedByRest);
        if (size.FlooredToZero) Interlocked.Increment(ref Stats.FlooredToZero);

        _telemetry.Write(new EvSignal(
            DateTime.UtcNow, pair.KalshiTicker, pair.EventId, c.Side, pair.KalshiOutcome, pair.EventTitle,
            pair.SettlementDate, c.InPlay,
            c.PinMine, c.PinOther, c.PinSum, c.Vig, c.ShinZ,
            c.PTrueProp, c.PTrueShin, c.PTrueUsed, _cfg.DeVigMethod, c.OracleAgeMs, c.OracleDepth,
            c.WsAsk, restAsk, c.WsBookAge, c.WsDepth,
            fee, cost, evProp, evShin, ev, c.EvWs, limit,
            size, BankrollUsd, EvMath.OrderFee(px, size.Contracts), size.Contracts * px,
            inWin, signal ? "SIGNAL" : !clears ? "REJECTED_REST"
                          : implausible ? "IMPLAUSIBLE"
                          : prematch ? "SIGNAL_PREMATCH" : "SIGNAL_UNVERIFIED",
            c.NumLegs, c.PinOddsAll, c.WsVerified, depthToLimit, capacityUsd));

        if (clears)
        {
            var col = !signal ? ConsoleColor.DarkGray : inWin ? ConsoleColor.Green : ConsoleColor.DarkYellow;
            Console.ForegroundColor = col;
            Console.WriteLine(
                $"{(signal ? "[+EV]" : "[~EV]")} {pair.KalshiTicker} {c.Side,-3} ev={ev * 100:+0.00;-0.00}c  "
              + $"pTrue={c.PTrueUsed:0.0000}  rest={restAsk:0.0000} (ws {c.WsAsk:0.0000}, "
              + $"gap {(double)(restAsk - c.WsAsk) * 100:+0.0;-0.0}c)  limit={limit:0.0000}  "
              + $"size={size.Contracts}/{depthToLimit:0} avail (${capacityUsd:0})  vig={c.Vig:0.0000}{(c.NumLegs > 2 ? $"  [{c.NumLegs}-way]" : "")}{(inWin ? "" : "  [outside price window]")}"
              + $"{(size.FlooredToZero ? "  [floored to 0 contracts]" : "")}"
              + $"{(signal ? "" : implausible ? $"  [IMPLAUSIBLE: we say {c.PTrueUsed:0.000}, Kalshi says {px:0.000} — "
                                     + $"a {disagree * 100:0}pt gap is a pairing fault, not an edge]"
                                   : prematch ? "  [PRE-MATCH: logged for calibration, not tradeable]"
                                   : "  [SCREENING-ONLY oracle: fresh timestamp, DELAYED price — not a signal]")}");
            Console.ResetColor();
        }
        else if (Verbose)
        {
            Console.WriteLine($"      {pair.KalshiTicker} {c.Side,-3} ws said {c.EvWs * 100:+0.0;-0.0}c, "
                            + $"REST says {ev * 100:+0.0;-0.0}c — not a signal");
        }
    }

    /// <summary>Ask in dollars from the /markets convenience fields, dollar-string first and cents-integer
    /// as a fallback. -1 when the market quotes no ask at all.</summary>
    public static decimal AskDollars(JsonElement mkt, bool yes)
    {
        string[] dollarKeys = yes ? ["yes_ask_dollars", "yes_ask_price"] : ["no_ask_dollars", "no_ask_price"];
        foreach (var k in dollarKeys)
        {
            if (!mkt.TryGetProperty(k, out var el)) continue;
            string? s = el.ValueKind == JsonValueKind.String ? el.GetString() : el.GetRawText();
            if (decimal.TryParse(s, NumberStyles.Any, CultureInfo.InvariantCulture, out var p) && p > 0m) return p;
        }
        if (mkt.TryGetProperty(yes ? "yes_ask" : "no_ask", out var c) && c.ValueKind == JsonValueKind.Number)
        {
            decimal cents = c.GetDecimal();
            if (cents > 0m) return Math.Round(cents / 100m, 4);
        }
        return -1m;
    }
}
