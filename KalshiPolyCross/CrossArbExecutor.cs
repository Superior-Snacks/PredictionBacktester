using System.Collections.Concurrent;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Channels;
using PredictionBacktester.Engine;
using PredictionBacktester.Engine.LiveExecution;

namespace KalshiPolyCross;

public record ArbPosition(
    string   PairId,
    string   ArbType,
    decimal  KalshiContracts,
    decimal  PolyShares,
    decimal  KalshiEntryPrice,
    decimal  PolyEntryPrice,
    DateTime EntryTime
);

/// <summary>
/// Fires simultaneous IOC/FAK orders on both legs when CrossPlatformArbTelemetryStrategy
/// detects a cross-platform arb window. Kalshi leg uses IOC via PlaceOrderAsync;
/// Polymarket leg uses FAK via PolymarketOrderClient (POLY_GNOSIS_SAFE signing).
/// </summary>
public class CrossArbExecutor
{
    private readonly KalshiOrderClient _kalshi;
    private readonly PolymarketOrderClient _poly;
    private readonly CrossPlatformArbTelemetryStrategy _telemetry;
    private readonly ConcurrentDictionary<string, LocalOrderBook> _books;

    // ── Configuration ─────────────────────────────────────────────────────────
    private readonly decimal _maxContracts;
    private readonly decimal _maxExposureUsd;
    private readonly decimal _executionThreshold;
    private readonly int     _pairCooldownSeconds;
    private readonly int     _fillTimeoutMs;
    private readonly bool    _dryRun;

    // ── Runtime state ─────────────────────────────────────────────────────────
    private readonly ConcurrentDictionary<string, long>        _cooldownUntil = new();
    private readonly ConcurrentDictionary<string, ArbPosition> _openPositions = new();
    private          decimal _totalExposure = 0m;
    private readonly object  _exposureLock  = new();

    // ── Fee model (mirrors CrossPlatformArbTelemetryStrategy) ─────────────────
    private static decimal KalshiFee(decimal p) => 0.07m * p * (1m - p);
    private static decimal PolyFee(decimal p)
        => p * 0.04m * (decimal)Math.Pow((double)(p * (1m - p)), 1.0);

    // ── CSV ───────────────────────────────────────────────────────────────────
    private readonly Channel<string> _csvChannel =
        Channel.CreateUnbounded<string>(new UnboundedChannelOptions { SingleReader = true });
    private readonly string _csvPath;
    private bool _headerWritten;

    public int     OpenPositionCount => _openPositions.Count(kv => kv.Value != null);
    public decimal TotalExposure     => _totalExposure;

    public CrossArbExecutor(
        KalshiOrderClient               kalshi,
        PolymarketOrderClient           poly,
        CrossPlatformArbTelemetryStrategy telemetry,
        ConcurrentDictionary<string, LocalOrderBook> books,
        decimal maxContracts        = 1m,
        decimal maxExposureUsd      = 10m,
        decimal executionThreshold  = 0.990m,
        int     pairCooldownSeconds = 120,
        int     fillTimeoutMs       = 5000,
        bool    dryRun              = false)
    {
        _kalshi              = kalshi;
        _poly                = poly;
        _telemetry           = telemetry;
        _books               = books;
        _maxContracts        = maxContracts;
        _maxExposureUsd      = maxExposureUsd;
        _executionThreshold  = executionThreshold;
        _pairCooldownSeconds = pairCooldownSeconds;
        _fillTimeoutMs       = fillTimeoutMs;
        _dryRun              = dryRun;
        _csvPath             = $"CrossArbExecution_{DateTime.UtcNow:yyyyMMdd_HHmmss}.csv";
        _ = Task.Run(RunCsvWriterAsync);
    }

    /// <summary>Wire to telemetry.OnArbOpened — fires on every new WS-detected arb window.</summary>
    public void OnArbOpened(string pairId, decimal netCost, string arbType, decimal depth)
        => _ = Task.Run(() => ExecuteAsync(pairId, arbType));

    // ── Core execution ────────────────────────────────────────────────────────

