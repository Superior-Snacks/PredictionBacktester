using System.Globalization;
using System.Net.Http;
using System.Text.Json;

namespace HardVenArb;

/// <summary>
/// Live client for the HardVen betting-site venue. It is a THIN HTTP client to the local sidecar
/// (the same process that serves <c>GET /odds</c>) — the sidecar owns the Pinnacle session/auth, so all
/// balance + bet traffic routes through it (<c>GET /balance</c>, <c>POST /bet</c>, <c>GET /bets/{id}</c>),
/// never a second Pinnacle connection from C#.
///
/// State (2026-07-20): ALL methods are wired to the sidecar. <see cref="GetUsdcBalanceAsync"/> returns the
/// real Pinnacle wallet balance FX-converted to USD (Kalshi is USD; the Pinnacle account is EUR). Fee/tick
/// reads return Pinnacle-correct benign values (no per-contract taker fee — the vig is baked into the decimal
/// odds). <see cref="SubmitOrderAsync"/> POSTs <c>/bet</c>, <see cref="GetOrderAsync"/> polls <c>/bets/{id}</c>,
/// <see cref="GetTokenBalanceAsync"/> sums <c>/bets/open</c>.
///
/// <para><b>Real money still requires the sidecar's own gates</b>, which are independent of this class:
/// <c>HARDVEN_BET_ENABLE=1</c> AND an implemented <c>_place_via_ui()</c>. With either missing the sidecar
/// replies <c>accepted=false</c> and places nothing, so this client can be exercised end-to-end (the
/// "preview dress rehearsal") without risk.</para>
/// </summary>
public sealed class HardVenOrderClient : IHardVenOrderExecutor
{
    private readonly HardVenApiConfig _config;
    private readonly string _sidecarBase;   // e.g. http://127.0.0.1:8787 — the odds/balance/bet sidecar
    private readonly decimal _fxToUsd;      // USD per account-unit (EUR≈1.08); 1.0 = USD book / no-op
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(15) };

    public HardVenOrderClient(HardVenApiConfig config, string sidecarBaseUrl, decimal fxToUsd)
    {
        _config      = config;
        _sidecarBase = (sidecarBaseUrl ?? "").TrimEnd('/');
        _fxToUsd     = fxToUsd > 0m ? fxToUsd : 1.0m;
    }

    // ── Read-only: live via the sidecar ────────────────────────────────────────
    /// <summary>Real Pinnacle wallet cash, FX-converted to USD. 0 on any error (executor treats 0 as
    /// "can't fund" and low-cash-alerts / skips — never throws out of the balance refresh loop).</summary>
    public async Task<decimal> GetUsdcBalanceAsync()
    {
        if (string.IsNullOrWhiteSpace(_sidecarBase)) return 0m;
        try
        {
            using var resp = await _http.GetAsync($"{_sidecarBase}/balance");
            if (!resp.IsSuccessStatusCode) return 0m;
            using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync());
            if (!doc.RootElement.TryGetProperty("balance", out var b) || !b.TryGetDecimal(out var amt))
                return 0m;
            return Math.Round(amt * _fxToUsd, 2);   // account currency → USD-equivalent
        }
        catch
        {
            return 0m;
        }
    }

    /// <summary>Position on a selection = the payout of the open (unsettled) back bets on it, in USD-equivalent
    /// $1-payout contracts (<c>stake × odds × fx</c>) — the same unit the feed serves as depth and the executor
    /// counts as "shares". Reads the sidecar's <c>GET /bets/open</c>.
    ///
    /// <para>Expected bet shape (the contract <c>open_bets()</c> must satisfy): <c>selection_id</c>, <c>stake</c>
    /// (account currency), <c>odds</c> (decimal, actually accepted). While <c>open_bets()</c> is unimplemented it
    /// returns <c>[]</c> → 0 here, i.e. "flat", which is the safe reading: recovery/reconcile then act on the
    /// in-memory fill record rather than believing a phantom position.</para>
    ///
    /// <para>0 on any error — this is polled by reconcile loops that must not throw.</para></summary>
    public async Task<decimal> GetTokenBalanceAsync(string tokenId)
    {
        if (string.IsNullOrWhiteSpace(_sidecarBase) || string.IsNullOrWhiteSpace(tokenId)) return 0m;
        try
        {
            using var resp = await _http.GetAsync($"{_sidecarBase}/bets/open");
            if (!resp.IsSuccessStatusCode) return 0m;
            using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync());
            if (!doc.RootElement.TryGetProperty("bets", out var bets) || bets.ValueKind != JsonValueKind.Array)
                return 0m;

            decimal payout = 0m;
            foreach (var b in bets.EnumerateArray())
            {
                if (!b.TryGetProperty("selection_id", out var sel) || sel.GetString() != tokenId) continue;
                decimal stake = ReadDecimal(b, "stake");
                decimal odds  = ReadDecimal(b, "odds");
                if (stake > 0m && odds > 0m) payout += stake * odds * _fxToUsd;   // account-currency stake → USD payout
            }
            return Math.Round(payout, 4);
        }
        catch
        {
            return 0m;
        }
    }

    /// <summary>Tolerant numeric read — the sidecar may serialize amounts as JSON numbers or strings.</summary>
    private static decimal ReadDecimal(JsonElement el, string prop)
    {
        if (!el.TryGetProperty(prop, out var v)) return 0m;
        if (v.ValueKind == JsonValueKind.Number && v.TryGetDecimal(out var n)) return n;
        if (v.ValueKind == JsonValueKind.String &&
            decimal.TryParse(v.GetString(), NumberStyles.Any, CultureInfo.InvariantCulture, out var s)) return s;
        return 0m;
    }

    /// <summary>Pinnacle has no per-contract taker fee — the margin (vig) is inside the decimal odds, so the
    /// 1/odds cost the feed serves already accounts for it. 0 = no add-on fee.</summary>
    public Task<int> GetTakerFeeAsync(string tokenId) => Task.FromResult(0);

    /// <summary>No separate HardVen fee curve (vig is in the odds) → R=0 so the executor adds no phantom
    /// per-share fee to the HardVen leg.</summary>
    public Task<(decimal R, double E)> GetFeeParamsAsync(string tokenId) => Task.FromResult((0m, 1.0));

    /// <summary>Odds price granularity placeholder (a back bet takes the offered odds, not a limit tick).</summary>
    public Task<string> GetTickSizeAsync(string tokenId) => Task.FromResult("0.01");

    // ── Mutating: routed to the sidecar /bet endpoint ─────────────────────────
    /// <summary>Place a back bet via the sidecar. Converts the executor's contract world into the book's
    /// stake world and maps the reply back into the fill shape <c>ExtractHardVenFill</c> parses.
    ///
    /// <para><b>Unit conversion.</b> The executor thinks in $1-payout contracts at <c>price = 1/odds</c>
    /// (unitless). A sportsbook takes a STAKE and pays <c>stake × odds</c>. So
    /// <c>stakeUsd = size × price</c>, and since the Pinnacle account is EUR,
    /// <c>stakeAccount = stakeUsd / HARDVEN_FX_TO_USD</c>. The reply's accepted stake/odds are converted back
    /// the same way, so a partial or slipped acceptance surfaces as fewer filled shares — which is exactly
    /// what the executor's slippage recompute and recovery paths already handle.</para>
    ///
    /// <para><b>max_odds</b> is the sidecar's floor on acceptable decimal odds (accept only if the offered
    /// odds are &gt;= it), so it is <c>1/price</c> — the worst odds that still make the arb work.</para>
    ///
    /// <para><b>No real money without two gates.</b> This only issues an HTTP call; the sidecar itself
    /// refuses unless <c>HARDVEN_BET_ENABLE=1</c> AND the UI bet-slip path exists. Until then it replies
    /// <c>accepted=false, reason="preview only …"</c>, which maps to <c>success=false</c> → the executor
    /// records a failed leg (0 shares) and runs recovery. That is the intended dress-rehearsal behaviour.</para>
    ///
    /// <para><b>SELL throws by design.</b> A placed sportsbook bet is IRREVERSIBLE — there is no lay. The
    /// no-reverse recovery model should never route a sell here, so reaching this is a bug in the executor,
    /// and faking success would leave the bot believing it had flattened a position it still holds.</para></summary>
    public async Task<string> SubmitOrderAsync(
        string tokenId, decimal price, decimal size, int side,
        bool negRisk = false, string tickSize = "0.01", int feeRateBps = 0)
    {
        if (side != 0)
            throw new NotSupportedException(
                $"HardVen SELL requested on {tokenId} — a placed sportsbook bet cannot be sold or cancelled. " +
                "Recovery must complete the hedge on Kalshi or hold to settlement, never reverse the HardVen leg.");
        if (string.IsNullOrWhiteSpace(_sidecarBase))
            throw new InvalidOperationException("HardVen sidecar URL not configured — cannot place a bet.");
        if (price <= 0m || size <= 0m)
            throw new ArgumentOutOfRangeException(nameof(size), $"invalid HardVen order price={price} size={size}");

        decimal stakeUsd = size * price;                          // USD at risk
        // → account currency (EUR), the book's stake unit, at its 2-dp granularity. Rounded DOWN, never to
        // nearest: rounding up would buy MORE payout than the arb sized for, and the excess sits on the
        // IRREVERSIBLE leg (must be hedged on Kalshi or held to settlement). Rounding down leaves the excess
        // on Kalshi instead, which recovery can simply reverse. Costs <1% of a small stake; removes a
        // systematic bias toward the expensive side of the asymmetry.
        decimal stakeAcct = Math.Floor(stakeUsd / _fxToUsd * 100m) / 100m;
        decimal minOdds   = Math.Round(1m / price, 4);            // accept only at or above these odds
        if (stakeAcct <= 0m)
            throw new ArgumentOutOfRangeException(nameof(size),
                $"HardVen stake rounds to 0 at the book's 2-dp granularity (size={size} price={price} fx={_fxToUsd})");

        var payload = JsonSerializer.Serialize(new
        {
            selection_id = tokenId,
            stake        = stakeAcct,
            max_odds     = minOdds,
        });

        using var content = new StringContent(payload, System.Text.Encoding.UTF8, "application/json");
        using var resp    = await _http.PostAsync($"{_sidecarBase}/bet", content);
        string body       = await resp.Content.ReadAsStringAsync();
        if (!resp.IsSuccessStatusCode)
            throw new HttpRequestException($"HardVen /bet HTTP {(int)resp.StatusCode} — {Truncate(body)}");

        using var doc = JsonDocument.Parse(body);
        var root      = doc.RootElement;

        bool accepted = root.TryGetProperty("accepted", out var acc) && acc.ValueKind == JsonValueKind.True;
        string betId  = root.TryGetProperty("bet_id", out var bid) ? bid.GetString() ?? "" : "";
        string reason = root.TryGetProperty("reason", out var rsn) ? rsn.GetString() ?? "" : "";

        if (!accepted)
            return JsonSerializer.Serialize(new
            {
                success = false,
                orderID = betId,
                status  = "rejected",
                reason  = string.IsNullOrEmpty(reason) ? "bet not accepted" : reason,
            });

        // Accepted: recompute the fill from what the book ACTUALLY took (odds can slip between quote and slip).
        decimal actualOdds  = ReadDecimal(root, "actual_odds");
        if (actualOdds <= 0m) actualOdds = minOdds;
        decimal actualStake = ReadDecimal(root, "stake");
        if (actualStake <= 0m) actualStake = stakeAcct;

        decimal spentUsd     = actualStake * _fxToUsd;            // account currency → USD
        decimal filledShares = spentUsd * actualOdds;             // payout in USD-equivalent $1 contracts

        return JsonSerializer.Serialize(new
        {
            success      = true,
            orderID      = betId,
            status       = "matched",
            takingAmount = Math.Round(filledShares, 4).ToString(CultureInfo.InvariantCulture),   // shares received
            makingAmount = Math.Round(spentUsd, 4).ToString(CultureInfo.InvariantCulture),       // USD spent
        });
    }

    /// <summary>Poll a placed bet via <c>GET /bets/{id}</c>, mapped to the <c>status</c>/<c>size_matched</c>
    /// shape the executor's fill-poll fallback reads. A settled/accepted bet reports <c>matched</c> with its
    /// payout as matched size. An unknown id (or the not-yet-implemented <c>bet()</c>, which returns null)
    /// reports <c>unknown</c> with 0 matched — the executor then keeps its own fill record rather than
    /// inventing one.</summary>
    public async Task<string> GetOrderAsync(string orderId)
    {
        if (string.IsNullOrWhiteSpace(_sidecarBase) || string.IsNullOrWhiteSpace(orderId))
            return JsonSerializer.Serialize(new { orderID = orderId, status = "unknown", size_matched = "0" });
        try
        {
            using var resp = await _http.GetAsync($"{_sidecarBase}/bets/{Uri.EscapeDataString(orderId)}");
            if (!resp.IsSuccessStatusCode)
                return JsonSerializer.Serialize(new { orderID = orderId, status = "unknown", size_matched = "0" });

            using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync());
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                return JsonSerializer.Serialize(new { orderID = orderId, status = "unknown", size_matched = "0" });

            decimal stake = ReadDecimal(root, "stake");
            decimal odds  = ReadDecimal(root, "odds");
            if (odds <= 0m) odds = ReadDecimal(root, "actual_odds");
            decimal payout = stake > 0m && odds > 0m ? stake * odds * _fxToUsd : 0m;

            return JsonSerializer.Serialize(new
            {
                orderID      = orderId,
                status       = payout > 0m ? "matched" : "unknown",
                size_matched = Math.Round(payout, 4).ToString(CultureInfo.InvariantCulture),
            });
        }
        catch
        {
            return JsonSerializer.Serialize(new { orderID = orderId, status = "unknown", size_matched = "0" });
        }
    }

    private static string Truncate(string s) => s.Length <= 200 ? s : s[..200];

    public Task UpdateBalanceAllowanceAsync(string tokenId) => Task.CompletedTask;   // no allowance concept on a sportsbook
}
