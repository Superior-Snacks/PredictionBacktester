using System.Globalization;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace PredictionBacktester.Engine.LiveExecution;

/// <summary>Details of a single 429 back-off, surfaced to callers for logging/journaling.</summary>
public readonly record struct RateLimitRetryInfo(
    string Method, string Path, int StatusCode, int Attempt, int MaxAttempts, double DelaySeconds);

/// <summary>
/// REST client for the Kalshi API. Handles RSA-PSS request signing, market queries,
/// and live IOC order placement / fill polling.
/// </summary>
public class KalshiOrderClient : IKalshiOrderExecutor, IDisposable
{
    private readonly KalshiApiConfig _config;
    private readonly RSA _rsa;
    private readonly HttpClient _http;

    // Full REST path prefix used when computing signatures
    // Signing requires the complete path from root: /trade-api/v2/...
    private const string PathPrefix = "/trade-api/v2";

    // Max 429 retries per request (see DelayForRateLimitAsync). A rate-limited order — above all
    // the recovery reverse that flattens an unhedged leg — must back off and retry, never abandon.
    private const int RateLimitMaxRetries = 5;

    /// <summary>
    /// Optional hook invoked with (relPath, responseBody) for every REST response.
    /// Set this in callers that need raw-response logging (e.g. KalshiPolyCross --debug).
    /// </summary>
    public Action<string, string>? RawResponseLogger { get; set; }

    /// <summary>
    /// Optional hook invoked just before each 429 back-off sleep, so callers can journal how
    /// often (and on which method/path) Kalshi rate limits are hit. Exceptions are swallowed.
    /// </summary>
    public Action<RateLimitRetryInfo>? RateLimitRetryLogger { get; set; }

    public KalshiOrderClient(KalshiApiConfig config)
    {
        _config = config;
        _rsa = LoadPrivateKey(config.PrivateKeyPath);
        _http = new HttpClient { BaseAddress = new Uri(config.BaseRestUrl),
                                 Timeout = config.HttpTimeout };
    }

    // ──────────────────────────────────────────────────────────────────────────
    //  Auth
    // ──────────────────────────────────────────────────────────────────────────

    /// <summary>
    /// Creates the three Kalshi auth headers for a request.
    /// Message signed: {timestampMs}{METHOD}{/trade-api/v2/path-without-query}
    /// </summary>
    public (string key, string timestamp, string signature) CreateAuthHeaders(string method, string relPath)
    {
        string ts = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString();

        // Strip query parameters before signing.
        // If relPath already contains the full path (e.g. WS upgrade), use it as-is.
        string fullPath = relPath.StartsWith("/trade-api/") ? relPath : PathPrefix + relPath;
        string pathForSig = fullPath.Split('?')[0];

        byte[] msgBytes = Encoding.UTF8.GetBytes(ts + method + pathForSig);
        byte[] sigBytes = _rsa.SignData(msgBytes, HashAlgorithmName.SHA256, RSASignaturePadding.Pss);
        string sig = Convert.ToBase64String(sigBytes);

        return (_config.ApiKeyId, ts, sig);
    }

    // ──────────────────────────────────────────────────────────────────────────
    //  HTTP helpers
    // ──────────────────────────────────────────────────────────────────────────

    // Backoff before retrying a 429: honor Retry-After when Kalshi sends it (capped at 10s), else
    // exponential backoff (0.25s→4s) with jitter so concurrent legs don't resync onto one retry tick.
    private static TimeSpan ComputeRateLimitDelay(HttpResponseMessage resp, int attempt)
    {
        TimeSpan delay;
        var ra = resp.Headers.RetryAfter;
        if (ra?.Delta is TimeSpan d && d > TimeSpan.Zero)
            delay = d;
        else if (ra?.Date is DateTimeOffset when && when > DateTimeOffset.UtcNow)
            delay = when - DateTimeOffset.UtcNow;
        else
            delay = TimeSpan.FromMilliseconds(Math.Min(250 * Math.Pow(2, attempt - 1), 4000));
        if (delay > TimeSpan.FromSeconds(10)) delay = TimeSpan.FromSeconds(10);
        delay += TimeSpan.FromMilliseconds(Random.Shared.Next(0, 150));
        return delay;
    }