    private async Task ExecuteAsync(string pairId, string arbType)
    {
        long now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();

        // Guard: cooldown or open position on this pair
        if (_cooldownUntil.TryGetValue(pairId, out long cd) && now < cd) return;
        if (_openPositions.ContainsKey(pairId)) return;

        var pair = _telemetry.GetPair(pairId);
        if (pair == null) return;

        // Re-read live book prices at execution time
        if (!_books.TryGetValue($"K:{pair.KalshiTicker}",    out var kYes)) return;
        if (!_books.TryGetValue($"K:{pair.KalshiTicker}_NO", out var kNo))  return;
        if (!_books.TryGetValue($"P:{pair.PolyYesTokenId}",  out var pYes)) return;
        if (!_books.TryGetValue($"P:{pair.PolyNoTokenId}",   out var pNo))  return;

        decimal kLegAsk, pLegAsk;
        string  kalshiSide, polyToken;

        if (arbType == "K_YES_P_NO")
        {
            kLegAsk    = kYes.GetBestAskPrice();
            pLegAsk    = pNo.GetBestAskPrice();
            kalshiSide = "yes";
            polyToken  = pair.PolyNoTokenId;
        }
        else // K_NO_P_YES
        {
            kLegAsk    = kNo.GetBestAskPrice();
            pLegAsk    = pYes.GetBestAskPrice();
            kalshiSide = "no";
            polyToken  = pair.PolyYesTokenId;
        }

        // Re-validate arb still holds at execution time
        decimal netNow = kLegAsk + pLegAsk + KalshiFee(kLegAsk) + PolyFee(pLegAsk);
        if (netNow >= _executionThreshold)
        {
            Console.WriteLine($"[EXEC SKIP] {pair.Label} | net=${netNow:0.0000} >= threshold {_executionThreshold:0.000}");
            return;
        }

        if (kLegAsk <= 0.02m || pLegAsk <= 0.02m) return;

        int     kPriceCents   = (int)Math.Round(kLegAsk * 100);
        decimal polyShares    = _maxContracts;
        decimal estimatedCost = kLegAsk * _maxContracts + pLegAsk * polyShares;

        if (_dryRun)
        {
            Console.ForegroundColor = ConsoleColor.Yellow;
            Console.WriteLine(
                $"[DRY RUN] {pair.Label} | {arbType} | K-{kalshiSide}={kLegAsk:0.0000} " +
                $"P={pLegAsk:0.0000} net=${netNow:0.0000} | {_maxContracts} contracts est.cost=${estimatedCost:0.00}");
            Console.ResetColor();
            EnqueueCsvRow(pair, arbType, DateTime.UtcNow, kPriceCents, kLegAsk, pLegAsk,
                          0m, 0m, 0m, netNow, "DRY_RUN");
            return;
        }

        // Exposure check (thread-safe)
        lock (_exposureLock)
        {
            if (_totalExposure + estimatedCost > _maxExposureUsd)
            {
                Console.WriteLine(
                    $"[EXEC SKIP] {pair.Label} | Exposure limit " +
                    $"${_totalExposure:0.00}+${estimatedCost:0.00} > ${_maxExposureUsd:0.00}");
                return;
            }
            _totalExposure += estimatedCost;
        }

        // Set cooldown before firing to block concurrent execution on the same pair
        _cooldownUntil[pairId] = now + _pairCooldownSeconds;

        Console.ForegroundColor = ConsoleColor.Cyan;
        Console.WriteLine(
            $"[EXEC] {pair.Label} | {arbType} | K-{kalshiSide}={kLegAsk:0.0000} " +
            $"P={pLegAsk:0.0000} net=${netNow:0.0000} | {_maxContracts} contracts");
        Console.ResetColor();

        var t0 = DateTime.UtcNow;

        // Fire both legs simultaneously
        var kalshiTask = PlaceKalshiLegAsync(pair.KalshiTicker, kalshiSide, kPriceCents, (int)_maxContracts);
        var polyTask   = PlacePolyLegAsync(polyToken, pLegAsk, polyShares);
        await Task.WhenAll(kalshiTask, polyTask);

        var (kOrderId, kStatus, kFilled) = kalshiTask.Result;
        var (pFilled, pActualPrice)       = polyTask.Result;

        bool bothFilled    = kFilled > 0 && pFilled > 0;
        bool neitherFilled = kFilled == 0 && pFilled == 0;

        if (bothFilled)
        {
            decimal actualCost = kLegAsk * kFilled + pActualPrice * pFilled;
            _openPositions[pairId] = new ArbPosition(
                pairId, arbType, kFilled, pFilled, kLegAsk, pActualPrice, t0);
            Console.ForegroundColor = ConsoleColor.Green;
            Console.WriteLine(
                $"[EXEC OK] {pair.Label} | K={kFilled} @ {kPriceCents}¢ | " +
                $"P={pFilled:0.00}sh @ ${pActualPrice:0.0000} | cost=${actualCost:0.00} net=${netNow:0.0000}");
            Console.ResetColor();
        }
        else if (!neitherFilled)
        {
            Console.ForegroundColor = ConsoleColor.Red;
            Console.WriteLine(
                $"[EXEC PARTIAL !!! OPEN POSITION !!!] {pair.Label} | " +
                $"K={kFilled}/{_maxContracts} P={pFilled:0.00}/{polyShares:0.00} k-status={kStatus}");
            Console.ResetColor();
        }
        else
        {
            Console.WriteLine($"[EXEC MISS] {pair.Label} | Neither leg filled. k-status={kStatus}");
        }

        // Release exposure reservation when no position opened
        if (!bothFilled)
            lock (_exposureLock) { _totalExposure -= estimatedCost; }

        EnqueueCsvRow(pair, arbType, t0, kPriceCents, kLegAsk, pLegAsk,
                      kFilled, pFilled, pActualPrice, netNow, kStatus);
    }

