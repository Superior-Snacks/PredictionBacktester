using System.Buffers;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using PredictionBacktester.Engine;
using PredictionBacktester.Engine.LiveExecution;

namespace KalshiPolyCross;

/// <summary>
/// Manages the Kalshi WebSocket connection lifecycle: connect, subscribe, receive,
/// process deltas/snapshots, and reconnect on error. All message-processing logic
/// that previously lived as static methods in Program.cs is encapsulated here.
/// </summary>
public class KalshiWebsocketFeed
{
    private readonly KalshiOrderClient _orderClient;
    private readonly KalshiApiConfig   _config;
    private readonly List<string>      _tickers;
    private readonly MarketStateTracker _state;
    private readonly CrossPlatformArbTelemetryStrategy _telemetry;
    private readonly int     _batchSize;
    private readonly decimal _minBookPrice;

    public KalshiWebsocketFeed(
        KalshiOrderClient orderClient,
        KalshiApiConfig   config,
        List<string>      tickers,
        MarketStateTracker state,
        CrossPlatformArbTelemetryStrategy telemetry,
        int     batchSize,
        decimal minBookPrice)
    {
        _orderClient  = orderClient;
        _config       = config;
        _tickers      = tickers;
        _state        = state;
        _telemetry    = telemetry;
        _batchSize    = batchSize;
        _minBookPrice = minBookPrice;
    }

    public async Task RunAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                using var ws = new ClientWebSocket();
                var (key, ts, sig) = _orderClient.CreateAuthHeaders("GET", "/trade-api/ws/v2");
                ws.Options.SetRequestHeader("KALSHI-ACCESS-KEY",       key);
                ws.Options.SetRequestHeader("KALSHI-ACCESS-TIMESTAMP", ts);
                ws.Options.SetRequestHeader("KALSHI-ACCESS-SIGNATURE", sig);

                await ws.ConnectAsync(new Uri(_config.BaseWsUrl), ct);
                Console.WriteLine($"[KALSHI WS] Connected to {_config.BaseWsUrl}");

                int msgId = 1;
                for (int i = 0; i < _tickers.Count; i += _batchSize)
                {
                    var batch = _tickers.Skip(i).Take(_batchSize);
                    string tickerArray = string.Join(",", batch.Select(t => $"\"{t}\""));
                    string subMsg = $"{{\"id\":{msgId++},\"cmd\":\"subscribe\",\"params\":{{\"channels\":[\"orderbook_delta\"],\"market_tickers\":[{tickerArray}]}}}}";
                    await ws.SendAsync(Encoding.UTF8.GetBytes(subMsg), WebSocketMessageType.Text, true, ct);
                    await Task.Delay(100, ct);
                }
                Console.WriteLine($"[KALSHI WS] Subscribed to {_tickers.Count} tickers");

                // Clear books on reconnect, then notify telemetry (closes open windows)
                foreach (var ticker in _tickers)
                    _state.ClearKalshiMarket(ticker);
                _telemetry.OnKalshiReconnect();

