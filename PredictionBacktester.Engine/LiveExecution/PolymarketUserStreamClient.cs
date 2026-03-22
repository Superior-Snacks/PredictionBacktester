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
                        ParseSingleTradeEvent(element);
                    }
                }
                else
                {
                    ParseSingleTradeEvent(root);
                }
            }
            catch (Exception ex)
            {
                // We log parse errors but DO NOT crash the thread
                Console.WriteLine($"[USER STREAM PARSE ERROR] {ex.Message}");
            }
        }

        private void ParseSingleTradeEvent(JsonElement element)
        {
            if (element.TryGetProperty("event", out var eventEl) && eventEl.GetString() == "trade")
            {
                // FIX #6: Defensive property checking (Asset_id vs TokenId)
                string tokenId = "";
                if (element.TryGetProperty("asset_id", out var assetEl)) tokenId = assetEl.GetString() ?? "";
                else if (element.TryGetProperty("tokenId", out var tokenEl)) tokenId = tokenEl.GetString() ?? "";
                
                string side = element.TryGetProperty("side", out var sideEl) ? (sideEl.GetString() ?? "") : "";

                // FIX #5: InvariantCulture to prevent European server decimal crashing
                decimal price = 0m;
                if (element.TryGetProperty("price", out var priceEl))
                    decimal.TryParse(priceEl.GetString(), NumberStyles.Any, CultureInfo.InvariantCulture, out price);

                decimal size = 0m;
                if (element.TryGetProperty("size", out var sizeEl))
                    decimal.TryParse(sizeEl.GetString(), NumberStyles.Any, CultureInfo.InvariantCulture, out size);

                if (!string.IsNullOrEmpty(tokenId) && size > 0)
                {
                    var tradeEvent = new UserTradeEvent(tokenId, price, size, side);
                    OnTradeMatched?.Invoke(tradeEvent);
                }
            }
        }

        public void Dispose()
        {
            _cts.Cancel();
            _ws?.Dispose();
            GC.SuppressFinalize(this);
        }
    }
}