    // ── Kalshi IOC leg ────────────────────────────────────────────────────────

    private async Task<(string OrderId, string Status, decimal FillCount)> PlaceKalshiLegAsync(
        string ticker, string side, int priceCents, int count)
    {
        try
        {
            var (orderId, status, fillImm) = await _kalshi.PlaceOrderAsync(ticker, side, priceCents, count);

            if (status == "executed" || fillImm >= count)
                return (orderId, status, fillImm);

            if (string.IsNullOrEmpty(orderId)) return ("", status, 0m);

            // Poll until resolved or fill timeout
            using var cts = new CancellationTokenSource(_fillTimeoutMs);
            while (!cts.Token.IsCancellationRequested)
            {
                await Task.Delay(50).ConfigureAwait(false);
                var (pollStatus, pollFill) = await _kalshi.PollOrderAsync(orderId);
                if (pollStatus == "executed" || pollStatus == "canceled")
                    return (orderId, pollStatus, pollFill);
            }

            return (orderId, "timeout", fillImm);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[KALSHI LEG ERROR] {ticker}: {ex.Message}");
            return ("", "error", 0m);
        }
    }

    // ── Polymarket FAK leg ────────────────────────────────────────────────────
    // Routes through POLY_PROXY_ADDRESS (Gnosis Safe) via PolymarketOrderClient —
    // identical EIP-712 POLY_GNOSIS_SAFE signing as PredictionLiveProduction.

