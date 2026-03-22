using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace PredictionBacktester.Engine.LiveExecution
{
    // FIX #3: A strict, clear record instead of loose variables
    public record UserTradeEvent(string TokenId, decimal Price, decimal Size, string Side);

    // FIX #4: IDisposable for clean memory management
    public class PolymarketUserStreamClient : IDisposable
    {
        private ClientWebSocket? _ws;
        private readonly PolymarketApiConfig _config;
        private readonly List<string> _conditionIds;
        private CancellationTokenSource _cts = new();
        private int _messagesReceived;

        /// <summary>Toggle verbose logging to see every raw WS message.</summary>
        public bool DebugMode { get; set; } = true;

        public event Action<UserTradeEvent>? OnTradeMatched;

        public PolymarketUserStreamClient(PolymarketApiConfig config, List<string> conditionIds)
        {
            _config = config;
            _conditionIds = conditionIds;
        }

        public async Task StartAsync()
        {
            _cts = new CancellationTokenSource();
            // Start the infinite reconnection loop in the background
            _ = Task.Run(() => ConnectAndListenAsync(_cts.Token));
        }

        // FIX #1: Reconnection Logic
        private async Task ConnectAndListenAsync(CancellationToken ct)
        {
            while (!ct.IsCancellationRequested)
            {
                try
                {
                    if (_ws != null) _ws.Dispose();
                    _ws = new ClientWebSocket();
                    
                    await _ws.ConnectAsync(new Uri("wss://ws-subscriptions-clob.polymarket.com/ws/user"), ct);
                    Console.WriteLine("[USER STREAM] Connected. Authenticating wallet...");

                    var subMsg = new
                    {
                        auth = new {
                            apiKey = _config.ApiKey,
                            secret = _config.ApiSecret,
                            passphrase = _config.ApiPassphrase
                        },
                        markets = _conditionIds,
                        type = "user"
                    };
                    
                    string subJson = JsonSerializer.Serialize(subMsg);
                    await _ws.SendAsync(Encoding.UTF8.GetBytes(subJson), WebSocketMessageType.Text, true, ct);

                    // Launch the Ping and Listen tasks for this specific connection
                    var pingTask = PingLoopAsync(ct);
                    var listenTask = ListenLoopAsync(ct);

                    // Wait until one of them fails or the socket closes
                    await Task.WhenAny(pingTask, listenTask);
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"[USER STREAM WRN] Connection dropped: {ex.Message}. Reconnecting in 5s...");
                }

                // Wait 5 seconds before trying to reconnect
                await Task.Delay(5000, ct);
            }
        }

        private async Task PingLoopAsync(CancellationToken ct)
        {
            var pingBytes = Encoding.UTF8.GetBytes("PING");
            while (!ct.IsCancellationRequested && _ws?.State == WebSocketState.Open)
            {
                await Task.Delay(10000, ct);
                try
                {
                    await _ws.SendAsync(pingBytes, WebSocketMessageType.Text, true, ct);
                }
                catch { break; } // If send fails, break to trigger reconnect
            }
        }

        private async Task ListenLoopAsync(CancellationToken ct)
        {
            var buffer = new byte[8192];
            
            while (!ct.IsCancellationRequested && _ws?.State == WebSocketState.Open)
            {
                // FIX #2: Dynamic MemoryStream to prevent JSON truncation
                using var ms = new MemoryStream();
                WebSocketReceiveResult result;
                
                do
                {
                    result = await _ws.ReceiveAsync(new ArraySegment<byte>(buffer), ct);
                    if (result.MessageType == WebSocketMessageType.Close) return; // Exit and trigger reconnect
                    
                    ms.Write(buffer, 0, result.Count);
                } 
                while (!result.EndOfMessage); // Keep reading until the frame is completely finished

                if (result.MessageType == WebSocketMessageType.Text)
                {
                    string msg = Encoding.UTF8.GetString(ms.ToArray());
                    if (msg.Trim() == "PONG") continue;

                    _messagesReceived++;
                    if (DebugMode)
                    {
                        // Show first 500 chars of every non-PONG message
                        string preview = msg.Length > 500 ? msg[..500] + "..." : msg;
                        Console.WriteLine($"[USER STREAM DBG] Msg #{_messagesReceived}: {preview}");
                    }

                    ProcessMessage(msg);
                }
            }
        }

        private void ProcessMessage(string msg)
        {
            try
            {
                using var doc = JsonDocument.Parse(msg);
                var root = doc.RootElement;

                // Only process array items or objects that have an "event" property
                if (root.ValueKind == JsonValueKind.Array)
                {
                    foreach (var element in root.EnumerateArray())
                    {
                        ParseSingleEvent(element);
                    }
                }
                else
                {
                    ParseSingleEvent(root);
                }
            }
            catch (Exception ex)
            {
                // We log parse errors but DO NOT crash the thread
                Console.WriteLine($"[USER STREAM PARSE ERROR] {ex.Message}");
            }
        }

        private void ParseSingleEvent(JsonElement element)
        {
            // Log all event types we see
            string eventType = "";
            if (element.TryGetProperty("event", out var dbgEventEl))
            {
                eventType = dbgEventEl.GetString() ?? "";
                if (DebugMode)
                    Console.WriteLine($"[USER STREAM DBG] Event type: '{eventType}'");
            }
            else if (element.TryGetProperty("type", out var typeEl))
            {
                eventType = typeEl.GetString() ?? "";
                if (DebugMode)
                    Console.WriteLine($"[USER STREAM DBG] Type: '{eventType}'");
            }

            // Parse "trade" events — MATCHED/CONFIRMED trade lifecycle
            if (eventType == "trade")
            {
                TryExtractFill(element);
            }
            // Parse "order" events — may contain fill data for our FAK orders
            else if (eventType == "order")
            {
                // Order events may report status changes with matched amounts
                string status = "";
                if (element.TryGetProperty("status", out var statusEl))
                    status = statusEl.GetString() ?? "";

                if (DebugMode)
                    Console.WriteLine($"[USER STREAM DBG] Order status: '{status}'");

                // If the order was matched, extract fill data
                if (status == "MATCHED" || status == "matched" || status == "LIVE" || status == "live")
                {
                    TryExtractFill(element);
                }
            }
        }

        private void TryExtractFill(JsonElement element)
        {
            // Try multiple possible field names for token ID
            string tokenId = "";
            foreach (string field in new[] { "asset_id", "tokenId", "token_id", "market" })
            {
                if (element.TryGetProperty(field, out var el) && !string.IsNullOrEmpty(el.GetString()))
                {
                    tokenId = el.GetString()!;
                    break;
                }
            }

            string side = "";
            if (element.TryGetProperty("side", out var sideEl))
                side = sideEl.GetString() ?? "";

            // Try parsing price/size from multiple possible field names and formats
            decimal price = TryParseDecimalField(element, "price", "match_price", "avgPrice");
            decimal size = TryParseDecimalField(element, "size", "taker_amount_matched", "matched_amount");

            if (!string.IsNullOrEmpty(tokenId) && size > 0)
            {
                if (DebugMode)
                    Console.WriteLine($"[USER STREAM] FILL DETECTED: {side} {size:0.00} @ ${price:0.000} | Token: {tokenId[..Math.Min(12, tokenId.Length)]}...");

                var tradeEvent = new UserTradeEvent(tokenId, price, size, side);
                OnTradeMatched?.Invoke(tradeEvent);
            }
        }

        private static decimal TryParseDecimalField(JsonElement element, params string[] fieldNames)
        {
            foreach (string field in fieldNames)
            {
                if (element.TryGetProperty(field, out var el))
                {
                    // Handle both string and number JSON values
                    if (el.ValueKind == JsonValueKind.String)
                    {
                        if (decimal.TryParse(el.GetString(), NumberStyles.Any, CultureInfo.InvariantCulture, out decimal val))
                            return val;
                    }
                    else if (el.ValueKind == JsonValueKind.Number)
                    {
                        return el.GetDecimal();
                    }
                }
            }
            return 0m;
        }

        /// <summary>
        /// Subscribe to additional markets on an already-connected stream.
        /// </summary>
        /// <summary>
        /// Subscribe to additional markets on an already-connected stream.
        /// Per Polymarket docs: dynamic subscription uses "operation":"subscribe", not full auth.
        /// </summary>
        public async Task SubscribeNewMarketsAsync(List<string> newConditionIds)
        {
            if (_ws == null || _ws.State != WebSocketState.Open || newConditionIds.Count == 0) return;

            _conditionIds.AddRange(newConditionIds);

            // Per docs: dynamic subscription format for user channel
            var subMsg = new
            {
                markets = newConditionIds,
                operation = "subscribe"
            };

            string json = JsonSerializer.Serialize(subMsg);
            await _ws.SendAsync(Encoding.UTF8.GetBytes(json), WebSocketMessageType.Text, true, _cts.Token);
        }

        public void Dispose()
        {
            _cts.Cancel();
            _ws?.Dispose();
            GC.SuppressFinalize(this);
        }
    }
}