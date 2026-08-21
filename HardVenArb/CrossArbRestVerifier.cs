using System.Collections.Concurrent;
using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using PredictionBacktester.Engine.LiveExecution;

namespace HardVenArb;

/// <summary>
/// Fires an async REST check against Kalshi + HardVen whenever a new arb window opens.
/// Calls UpdateRestVerification on the telemetry strategy with the confirmed price and delay.
/// </summary>
public class CrossArbRestVerifier
{
    // ONE definition of Kalshi's fee, matching CrossArbExecutor and the telemetry strategy. Duplicated
    // rather than shared only because this class has no reference to either; if a third copy appears it
    // should become a single helper instead.
    private const decimal KalshiFeeRate = 0.07m;
    private static decimal KalshiFee(decimal p) => KalshiFeeRate * p * (1m - p);

    private readonly KalshiOrderClient _kalshi;
    private readonly HttpClient _http;
    private readonly CrossPlatformArbTelemetryStrategy _telemetry;
    private readonly SemaphoreSlim _sem = new(2, 2); // max 2 concurrent REST checks

    /// <summary>Tick size per HardVen token ID, populated lazily from /book REST responses.</summary>
    public ConcurrentDictionary<string, string> HardVenTickSizes { get; } = new();

    // HardVen odds come from the local sidecar (the only source) — the verifier re-reads /odds to confirm
    // a window at arb-open. (Replaces a dead clob.hardven.com host left over from the Poly→HardVen rename.)
    private readonly string _sidecarBase;

    public CrossArbRestVerifier(KalshiOrderClient kalshi, CrossPlatformArbTelemetryStrategy telemetry,
                                string? socksProxy = null, string? sidecarBase = null)
    {
        _kalshi    = kalshi;
        _telemetry = telemetry;
        _sidecarBase = (sidecarBase ?? "http://127.0.0.1:8787").TrimEnd('/');
        if (!string.IsNullOrEmpty(socksProxy))
        {
            var handler = new HttpClientHandler
            {
                Proxy    = new System.Net.WebProxy(socksProxy),
                UseProxy = true
            };
            _http = new HttpClient(handler) { Timeout = HttpCeiling };
        }
        else
        {
            _http = new HttpClient { Timeout = HttpCeiling };
        }
    }

    /// <summary>Subscribe to telemetry.OnArbOpened and call this method.</summary>
    public void OnArbOpened(string pairId, decimal netCost, string arbType, decimal depth, decimal kLegAsk, decimal pLegAsk)
    {
        DebugLog.Trades($"CrossArbRestVerifier.OnArbOpened: {pairId} {arbType} — queuing REST check");
        _ = Task.Run(async () =>
        {
            try { await VerifyAsync(pairId, arbType); }
            catch (Exception ex)
            {
                Console.WriteLine($"[REST CHECK ERROR] {pairId}: {ApiErrorHelper.ClassifyHardVen(ex)}");
                DebugLog.Trades($"VerifyAsync unhandled exception for {pairId}: {ex}");
            }
        });
    }

    /// <summary>
    /// Fetches live ask prices for both legs directly. Used by the executor as a
    /// stale-book gate before firing orders when venue time-skew is large.
    /// Returns (-1,-1) if either fetch fails.
    /// </summary>
    public async Task<(decimal KAsk, decimal PAsk, bool? PVenueFresh)> GetCurrentAsksAsync(
        CrossPair pair, string arbType)
    {
        string hardvenToken = arbType == "K_YES_P_NO" ? pair.HardVenNoTokenId : pair.HardVenYesTokenId;
        decimal kAsk = await GetKalshiAskAsync(pair.KalshiTicker, arbType);
        var (pAsk, pVenueFresh) = await GetHardVenAskAsync(hardvenToken);
        return (kAsk, pAsk, pVenueFresh);
    }