    private async Task<(decimal FilledShares, decimal AvgPrice)> PlacePolyLegAsync(
        string tokenId, decimal price, decimal shares)
    {
        try
        {
            // +1¢ slippage to cross the spread and guarantee the FAK fill (mirrors PolymarketLiveBroker)
            decimal limitPrice = Math.Min(0.99m, price + 0.01m);

            string result = "";
            int feeRate = 0;
            for (int attempt = 0; attempt < 2; attempt++)
            {
                try
                {
                    result = await _poly.SubmitOrderAsync(
                        tokenId, limitPrice, shares, side: 0 /*BUY*/,
                        negRisk: false, feeRateBps: feeRate);
                    break;
                }
                catch (Exception ex) when (
                    ex.Message.Contains("invalid fee rate") &&
                    ex.Message.Contains("taker fee:"))
                {
                    var m = Regex.Match(ex.Message, @"taker fee:\s*(\d+)");
                    if (m.Success && int.TryParse(m.Groups[1].Value, out int fee))
                        feeRate = fee;
                    else
                        throw;
                }
            }

            if (string.IsNullOrEmpty(result)) return (0m, 0m);

            using var doc = JsonDocument.Parse(result);
            var root = doc.RootElement;

            if (!root.TryGetProperty("success", out var sv) || !sv.GetBoolean())
                return (0m, 0m);

            // BUY: takingAmount = shares received, makingAmount = USDC spent
            decimal takingVal = 0m, makingVal = 0m;
            if (root.TryGetProperty("takingAmount", out var ta) || root.TryGetProperty("taking_amount", out ta))
            {
                string? v = ta.ValueKind == JsonValueKind.String ? ta.GetString() : ta.ToString();
                if (v != null) decimal.TryParse(v, out takingVal);
            }
            if (root.TryGetProperty("makingAmount", out var ma) || root.TryGetProperty("making_amount", out ma))
            {
                string? v = ma.ValueKind == JsonValueKind.String ? ma.GetString() : ma.ToString();
                if (v != null) decimal.TryParse(v, out makingVal);
            }

            decimal filledShares = takingVal;
            decimal spentUsdc    = makingVal;
            if (filledShares <= 0) return (0m, 0m);

            decimal avgPrice = spentUsdc > 0 ? spentUsdc / filledShares : price;
            return (filledShares, avgPrice);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[POLY LEG ERROR] {tokenId[..Math.Min(12, tokenId.Length)]}...: {ex.Message}");
            return (0m, 0m);
        }
    }

    // ── CSV ───────────────────────────────────────────────────────────────────

    private void EnqueueCsvRow(CrossPair pair, string arbType, DateTime t,
        int kPriceCents, decimal kLegAsk, decimal pLegAsk,
        decimal kFilled, decimal pFilled, decimal pPrice, decimal netCost, string kStatus)
    {
        if (!_headerWritten)
        {
            _headerWritten = true;
            _csvChannel.Writer.TryWrite(
                "Time,PairId,Label,ArbType,KalshiTicker,PolyToken," +
                "KPriceCents,KLegAsk,PLegAsk,NetCost," +
                "KFilled,PFilled,PAvgPrice,KStatus,DryRun");
        }
        string row = string.Join(",",
            t.ToString("yyyy-MM-dd HH:mm:ss.fff"),
            Quote(pair.PairId),
            Quote(pair.Label),
            arbType,
            pair.KalshiTicker,
            Quote(arbType == "K_YES_P_NO" ? pair.PolyNoTokenId : pair.PolyYesTokenId),
            kPriceCents,
            kLegAsk.ToString("0.0000"),
            pLegAsk.ToString("0.0000"),
            netCost.ToString("0.0000"),
            kFilled.ToString("0.00"),
            pFilled.ToString("0.00"),
            pPrice.ToString("0.0000"),
            kStatus,
            _dryRun ? "1" : "0"
        );
        _csvChannel.Writer.TryWrite(row);
    }

    private static string Quote(string s) => $"\"{s.Replace("\"", "\"\"")}\"";

    private async Task RunCsvWriterAsync()
    {
        try
        {
            using var sw = new StreamWriter(_csvPath, append: false, Encoding.UTF8) { AutoFlush = false };
            await foreach (var line in _csvChannel.Reader.ReadAllAsync())
            {
                await sw.WriteLineAsync(line);
                await sw.FlushAsync();
            }
        }
        catch (Exception ex) { Console.WriteLine($"[EXEC CSV ERROR] {ex.Message}"); }
    }

    public async Task ShutdownAsync()
    {
        _csvChannel.Writer.TryComplete();
        // Allow the writer task to drain
        await Task.Delay(200);
    }
}