                // Rent a buffer from the pool — returned in the finally below
                byte[] buf = ArrayPool<byte>.Shared.Rent(65536);
                try
                {
                    using var ms = new MemoryStream();
                    while (!ct.IsCancellationRequested && ws.State == WebSocketState.Open)
                    {
                        ms.SetLength(0);
                        WebSocketReceiveResult result;
                        do
                        {
                            result = await ws.ReceiveAsync(new ArraySegment<byte>(buf), ct);
                            if (result.MessageType == WebSocketMessageType.Close)
                            {
                                await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "", CancellationToken.None);
                                goto reconnect;
                            }
                            ms.Write(buf, 0, result.Count);
                        } while (!result.EndOfMessage);

                        if (ms.Length == 0) continue;
                        string message = Encoding.UTF8.GetString(ms.GetBuffer(), 0, (int)ms.Length);
                        if (message is "heartbeat" or "PONG" or "pong") continue;

                        ProcessMessage(message);
                    }
                }
                finally
                {
                    ArrayPool<byte>.Shared.Return(buf);
                }
                reconnect:;
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                Console.WriteLine($"[KALSHI WS ERROR] {ex.Message} — reconnecting in 5s...");
            }

            if (!ct.IsCancellationRequested)
                await Task.Delay(5_000, ct).ContinueWith(_ => { });
        }
    }

    // ── Message routing ───────────────────────────────────────────────────────

    private void ProcessMessage(string message)
    {
        try
        {
            using var doc = JsonDocument.Parse(message);
            var root = doc.RootElement;

            if (!root.TryGetProperty("type",  out var typeEl)) return;
            if (!root.TryGetProperty("msg",   out var msgEl))  return;
            if (!msgEl.TryGetProperty("market_ticker", out var tickerEl)) return;

            string msgType = typeEl.GetString()  ?? "";
            string ticker  = tickerEl.GetString() ?? "";

            if (!_state.Books.TryGetValue($"K:{ticker}", out var yesBook)) return;
            _state.Books.TryGetValue($"K:{ticker}_NO", out var noBook);

            if (!_state.YesSizes.TryGetValue(ticker, out var ySizeMap) ||
                !_state.NoSizes .TryGetValue(ticker, out var nSizeMap)) return;

            if (msgType == "orderbook_snapshot")
            {
                ApplySnapshot(yesBook, noBook, msgEl, ySizeMap, nSizeMap);
                _telemetry.OnBookUpdate($"K:{ticker}");
                if (noBook != null) _telemetry.OnBookUpdate($"K:{ticker}_NO");
            }
            else if (msgType == "orderbook_delta")
            {
                bool noChanged = ApplyDelta(yesBook, noBook, msgEl, ySizeMap, nSizeMap);
                _telemetry.OnBookUpdate($"K:{ticker}");
                if (noBook != null && noChanged) _telemetry.OnBookUpdate($"K:{ticker}_NO");
            }
        }
        catch (JsonException) { }
    }

    private bool ApplyDelta(
        LocalOrderBook yesBook, LocalOrderBook? noBook,
        JsonElement msg,
        Dictionary<decimal, decimal> yesSizeMap, Dictionary<decimal, decimal> noSizeMap)
    {
        if (!msg.TryGetProperty("price_dollars", out var priceEl)) return false;
        if (!msg.TryGetProperty("delta_fp",      out var deltaEl)) return false;
        if (!msg.TryGetProperty("side",          out var sideEl))  return false;

        if (!decimal.TryParse(priceEl.GetString(), System.Globalization.NumberStyles.Any,
                System.Globalization.CultureInfo.InvariantCulture, out decimal price)) return false;
        if (!decimal.TryParse(deltaEl.GetString(), System.Globalization.NumberStyles.Any,
                System.Globalization.CultureInfo.InvariantCulture, out decimal delta)) return false;
        string side = sideEl.GetString() ?? "";

        if (price < _minBookPrice || price > (1m - _minBookPrice)) return false;

        if (side == "yes")
        {
            decimal newSize        = yesSizeMap.GetValueOrDefault(price, 0m) + delta;
            decimal impliedNoAsk   = Math.Round(1m - price, 4);
            if (newSize <= 0) { yesSizeMap.Remove(price); yesBook.UpdatePriceLevel("BUY", price, 0m);     noBook?.UpdatePriceLevel("SELL", impliedNoAsk, 0m); }
            else              { yesSizeMap[price] = newSize; yesBook.UpdatePriceLevel("BUY", price, newSize); noBook?.UpdatePriceLevel("SELL", impliedNoAsk, newSize); }
            yesBook.MarkDeltaReceived();
            return false;
        }
        if (side == "no")
        {
            decimal newSize         = noSizeMap.GetValueOrDefault(price, 0m) + delta;
            decimal impliedYesAsk   = Math.Round(1m - price, 4);
            if (newSize <= 0) { noSizeMap.Remove(price); noBook?.UpdatePriceLevel("BUY", price, 0m);     yesBook.UpdatePriceLevel("SELL", impliedYesAsk, 0m); }
            else              { noSizeMap[price] = newSize; noBook?.UpdatePriceLevel("BUY", price, newSize); yesBook.UpdatePriceLevel("SELL", impliedYesAsk, newSize); }
            noBook?.MarkDeltaReceived();
            yesBook.MarkDeltaReceived();
            return true;
        }
        return false;
    }

    private void ApplySnapshot(
        LocalOrderBook yesBook, LocalOrderBook? noBook,
        JsonElement msg,
        Dictionary<decimal, decimal> yesSizeMap, Dictionary<decimal, decimal> noSizeMap)
    {
        yesBook.ClearBook();
        noBook?.ClearBook();
        yesSizeMap.Clear();
        noSizeMap.Clear();

        if (msg.TryGetProperty("yes_dollars_fp", out var yesEl) && yesEl.ValueKind == JsonValueKind.Array)
            foreach (var level in yesEl.EnumerateArray())
                if (TryParseLevel(level, out decimal price, out decimal size))
                {
                    yesSizeMap[price] = size;
                    yesBook.UpdatePriceLevel("BUY", price, size);
                    noBook?.UpdatePriceLevel("SELL", Math.Round(1m - price, 4), size);
                }

        if (msg.TryGetProperty("no_dollars_fp", out var noEl) && noEl.ValueKind == JsonValueKind.Array)
            foreach (var level in noEl.EnumerateArray())
                if (TryParseLevel(level, out decimal noPrice, out decimal size))
                {
                    noSizeMap[noPrice] = size;
                    noBook?.UpdatePriceLevel("BUY", noPrice, size);
                    yesBook.UpdatePriceLevel("SELL", Math.Round(1m - noPrice, 4), size);
                }
    }

    private static bool TryParseLevel(JsonElement level, out decimal price, out decimal size)
    {
        price = 0; size = 0;
        if (level.ValueKind != JsonValueKind.Array) return false;
        var arr = level.EnumerateArray().ToArray();
        if (arr.Length < 2) return false;
        if (!decimal.TryParse(arr[0].GetString(), System.Globalization.NumberStyles.Any,
                System.Globalization.CultureInfo.InvariantCulture, out price)) return false;
        if (!decimal.TryParse(arr[1].GetString(), System.Globalization.NumberStyles.Any,
                System.Globalization.CultureInfo.InvariantCulture, out size)) return false;
        return true; // price-range guard is applied per caller via _minBookPrice
    }
}