    /// <summary>SYNCHRONOUS WS-verify: ask the sidecar to point its ROVING tab at this selection's league and
    /// wait for live WS coverage, so the caller can re-check and fire on the SAME arb window.
    ///
    /// <para>Replaces skip-and-hope. The old fire-and-forget <c>/verify</c> only promoted the league for some
    /// FUTURE window, and asked the DEDICATED tab pool for a slot — answering <c>at-cap</c> whenever that pool
    /// was full, which permanently blocked those leagues rather than delaying them. The rove tab is uncapped.</para>
    ///
    /// <para>Returns false on any failure (timeout, no rove tab, bet in flight) — the caller then skips, exactly
    /// as before. Never throws.</para></summary>
    /// <summary>Client-wide ceiling ONLY — every call sets its own deadline via a CancellationTokenSource.
    /// This was 5s, which silently overrode them all: HttpClient.Timeout fires first, so a slip quote asking
    /// for 20s got a TaskCanceledException at 5s and HARDVEN_SLIP_QUOTE_TIMEOUT_SEC did nothing. Observed
    /// 2026-08-12 killing the best arb of the session (net 0.9373, 4.5c/share) three times in a row while a
    /// successful quote elsewhere took 1046ms. Keep this ABOVE every per-call timeout.</summary>
    private static readonly TimeSpan HttpCeiling = TimeSpan.FromSeconds(
        double.TryParse(Environment.GetEnvironmentVariable("HARDVEN_SIDECAR_HTTP_CEILING_SEC"),
                        System.Globalization.NumberStyles.Any,
                        CultureInfo.InvariantCulture, out var _hc) && _hc > 0 ? _hc : 60.0);