    // Notify the retry hook (query stripped from the path), then sleep before the next attempt.
    private async Task HandleRateLimitAsync(string method, string relPath, HttpResponseMessage resp, int attempt)
    {
        TimeSpan delay = ComputeRateLimitDelay(resp, attempt);
        try
        {
            RateLimitRetryLogger?.Invoke(new RateLimitRetryInfo(
                method, relPath.Split('?')[0], (int)resp.StatusCode,
                attempt, RateLimitMaxRetries, Math.Round(delay.TotalSeconds, 3)));
        }
        catch { /* logging must never disrupt the retry */ }
        await Task.Delay(delay);
    }

    private async Task<JsonDocument> GetAsync(string relPath)
    {
        for (int attempt = 1; ; attempt++)
        {
            // Re-sign each attempt: the auth signature embeds a timestamp Kalshi rejects once stale.
            var (key, ts, sig) = CreateAuthHeaders("GET", relPath);
            using var req = new HttpRequestMessage(HttpMethod.Get, _config.BaseRestUrl.TrimEnd('/') + relPath);
            req.Headers.Add("KALSHI-ACCESS-KEY", key);
            req.Headers.Add("KALSHI-ACCESS-TIMESTAMP", ts);
            req.Headers.Add("KALSHI-ACCESS-SIGNATURE", sig);
            req.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));

            using var resp = await _http.SendAsync(req);
            string body = await resp.Content.ReadAsStringAsync();
            RawResponseLogger?.Invoke(relPath, body);

            if (resp.IsSuccessStatusCode)
                return JsonDocument.Parse(body);

            if ((int)resp.StatusCode == 429 && attempt <= RateLimitMaxRetries)
            {
                await HandleRateLimitAsync("GET", relPath, resp, attempt);
                continue;
            }

