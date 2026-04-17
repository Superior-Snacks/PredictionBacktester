using System.Buffers;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;

namespace KalshiPolyCross;

/// <summary>
/// Manages the Polymarket WebSocket connection lifecycle: connect, subscribe,
/// maintain the keep-alive ping, receive price_change deltas, and reconnect
/// on error. Message-processing logic that previously lived as a static method
/// in Program.cs is encapsulated here.
/// </summary>
public class PolymarketWebsocketFeed
{
    private readonly string  _wsUrl;
    private readonly List<string> _tokens;
    private readonly MarketStateTracker _state;
    private readonly CrossPlatformArbTelemetryStrategy _telemetry;
    private readonly int _batchSize;
    private readonly int _pingIntervalMs;

    public PolymarketWebsocketFeed(
        string   wsUrl,
        List<string> tokens,
        MarketStateTracker state,
        CrossPlatformArbTelemetryStrategy telemetry,
        int batchSize,
        int pingIntervalMs)
    {
        _wsUrl          = wsUrl;
        _tokens         = tokens;
        _state          = state;
        _telemetry      = telemetry;
        _batchSize      = batchSize;
        _pingIntervalMs = pingIntervalMs;
    }

    public async Task RunAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                using var ws = new ClientWebSocket();
                await ws.ConnectAsync(new Uri(_wsUrl), ct);
                Console.WriteLine($"[POLY WS] Connected to {_wsUrl}");

                // Keep-alive: Polymarket drops the connection without a PING every ~10s
                var pingSrc  = CancellationTokenSource.CreateLinkedTokenSource(ct);
                var pingBytes = Encoding.UTF8.GetBytes("PING");
                _ = Task.Run(async () =>
                {
                    while (!pingSrc.Token.IsCancellationRequested && ws.State == WebSocketState.Open)
                    {
                        try
                        {
                            await Task.Delay(_pingIntervalMs, pingSrc.Token);
                            await ws.SendAsync(new ArraySegment<byte>(pingBytes), WebSocketMessageType.Text, true, pingSrc.Token);
                        }
                        catch { break; }
                    }
                });

                // Subscribe to all YES and NO tokens in batches
                bool isFirst = true;
                for (int i = 0; i < _tokens.Count; i += _batchSize)
                {
                    var batch = _tokens.Skip(i).Take(_batchSize);
                    string assetList = string.Join("\",\"", batch);
                    string subMsg = isFirst
                        ? $"{{\"assets_ids\":[\"{assetList}\"],\"type\":\"market\"}}"
                        : $"{{\"assets_ids\":[\"{assetList}\"],\"operation\":\"subscribe\"}}";
                    isFirst = false;
                    await ws.SendAsync(Encoding.UTF8.GetBytes(subMsg), WebSocketMessageType.Text, true, ct);
                    await Task.Delay(100, ct);
                }
                Console.WriteLine($"[POLY WS] Subscribed to {_tokens.Count} tokens");

                // Clear books on reconnect, then notify telemetry (closes open windows)
                foreach (var token in _tokens)
                    _state.ClearPolyToken(token);
                _telemetry.OnPolyReconnect();

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
                        if (message is "PONG" or "pong") continue;

                        ProcessMessage(message);
                    }
                }
                finally
                {
                    ArrayPool<byte>.Shared.Return(buf);
                    pingSrc.Cancel();
                }
                reconnect:
                pingSrc.Cancel();
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                Console.WriteLine($"[POLY WS ERROR] {ex.Message} — reconnecting in 5s...");
            }

            if (!ct.IsCancellationRequested)
                await Task.Delay(5_000, ct).ContinueWith(_ => { });
        }
    }

    // ── Message processing ────────────────────────────────────────────────────

    private void ProcessMessage(string message)
    {
        try
        {
            using var doc = JsonDocument.Parse(message);
            var root = doc.RootElement;
            if (!root.TryGetProperty("event_type", out var etEl)) return;
            string eventType = etEl.GetString() ?? "";

            if (eventType == "book" && root.TryGetProperty("asset_id", out var idEl))
            {
                string assetId = idEl.GetString() ?? "";
                string bookKey = $"P:{assetId}";
                if (!_state.Books.TryGetValue(bookKey, out var book)) return;
                if (root.TryGetProperty("bids", out var bidsEl) && root.TryGetProperty("asks", out var asksEl))
                    book.ProcessBookUpdate(bidsEl, asksEl);
                // Do NOT call telemetry here: snapshot alone is not live data
            }
            else if (eventType == "price_change" && root.TryGetProperty("price_changes", out var changesEl))
            {
                foreach (var change in changesEl.EnumerateArray())
                {
                    if (!change.TryGetProperty("asset_id", out var assetIdEl)) continue;
                    string assetId = assetIdEl.GetString() ?? "";
                    string bookKey = $"P:{assetId}";
                    if (!_state.Books.TryGetValue(bookKey, out var book)) continue;

                    if (!decimal.TryParse(change.GetProperty("price").GetString(),
                            System.Globalization.NumberStyles.Any,
                            System.Globalization.CultureInfo.InvariantCulture, out decimal price)) continue;
                    if (!decimal.TryParse(change.GetProperty("size").GetString(),
                            System.Globalization.NumberStyles.Any,
                            System.Globalization.CultureInfo.InvariantCulture, out decimal size)) continue;
                    string side = change.GetProperty("side").GetString() ?? "";

                    book.UpdatePriceLevel(side, price, size);
                    book.MarkDeltaReceived();
                    _telemetry.OnBookUpdate(bookKey);
                }
            }
        }
        catch (JsonException) { }
    }
}