    public async Task<bool> VerifyNowAsync(string hardvenToken, double timeoutSec = 10.0)
    {
        if (string.IsNullOrWhiteSpace(hardvenToken)) return false;
        try
        {
            string url = $"{_sidecarBase}/verify_now?selection_id={Uri.EscapeDataString(hardvenToken)}"
                       + $"&timeout={timeoutSec.ToString(CultureInfo.InvariantCulture)}";
            // Own deadline: the client-wide ceiling is now generous, so each call states its own. The
            // sidecar is asked to wait `timeoutSec` for a WS push, so allow a little over that.
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(timeoutSec + 5));
            using var resp = await _http.PostAsync(url, null, cts.Token);
            if (!resp.IsSuccessStatusCode) return false;
            using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync());
            return doc.RootElement.TryGetProperty("verified", out var v) && v.ValueKind == JsonValueKind.True;
        }
        catch (Exception ex)
        {
            DebugLog.Trades($"VerifyNowAsync {hardvenToken}: {ex.GetType().Name}: {ex.Message}");
            return false;
        }
    }

    /// <summary>
    /// Opens the venue's BETSLIP for this selection and returns the true offered price (implied, 0-1), or
    /// -1 if it could not be quoted. Places nothing.
    ///
    /// This is the only independent confirmation of a HardVen price that exists: `/odds` answers from the
    /// sidecar cache, so verifying against it is a cache agreeing with itself. The slip is what the venue
    /// will actually honour — and it is exactly where the two diverge (observed 2026-08-11: screened 1.5102,
    /// slip 1.5100, leg rejected).
    ///
    /// Costs seconds, so it belongs on the execution path only. Returns -1 for a book that cannot quote
    /// (404) so the caller can fall back rather than refuse a venue that structurally has no slip read.
    /// </summary>
    /// <summary>Name the venue showed for the most recent slip quote, e.g. "Sabrina Dias (Sets)".
    /// Set by SlipQuoteAsync; read immediately after, on the same call path (slip quotes are serialised
    /// by the sidecar's rover lock, and the executor quotes one leg at a time).</summary>
    public string LastSlipLabel { get; private set; } = "";

    /// <summary>Tier that served the last slip quote ("cache" / "sport-tab" / "rover"). The
    /// sampler throttles harder after a rover quote, since that one navigated the browser.</summary>
    public string LastSlipVia { get; private set; } = "";

    /// <summary>Whether the last slip quote actually CLICKED at the venue. False for the refusals decided
    /// from data the sidecar already holds — an event the venue will not put on a betslip, an event already
    /// subscribed, a row rendering no odds. Those cost nothing and should not spend a sampling interval,
    /// which is the difference between 12 samples in an evening and one per arb worth checking.
    /// Defaults to TRUE on a parse failure or an exception: assuming a click happened is the cautious
    /// reading, since the penalty for being wrong is slowing down rather than hammering the venue.</summary>
    public bool LastSlipClicked { get; private set; } = true;

    /// <summary>True when the venue flagged the last quote's event as NOT available for accumulators.
    /// Surfaced so a SUCCESSFUL quote on a flagged event is visible — that single line is what proves
    /// the flag does not block a betslip read, and it is why the gate was opened rather than obeyed.</summary>
    public bool LastSlipAccaFlagged { get; private set; }

    /// <summary>Close the betslip the sidecar opened. Idempotent and best-effort — never throws.
    ///
    /// Worth calling even when the quote failed: the click may have opened a slip before whatever went
    /// wrong afterwards. Leaving it open keeps the event subscribed, and a subscribed event whose cached
    /// price ages out can never be quoted again ("re-clicking cannot help"), so an un-closed slip
    /// permanently burns that market for the life of the socket.</summary>
    public async Task<bool> SlipCloseAsync(double timeoutSec = 20.0)
    {
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(timeoutSec));
            using var resp = await _http.PostAsync($"{_sidecarBase}/slip_close", null, cts.Token);
            if (!resp.IsSuccessStatusCode) return false;
            using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync(cts.Token));
            return doc.RootElement.TryGetProperty("closed", out var c) && c.ValueKind == JsonValueKind.True;
        }
        catch (Exception ex)
        {
            DebugLog.Trades($"SlipCloseAsync: {ex.GetType().Name}: {ex.Message}");
            return false;
        }
    }

    public async Task<(decimal Price, string Error)> SlipQuoteAsync(string hardvenToken, double timeoutSec = 20.0)
    {
        LastSlipClicked = true;   // pessimistic until the sidecar says otherwise (see the property)
        try
        {
            string url = $"{_sidecarBase}/slip_quote?selection_id={Uri.EscapeDataString(hardvenToken)}";
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(timeoutSec));
            using var resp = await _http.PostAsync(url, null, cts.Token);
            if (resp.StatusCode == System.Net.HttpStatusCode.NotFound)
                return (-1m, "unsupported");            // book has no betslip read — caller decides
            string body = await resp.Content.ReadAsStringAsync(cts.Token);
            using var doc = JsonDocument.Parse(body);
            var root = doc.RootElement;
            if (root.TryGetProperty("clicked", out var ck) &&
                (ck.ValueKind == JsonValueKind.True || ck.ValueKind == JsonValueKind.False))
                LastSlipClicked = ck.ValueKind == JsonValueKind.True;
            LastSlipAccaFlagged = root.TryGetProperty("acca", out var ac) && ac.ValueKind == JsonValueKind.False;
            if (!(root.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True))
                return (-1m, root.TryGetProperty("error", out var e) ? (e.GetString() ?? "?") : "not ok");
            decimal price = root.TryGetProperty("implied_price", out var ip) && ip.TryGetDecimal(out var p)
                ? p : -1m;
            // The venue's own name for this selection ("Sabrina Dias (Sets)"). Returned as the Error slot's
            // sibling via LastSlipLabel rather than widening the tuple for every existing caller.
            LastSlipLabel = root.TryGetProperty("selection_label", out var sl) ? (sl.GetString() ?? "") : "";
            // Which tier served it: "cache" / "sport-tab" (cheap) vs "rover" (navigated).
            LastSlipVia   = root.TryGetProperty("via", out var vv) ? (vv.GetString() ?? "") : "";
            return price > 0m ? (price, "") : (-1m, "no implied_price in the quote");
        }
        catch (Exception ex)
        {
            DebugLog.Trades($"SlipQuoteAsync {hardvenToken}: {ex.GetType().Name}: {ex.Message}");
            return (-1m, $"{ex.GetType().Name}: {ex.Message}");
        }
    }

    /// <summary>
    /// Fetches live bid prices for both held legs. Used by early-exit monitoring when
    /// WS books are stale. Returns (-1,-1) if either fetch fails.
    /// K_YES_P_NO: we hold K YES + P NO → fetch yes_bid on Kalshi, bids on HardVen NO token.
    /// K_NO_P_YES: we hold K NO  + P YES → fetch no_bid  on Kalshi, bids on HardVen YES token.
    /// </summary>
    public async Task<(decimal KBid, decimal PBid)> GetCurrentBidsAsync(CrossPair pair, string arbType)
    {
        string hardvenToken = arbType == "K_YES_P_NO" ? pair.HardVenNoTokenId : pair.HardVenYesTokenId;
        decimal kBid = await GetKalshiBidAsync(pair.KalshiTicker, arbType);
        decimal pBid = await GetHardVenBidAsync(hardvenToken);
        return (kBid, pBid);
    }

    private async Task VerifyAsync(string pairId, string arbType)
    {
        DebugLog.Trades($"VerifyAsync {pairId}: waiting for semaphore (current count unknown)");
        await _sem.WaitAsync();
        var sw = Stopwatch.StartNew();
        try
        {
            var pair = _telemetry.GetPair(pairId);
            if (pair == null)
            {
                DebugLog.Trades($"VerifyAsync {pairId}: pair not found in telemetry, skipping");
                return;
            }

            DebugLog.Trades($"VerifyAsync {pair.Label}: fetching Kalshi ask for {pair.KalshiTicker}");
            decimal kAsk = await GetKalshiAskAsync(pair.KalshiTicker, arbType);
            DebugLog.Trades($"VerifyAsync {pair.Label}: Kalshi ask={kAsk:0.0000}");

            string hardvenToken = arbType == "K_YES_P_NO" ? pair.HardVenNoTokenId : pair.HardVenYesTokenId;
            DebugLog.Trades($"VerifyAsync {pair.Label}: fetching HardVen ask for token {hardvenToken[..Math.Min(8, hardvenToken.Length)]}...");
            var (pAsk, pVenueFresh) = await GetHardVenAskAsync(hardvenToken);
            DebugLog.Trades($"VerifyAsync {pair.Label}: HardVen ask={pAsk:0.0000} venueFresh={pVenueFresh}");

            sw.Stop();
            // ── THE FEE IS PART OF THE PRICE ────────────────────────────────────────────────────────
            // This confirmed on the GROSS sum, so it called an arb whenever the two asks were under a
            // dollar — ignoring Kalshi's 0.07*p*(1-p), which is up to 1.75c per contract and near the money
            // is most of a thin edge. Every REST confirmation was therefore up to 1.75c optimistic, and the
            // confirm rate looked correspondingly better than it was.
            //
            // Seen on real money 2026-08-21: a window confirmed at gross 0.9831 had a true net of 0.9998 —
            // announced as 1.7c of room, actually 0.02c — and the fill that followed finished 0.48c down.
            // This is the LAST check before a press, so it is exactly the wrong place to be optimistic.
            decimal gross = kAsk + pAsk;
            decimal net   = gross + KalshiFee(kAsk);
            bool confirmed = kAsk > 0m && pAsk > 0m && net < 1.00m;
            DebugLog.Trades($"VerifyAsync {pair.Label}: gross={gross:0.0000} net={net:0.0000} " +
                            $"confirmed={confirmed} in {sw.ElapsedMilliseconds}ms");

            _telemetry.UpdateRestVerification(pairId, confirmed, kAsk, pAsk, sw.ElapsedMilliseconds);

            if (!confirmed)
            {
                string verdict = kAsk < 0m || pAsk < 0m ? "FETCH_FAIL" : "NO_ARB";
                Console.WriteLine($"[REST CHECK] {pair.Label} | {arbType} | " +
                                  $"K={kAsk:0.0000} P={pAsk:0.0000} gross={gross:0.0000} net={net:0.0000} | " +
                                  $"{verdict} in {sw.ElapsedMilliseconds}ms");
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[REST CHECK ERROR] {pairId}: {ApiErrorHelper.ClassifyHardVen(ex)}");
            DebugLog.Trades($"VerifyAsync caught exception for {pairId}: {ex}");
        }
        finally
        {
            _sem.Release();
            DebugLog.Trades($"VerifyAsync {pairId}: semaphore released");
        }
    }

    // Uses /markets/{ticker} convenience price fields (yes_ask_dollars / no_ask_dollars).
    // K_YES_P_NO: we buy YES on Kalshi → want yes_ask
    // K_NO_P_YES: we buy NO  on Kalshi → want no_ask
    /// <summary>Kalshi ask straight from REST, for SHADOW measurement only. Public so the telemetry can
    /// poll it without going through any of the verify plumbing — and deliberately nothing else: the value
    /// must never reach a book, a price or a decision, or the comparison it exists to make is worthless.</summary>
    public Task<decimal> ShadowKalshiAskAsync(string ticker, string arbType) => GetKalshiAskAsync(ticker, arbType);

    private async Task<decimal> GetKalshiAskAsync(string ticker, string arbType)
    {
        using var doc = await _kalshi.GetMarketAsync(ticker);
        var mkt = doc.RootElement.TryGetProperty("market", out var m) ? m : doc.RootElement;

        // Dollar-string fields are preferred; fall back to cents-integer fields
        bool buyYes = arbType == "K_YES_P_NO";
        string[] dollarKeys = buyYes
            ? ["yes_ask_dollars", "yes_ask_price"]
            : ["no_ask_dollars",  "no_ask_price"];
        string centsKey = buyYes ? "yes_ask" : "no_ask";

        foreach (var key in dollarKeys)
        {
            if (!mkt.TryGetProperty(key, out var el)) continue;
            string? s = el.ValueKind == JsonValueKind.String ? el.GetString() : el.GetRawText();
            if (decimal.TryParse(s, NumberStyles.Any, CultureInfo.InvariantCulture, out decimal p) && p > 0m)
            {
                DebugLog.Trades($"GetKalshiAskAsync {ticker}: found {key}={p:0.0000}");
                return p;
            }
        }

        // Fallback: cents integer (e.g. yes_ask = 65 → 0.65)
        if (mkt.TryGetProperty(centsKey, out var centsEl) && centsEl.ValueKind == JsonValueKind.Number)
        {
            decimal cents = centsEl.GetDecimal();
            if (cents > 0m)
            {
                decimal result = Math.Round(cents / 100m, 4);
                DebugLog.Trades($"GetKalshiAskAsync {ticker}: fallback cents {centsKey}={cents} → {result:0.0000}");
                return result;
            }
        }

        DebugLog.Trades($"GetKalshiAskAsync {ticker}: no valid ask field found");
        return -1m;
    }

    /// <summary>
    /// Re-reads one HardVen selection from the sidecar /odds → (implied_price, status).
    /// implied_price is the per-contract cost (= 1/decimal_odds = the "ask"); status is "open"/"suspended".
    /// (-1, "") on any error or missing selection.
    /// </summary>
    /// <summary>
    /// Reads a HardVen selection from the sidecar. <paramref name="fromVenue"/> asks the sidecar to
    /// re-read it FROM THE VENUE first (`?fresh=1`) and reports, via <c>VenueFresh</c>, whether that
    /// actually happened.
    ///
    /// WHY THE FLAG MATTERS. `/odds` normally answers from the same cache the screening price came from,
    /// so verifying against it is a cache agreeing with itself: measured 2026-08-11, the HardVen leg
    /// "confirmed" 110/110 windows while the independently-checked Kalshi leg disagreed with its screening
    /// price 76% of the time. That was not a better venue, it was a mirror. With `fresh=1` the sidecar
    /// re-seeds the league from Pinnacle before answering — and when it cannot, `venue_fresh` comes back
    /// false so the caller can refuse rather than silently accept the cached number as confirmation.
    /// </summary>
    private async Task<(decimal price, string status, bool? venueFresh)> GetHardVenSelectionAsync(
        string tokenId, bool fromVenue = false)
    {
        string url = $"{_sidecarBase}/odds?selections={Uri.EscapeDataString(tokenId)}"
                   + (fromVenue ? "&fresh=1" : "");
        // Own deadline (see HttpCeiling): a price read must stay snappy — this sits on the execution path.
        using var oddsCts = new CancellationTokenSource(TimeSpan.FromSeconds(fromVenue ? 15 : 5));
        string json = await _http.GetStringAsync(url, oddsCts.Token);
        using var doc = JsonDocument.Parse(json);
        // TRI-STATE (see the sidecar's /odds):
        //   true  = "ok"          the venue confirmed this price
        //   false = "failed"      we asked the venue and got nothing -> caller must REFUSE
        //   null  = "unsupported" this book has no independent price read at all, or we did not ask.
        // null must NOT block. BetInAsia is push-only with no REST price endpoint, so demanding an
        // independent re-read there would refuse every arb forever; its correctness gate is the betslip.
        bool? venueFresh = null;
        if (doc.RootElement.TryGetProperty("venue_refetch", out var vr) && vr.ValueKind == JsonValueKind.String)
        {
            string s = vr.GetString() ?? "";
            if (s == "ok") venueFresh = true;
            else if (s == "failed") venueFresh = false;
            // "unsupported" deliberately leaves it null
        }
        if (!doc.RootElement.TryGetProperty("selections", out var sels) ||
            !sels.TryGetProperty(tokenId, out var sel))
            return (-1m, "", venueFresh);
        string status = sel.TryGetProperty("status", out var st) ? (st.GetString() ?? "") : "";
        decimal price = sel.TryGetProperty("implied_price", out var ip) && ip.TryGetDecimal(out var p) ? p : -1m;
        return (price, status, venueFresh);
    }

    /// <summary>
    /// Whether a HardVen token is currently tradeable (sidecar reports status "open"). False on error.
    /// </summary>
    public async Task<bool> CheckHardVenTokenAsync(string tokenId)
    {
        try { return (await GetHardVenSelectionAsync(tokenId)).status == "open"; }
        catch { return false; }
    }

    // Same structure as GetKalshiAskAsync but reads bid fields.
    // K_YES_P_NO: hold YES → sell YES → yes_bid; K_NO_P_YES: hold NO → sell NO → no_bid.
    private async Task<decimal> GetKalshiBidAsync(string ticker, string arbType)
    {
        using var doc = await _kalshi.GetMarketAsync(ticker);
        var mkt = doc.RootElement.TryGetProperty("market", out var m) ? m : doc.RootElement;

        bool sellYes = arbType == "K_YES_P_NO";
        string[] dollarKeys = sellYes
            ? ["yes_bid_dollars", "yes_bid_price"]
            : ["no_bid_dollars",  "no_bid_price"];
        string centsKey = sellYes ? "yes_bid" : "no_bid";

        foreach (var key in dollarKeys)
        {
            if (!mkt.TryGetProperty(key, out var el)) continue;
            string? s = el.ValueKind == JsonValueKind.String ? el.GetString() : el.GetRawText();
            if (decimal.TryParse(s, NumberStyles.Any, CultureInfo.InvariantCulture, out decimal p) && p > 0m)
            {
                DebugLog.Trades($"GetKalshiBidAsync {ticker}: found {key}={p:0.0000}");
                return p;
            }
        }

        if (mkt.TryGetProperty(centsKey, out var centsEl) && centsEl.ValueKind == JsonValueKind.Number)
        {
            decimal cents = centsEl.GetDecimal();
            if (cents > 0m)
            {
                decimal result = Math.Round(cents / 100m, 4);
                DebugLog.Trades($"GetKalshiBidAsync {ticker}: fallback cents {centsKey}={cents} → {result:0.0000}");
                return result;
            }
        }

        DebugLog.Trades($"GetKalshiBidAsync {ticker}: no valid bid field found");
        return -1m;
    }

    // HardVen is a BACK-ONLY sportsbook — there is no lay/sell side, so no bid to confirm. Return -1
    // ("no bid"); the executor treats a HardVen leg as non-reversible (it can't sell into a bid).
    private Task<decimal> GetHardVenBidAsync(string tokenId) => Task.FromResult(-1m);

    // HardVen "ask" = the sidecar's per-contract implied price (1/decimal_odds) when the market is open.
    // ALWAYS asks the sidecar to re-read from the venue: every caller of this is a VERIFY path (arb-open
    // telemetry check, or the executor's pre-fire re-check), never the 3s poll loop, so the extra authed
    // league fetch is marginal against the 90s backstop that already re-seeds every active league.
    // Returns venueFresh=false when the venue read did not happen, so a cached echo is never mistaken for
    // confirmation.
    private async Task<(decimal Ask, bool? VenueFresh)> GetHardVenAskAsync(string tokenId)
    {
        var (price, status, venueFresh) = await GetHardVenSelectionAsync(tokenId, fromVenue: true);
        if (status == "open" && price > 0m)
        {
            DebugLog.Trades($"GetHardVenAskAsync {tokenId}: ask={price:0.0000} venueFresh={venueFresh}");
            return (price, venueFresh);
        }
        DebugLog.Trades($"GetHardVenAskAsync {tokenId}: not open / no price (status={status})");
        return (-1m, venueFresh);
    }
}