            throw new HttpRequestException(
                $"Kalshi GET {relPath} {(int)resp.StatusCode}: {body[..Math.Min(400, body.Length)]}",
                inner: null, statusCode: resp.StatusCode);
        }
    }

    private async Task<JsonDocument> PostAsync(string relPath, object body)
    {
        string json = JsonSerializer.Serialize(body);
        for (int attempt = 1; ; attempt++)
        {
            // Re-sign + rebuild the request each attempt (signature timestamp; a request isn't reusable).
            // The body — including any client_order_id — is identical across retries, so a 429'd order
            // (rejected at the gateway, never placed) is safely re-sent with no risk of a double fill.
            var (key, ts, sig) = CreateAuthHeaders("POST", relPath);
            using var req = new HttpRequestMessage(HttpMethod.Post, _config.BaseRestUrl.TrimEnd('/') + relPath);
            req.Headers.Add("KALSHI-ACCESS-KEY",       key);
            req.Headers.Add("KALSHI-ACCESS-TIMESTAMP", ts);
            req.Headers.Add("KALSHI-ACCESS-SIGNATURE", sig);
            req.Content = new StringContent(json, Encoding.UTF8, "application/json");

            using var resp = await _http.SendAsync(req);
            string respBody = await resp.Content.ReadAsStringAsync();
            RawResponseLogger?.Invoke(relPath, respBody);

            if (resp.IsSuccessStatusCode)
                return JsonDocument.Parse(respBody);

            if ((int)resp.StatusCode == 429 && attempt <= RateLimitMaxRetries)
            {
                await HandleRateLimitAsync("POST", relPath, resp, attempt);
                continue;
            }

            throw new HttpRequestException(
                $"Kalshi POST {relPath} {(int)resp.StatusCode}: {respBody[..Math.Min(400, respBody.Length)]}",
                inner: null, statusCode: resp.StatusCode);
        }
    }

    // ──────────────────────────────────────────────────────────────────────────
    //  Public API methods
    // ──────────────────────────────────────────────────────────────────────────

    /// <summary>Returns the event JSON including nested markets.</summary>
    public async Task<JsonDocument> GetEventAsync(string eventTicker)
        => await GetAsync($"/events/{eventTicker}?with_nested_markets=true");

    /// <summary>Returns market metadata including top-of-book convenience price fields.</summary>
    public async Task<JsonDocument> GetMarketAsync(string ticker)
        => await GetAsync($"/markets/{ticker}");

    /// <summary>Returns the order book for a single market ticker.</summary>
    public async Task<JsonDocument> GetMarketOrderBookAsync(string ticker)
        => await GetAsync($"/markets/{ticker}/orderbook");

    /// <summary>Returns available balance in cents (integer).</summary>
    public async Task<long> GetBalanceCentsAsync()
    {
        using var doc = await GetAsync("/portfolio/balance");
        return doc.RootElement.GetProperty("balance").GetInt64();
    }

    /// <summary>
    /// Returns all open market positions. Positive position = net YES, negative = net NO.
    /// Paginates automatically. Throws on HTTP/parse errors — callers must distinguish
    /// failure from a genuinely empty account (empty list = no positions, exception = bad read).
    /// </summary>
    /// <summary>Balance on ONE exchange shard.
    ///
    /// <para><see cref="GetBalanceCentsAsync"/> returns the ACCOUNT TOTAL, and that is precisely what made
    /// the 2026-08-28 outage invisible: $576 in the account, $0 on the shard where the markets actually
    /// lived, and every order answering 404 user_not_found. Collateral is per shard — check the shard.</para></summary>
    public async Task<double> ShardBalanceAsync(int shard)
    {
        using var doc = await GetAsync("/portfolio/balance");
        if (doc.RootElement.TryGetProperty("balance_breakdown", out var arr)
            && arr.ValueKind == JsonValueKind.Array)
            foreach (var b in arr.EnumerateArray())
                if (b.TryGetProperty("exchange_index", out var xi) && xi.ValueKind == JsonValueKind.Number
                    && xi.GetInt32() == shard && b.TryGetProperty("balance", out var bal))
                {
                    string? txt = bal.ValueKind == JsonValueKind.String ? bal.GetString() : bal.GetRawText();
                    return double.TryParse(txt, NumberStyles.Any, CultureInfo.InvariantCulture, out double v) ? v : 0;
                }
        return 0;
    }

    public async Task<List<(string Ticker, int Position)>> GetPositionsAsync()
    {
        var result = new List<(string, int)>();
        string cursor = "";
        while (true)
        {
            string path = cursor == ""
                ? "/portfolio/positions?limit=200"
                : $"/portfolio/positions?limit=200&cursor={Uri.EscapeDataString(cursor)}";

            using var doc = await GetAsync(path);   // throws on HTTP error — callers distinguish failure from empty
            var root = doc.RootElement;

            if (root.TryGetProperty("market_positions", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var el in arr.EnumerateArray())
                {
                    string ticker = el.TryGetProperty("ticker", out var t) ? t.GetString() ?? "" : "";
                    int pos = ReadIntFlexible(el, "position_fp");
                    if (!string.IsNullOrEmpty(ticker) && pos != 0) result.Add((ticker, pos));
                }
            }

            if (root.TryGetProperty("cursor", out var cEl) && cEl.ValueKind == JsonValueKind.String &&
                !string.IsNullOrEmpty(cEl.GetString()))
                cursor = cEl.GetString()!;
            else break;

            await Task.Delay(200);
        }
        return result;
    }

    private static int ReadIntFlexible(JsonElement el, string prop)
    {
        if (!el.TryGetProperty(prop, out var v)) return 0;
        return v.ValueKind switch
        {
            JsonValueKind.Number => v.TryGetInt32(out var i) ? i : (int)Math.Round(v.GetDouble()),
            JsonValueKind.String =>
                int.TryParse(v.GetString(), NumberStyles.Any, CultureInfo.InvariantCulture, out var s) ? s :
                decimal.TryParse(v.GetString(), NumberStyles.Any, CultureInfo.InvariantCulture, out var d) ? (int)Math.Round(d) : 0,
            _ => 0
        };
    }

    private static decimal ReadDecimalFlexible(JsonElement el, string prop)
    {
        if (!el.TryGetProperty(prop, out var v)) return 0m;
        return v.ValueKind switch
        {
            JsonValueKind.Number => v.TryGetDecimal(out var d) ? d : (decimal)v.GetDouble(),
            JsonValueKind.String => decimal.TryParse(v.GetString(), NumberStyles.Any,
                                                     CultureInfo.InvariantCulture, out var s) ? s : 0m,
            _ => 0m
        };
    }

    /// <summary>V2 create-order path. The legacy POST /portfolio/orders now returns HTTP 410
    /// (<c>deprecated_v1_order_endpoint</c>) — observed live 2026-08-11, when EVERY Kalshi leg failed and
    /// left the already-filled book leg naked. GET /portfolio/orders/{id} is NOT deprecated and still
    /// carries the authoritative status, so only order CREATION moved.</summary>
    // V2 CREATE-ORDER. Confirmed against Kalshi's own docs 2026-08-27
    // (docs.kalshi.com/api-reference/orders/create-order-v2): this path and this body shape are correct,
    // and the older /portfolio/orders now answers 410 deprecated_v1_order_endpoint.
    private const string V2OrdersPath = "/portfolio/events/orders";

    /// <summary>Fallback exchange when a market's own index cannot be read. 0 = "Default".</summary>
    private static readonly int ExchangeIndexFallback =
        int.TryParse(Environment.GetEnvironmentVariable("KALSHI_EXCHANGE_INDEX"), out var xi) && xi >= 0 ? xi : 0;

    /// <summary>Ticker -> exchange shard, resolved once from the market itself.
    ///
    /// <para><b>Kalshi shards its exchange and an order must name the shard the market lives on.</b>
    /// Observed 2026-08-28: every tennis market reports <c>exchange_index: 3</c> ("Tennis &amp; Baseball"),
    /// while index 0 is "Default" — so posting a tennis order against 0 answers
    /// <c>404 market_not_found</c>, which reads as a bad ticker rather than a routing mistake. A hardcoded
    /// index is therefore wrong for any account trading more than one category, and the market object is
    /// the only authority on which one is right.</para>
    ///
    /// <para>Cached because it is a property of the market, not of the moment.</para></summary>
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, int> _shard = new(StringComparer.Ordinal);

    /// <summary>Ticker -> the SERIES fee multiplier M, read from the venue and remembered.
    ///
    /// <para><b>M is per series and Kalshi publishes it as a live field</b>
    /// (<c>GET /series/{ticker}.fee_multiplier</c>), so nothing should assume 1. The published schedule
    /// carries a "Non-Standard Fees" table where some series sit at 0 (free) and a combos series at 2, and
    /// that table is dated — it can change under a running bot. Since the fee is subtracted from a 1-2c
    /// edge, a silently doubled M would turn every signal in that series into a loss while the telemetry
    /// went on reporting a profit.</para>
    ///
    /// <para>All six tennis series read 1 on 2026-08-28. This exists so that stays a measurement rather
    /// than an assumption.</para></summary>
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, double> _feeMult = new(StringComparer.Ordinal);

    /// <summary>Fee shapes whose arithmetic we actually implement. `quadratic` is
    /// <c>M x rate x C x P x (1-P)</c>; the maker variant differs only in the MAKER leg, which an IOC
    /// taker never pays. Anything else means Kalshi has introduced a shape we do not compute, and the
    /// multiplier alone will not save us — hence the loud warning rather than a silent default.</summary>
    private static readonly HashSet<string> KnownFeeTypes =
        new(StringComparer.OrdinalIgnoreCase) { "quadratic", "quadratic_with_maker_fees" };

    /// <summary>Set when a series reports a fee_type this client does not implement. Read it after
    /// priming: the arithmetic downstream is only valid while this stays empty.</summary>
    public IReadOnlyCollection<string> UnknownFeeTypes => _unknownFeeTypes;
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, byte> _unknownSet = new(StringComparer.Ordinal);
    private List<string> _unknownFeeTypes => _unknownSet.Keys.ToList();

    public async Task<double> FeeMultiplierForAsync(string tickerOrSeries)
    {
        string series = tickerOrSeries.Split('-')[0];
        if (_feeMult.TryGetValue(series, out double known)) return known;
        double m = 1.0;
        try
        {
            using var doc = await GetAsync($"/series/{series}");
            var root = doc.RootElement.TryGetProperty("series", out var se) ? se : doc.RootElement;
            if (root.TryGetProperty("fee_multiplier", out var fm) && fm.ValueKind == JsonValueKind.Number)
                m = fm.GetDouble();
            // THE MULTIPLIER IS ONLY HALF THE CONTRACT. It scales a formula whose SHAPE we hardcode, so a
            // series that switched to a different fee_type would be mispriced no matter what M said.
            string ft = root.TryGetProperty("fee_type", out var fe) && fe.ValueKind == JsonValueKind.String
                      ? (fe.GetString() ?? "") : "";
            if (ft.Length > 0 && !KnownFeeTypes.Contains(ft))
                _unknownSet[$"{series}={ft}"] = 0;
        }
        catch { /* default 1 — the published default, and the safe direction is not to under-charge */ }
        _feeMult[series] = m;
        return m;
    }

    /// <summary>Record a market's shard from a response the caller already had.
    ///
    /// <para><b>This is the latency fix.</b> Kalshi auto-routes when exchange_index is omitted and warns
    /// that "automatic routing will incur an additional latency cost"; naming the shard explicitly avoids
    /// it (and bills only that shard's write budget instead of every shard's). But resolving the shard with
    /// our OWN extra GET just moves the cost onto the first order for each market. The evaluator already
    /// fetches the market to price it, and that response carries exchange_index — so feeding it here makes
    /// the order path both explicitly routed AND free of any lookup.</para></summary>
    /// <summary>Raised on every FILL with what the venue actually charged, so a caller can reconcile it
    /// against whatever fee it modelled. (ticker, contractsFilled, avgFillPrice, feePaidPerContract).
    ///
    /// <para><b>Why this exists.</b> We read the per-series multiplier live, but the RATE (0.07), the
    /// quadratic SHAPE and the centicent ROUNDING are all still hardcoded from a dated PDF. No API field
    /// exposes them. The venue does, however, report <c>average_fee_paid</c> on every fill — so comparing
    /// that against the model turns all three assumptions into a monitored invariant instead of a belief.
    /// A silent fee change is otherwise invisible until it has eaten a month of a 1-2c edge.</para></summary>
    public Action<string, string, decimal, decimal, decimal>? FeeObserved { get; set; }

    /// <summary>The multiplier already resolved for this series, or 1 if it has not been read yet.
    /// Synchronous on purpose: the fee-reconciliation callback runs on the order path and must not await.</summary>
    public double CachedFeeMultiplier(string tickerOrSeries)
        => _feeMult.TryGetValue(tickerOrSeries.Split('-')[0], out double m) ? m : 1.0;

    public void NoteExchangeIndex(string ticker, int exchangeIndex)
    {
        if (exchangeIndex >= 0) _shard[ticker] = exchangeIndex;
    }

    /// <summary>The shard this market trades on, read from the market and remembered. Falls back rather
    /// than throwing: a failed lookup should degrade to the old behaviour, not stop trading.</summary>
    public async Task<int> ExchangeIndexForAsync(string ticker)
    {
        if (_shard.TryGetValue(ticker, out int known)) return known;
        int idx = ExchangeIndexFallback;
        try
        {
            using var doc = await GetMarketAsync(ticker);
            var mkt = doc.RootElement.TryGetProperty("market", out var mm) ? mm : doc.RootElement;
            if (mkt.TryGetProperty("exchange_index", out var xe) && xe.ValueKind == JsonValueKind.Number)
                idx = xe.GetInt32();
        }
        catch { /* fall back; the order will report the real error if the shard is wrong */ }
        _shard[ticker] = idx;
        return idx;
    }

    /// <summary>
    /// Maps our (side, action) pair onto Kalshi V2's single YES-quoted book.
    /// V2 dropped the yes/no + buy/sell model: it exposes only <c>bid</c> (buy YES) and <c>ask</c> (sell
    /// YES), so a NO order is the OPPOSITE action on YES at the COMPLEMENT price —
    /// buy NO @ p  ==  sell YES @ (1 − p),  sell NO @ p  ==  buy YES @ (1 − p).
    ///
    /// This is a pure function purely so it can be unit-tested: inverting it would place a real order on
    /// the WRONG SIDE of the market at a plausible-looking price, which no downstream check would catch —
    /// the fill would simply come back as a directional bet against the position we meant to hold.
    ///
    /// Limit direction survives the transform: a bid fills at or below its price and an ask at or above,
    /// so "pay ≤ 41¢ for NO" becomes "receive ≥ 59¢ for YES", the same constraint.
    /// </summary>
    internal static (string BookSide, decimal YesPrice) MapToV2Book(string side, int priceCents, string action)
    {
        bool isYes = string.Equals(side, "yes", StringComparison.OrdinalIgnoreCase);
        bool isBuy = string.Equals(action, "buy", StringComparison.OrdinalIgnoreCase);
        // buy YES and sell NO both LIFT the yes book (bid); sell YES and buy NO both HIT it (ask).
        string bookSide = isBuy == isYes ? "bid" : "ask";
        int    yesCents = isYes ? priceCents : 100 - priceCents;
        return (bookSide, yesCents / 100m);
    }

    /// <summary>
    /// Places an IOC order on Kalshi via the V2 endpoint. Returns (orderId, status, fillCount).
    /// side = "yes" | "no", action = "buy" | "sell", priceCents = price of THAT side in cents
    /// (e.g. 65 for $0.65) — the caller-facing contract is unchanged; the V2 translation is internal.
    /// clientOrderId tags the order for idempotency / self-trade prevention.
    /// </summary>
    public async Task<(string OrderId, string Status, decimal FillCount, decimal AvgFillPrice)> PlaceOrderAsync(
        string ticker, string side, int priceCents, int count,
        string action = "buy", string? clientOrderId = null)
    {
        // Resolved here rather than plumbed in from every caller: the signature is fixed by
        // IKalshiOrderExecutor (two simulated clients implement it too), and doing it inside means every
        // consumer of this client is correct without touching any of them. Cached per ticker, so it costs
        // one extra GET on the first order for a market and nothing thereafter.
        int shard = await ExchangeIndexForAsync(ticker);
        var (bookSide, yesPrice) = MapToV2Book(side, priceCents, action);
        var body = new Dictionary<string, object>
        {
            ["ticker"]        = ticker,
            ["side"]          = bookSide,
            // V2 takes count and price as fixed-point STRINGS, not numbers. Prices are DOLLARS (V1 was cents).
            ["count"]         = count.ToString(CultureInfo.InvariantCulture),
            ["price"]         = yesPrice.ToString("0.00##", CultureInfo.InvariantCulture),
            ["time_in_force"] = "immediate_or_cancel",
            // Required by V2. taker_at_cross cancels OUR taker order if it would cross our own resting
            // order — the right choice for an IOC taker: never trade with ourselves, never rest.
            ["self_trade_prevention_type"] = "taker_at_cross",
            // THE FIELD THAT WAS ACTUALLY MISSING, and the docs list it as OPTIONAL. Without it every
            // POST answered 404 {"code":"user_not_found"} while the SAME credentials returned 200 on
            // /portfolio/balance, /positions and /orders - so the account was fine and the order endpoint
            // simply could not resolve a user. Adding it changes the error to market_not_found, i.e. the
            // user now resolves and only the ticker is being judged. Proven 2026-08-27 by direct probe.
            // THE SHARD THE MARKET ACTUALLY LIVES ON, not a constant. Tennis is index 3.
            ["exchange_index"] = shard,
        };
        if (!string.IsNullOrEmpty(clientOrderId))
            body["client_order_id"] = clientOrderId;

        using var doc = await PostAsync(V2OrdersPath, body);
        var root = doc.RootElement;

        string  orderId = root.TryGetProperty("order_id", out var id) ? (id.GetString() ?? "") : "";
        decimal fill    = ReadDecimalFlexible(root, "fill_count");
        // WHAT WE ACTUALLY PAID. An IOC limit fills at the best available price up to the limit, so the
        // limit is an upper bound, NOT the price. Without reading this back the caller can only assume it
        // got the ask it screened — and if the book moved and it filled a cent worse, the reported P&L is
        // silently too good by that cent (on a 1-2c arb, most of the edge). V2 reports DOLLARS as a string,
        // which ReadDecimalFlexible already handles. 0 = absent (no fill, or an older payload) — the caller
        // must then fall back rather than treat it as a free trade.
        // ...IN THE CALLER'S DENOMINATION, WHICH IS NOT THE ONE THE VENUE ANSWERS IN. V2 is a YES-book:
        // the request above was translated to a yes price, and `average_fill_price` comes back on that same
        // yes scale. A buy-NO leg therefore reads back `1 - what we paid`, and returning it raw made the
        // caller price a NO fill at its complement.
        //
        // OBSERVED LIVE 2026-08-19 (bet 2258987331): screened NO ask 0.4800, order sent NO 49c, venue
        // answered average_fill_price 0.52. The executor logged "actually paid 0.5200 (+4.00c/share)" and
        // declared the arb eaten by slippage — but 0.52 is ABOVE the 0.49 limit, which an IOC cannot do.
        // 1 - 0.52 = 0.48 = exactly the screened ask: the fill was clean and the slippage was arithmetic.
        //
        // The simulator returns `priceCents / 100m` — already the caller's side — so sim and live disagreed
        // and no dry run could ever surface this. Converting HERE keeps the one contract both honour:
        // priceCents goes in as the price of `side`, AvgFillPrice comes back as the price of `side`.
        // Guard on > 0: 0 means absent, and 1 - 0 would report a NO fill at 1.00.
        decimal avgFillYes = ReadDecimalFlexible(root, "average_fill_price");
        decimal avgFill    = avgFillYes <= 0m ? 0m
                           : (string.Equals(side, "yes", StringComparison.OrdinalIgnoreCase)
                                 ? avgFillYes
                                 : 1m - avgFillYes);
        // V2 returns NO status field (only fill_count / remaining_count). Claim "executed" only on a full
        // immediate fill; anything else reports "resting" so the caller falls through to its GET poll,
        // which is still the authoritative source. Costs one poll on a partial/no fill, and guessing
        // "canceled" here would report a fill count we never confirmed.
        string status = fill >= count ? "executed" : "resting";

        if (fill > 0 && FeeObserved is not null)
        {
            decimal feePaid = ReadDecimalFlexible(root, "average_fee_paid");
            try { FeeObserved(clientOrderId ?? "", ticker, fill, avgFillYes, feePaid); } catch { }
        }
        return (orderId, status, fill, avgFill);
    }

    /// <summary>
    /// Polls GET /portfolio/orders/{orderId} once and returns (status, fill_count_fp).
    ///
    /// A 404 here means "not visible YET", not "does not exist": order creation and order lookup are
    /// eventually consistent. Observed live 2026-08-11 — a sell placed at 13:01:39.526 answered 404 to a
    /// poll ~112ms later, yet had `status=executed, fill=5.00` moments afterwards and the position was
    /// genuinely flat. Returning "pending" lets the caller's poll loop keep trying until its fill timeout.
    ///
    /// This is deliberately NOT allowed to throw. The caller wraps its whole placement in
    /// `catch (HttpRequestException) when (StatusCode == NotFound)`, whose handler reads a 404 as "market
    /// delisted" and PERMANENTLY BLOCKLISTS the ticker — so a transient lookup race would have blocklisted a
    /// live market AND reported a filled order as a failed leg, sending the executor into recovery against
    /// a hedge it already had.
    /// </summary>
    public async Task<(string Status, decimal FillCount)> PollOrderAsync(string orderId)
    {
        JsonDocument doc;
        try
        {
            doc = await GetAsync($"/portfolio/orders/{orderId}");
        }
        catch (HttpRequestException ex) when (ex.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            return ("pending", 0m);
        }
        using (doc)
        {
            var order = doc.RootElement.TryGetProperty("order", out var o) ? o : doc.RootElement;

            string  status = order.TryGetProperty("status",        out var st) ? (st.GetString() ?? "") : "";
            decimal fill   = order.TryGetProperty("fill_count_fp", out var fc)
                ? decimal.Parse(fc.GetString() ?? "0", CultureInfo.InvariantCulture) : 0m;

            return (status, fill);
        }
    }

    /// <summary>
    /// Returns open events with their nested markets in one call.
    /// Paginates automatically until all events are fetched.
    /// </summary>
    public async Task<List<JsonElement>> GetOpenEventsWithMarketsAsync()
    {
        var results = new List<JsonElement>();
        string cursor = "";

        while (true)
        {
            string path = cursor == ""
                ? "/events?status=open&limit=200&with_nested_markets=true"
                : $"/events?status=open&limit=200&with_nested_markets=true&cursor={Uri.EscapeDataString(cursor)}";

            using var doc = await GetAsync(path);
            var root = doc.RootElement;

            if (root.TryGetProperty("events", out var eventsEl))
            {
                foreach (var ev in eventsEl.EnumerateArray())
                    results.Add(ev.Clone());
            }

            // Check for next page
            if (root.TryGetProperty("cursor", out var cursorEl) &&
                cursorEl.ValueKind == JsonValueKind.String &&
                !string.IsNullOrEmpty(cursorEl.GetString()))
            {
                cursor = cursorEl.GetString()!;
            }
            else
            {
                break;
            }

            await Task.Delay(200); // Polite rate limiting between pages
        }

        return results;
    }

    // ──────────────────────────────────────────────────────────────────────────
    //  Private helpers
    // ──────────────────────────────────────────────────────────────────────────

    private static RSA LoadPrivateKey(string keyPath)
    {
        if (string.IsNullOrEmpty(keyPath))
            throw new InvalidOperationException("KALSHI_PRIVATE_KEY_PATH is not set.");

        string pem = File.ReadAllText(keyPath);
        var rsa = RSA.Create();
        rsa.ImportFromPem(pem);
        return rsa;
    }

    public void Dispose()
    {
        _rsa.Dispose();
        _http.Dispose();
    }
}
