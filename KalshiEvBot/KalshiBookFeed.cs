using System.Buffers;
using System.Collections.Concurrent;
using System.Globalization;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using PredictionBacktester.Engine.LiveExecution;

namespace KalshiEvBot;

/// <summary>Top of book for one Kalshi market, as the WS believes it. A DETECTION artefact, not a price.</summary>
public readonly record struct BookTop(decimal YesAsk, decimal NoAsk, decimal YesAskDepth, decimal NoAskDepth,
                                      double AgeMs, bool HasSnapshot);

/// <summary>
/// Kalshi order-book WebSocket, used for ONE job: noticing that a market moved.
///
/// <para><b>Nothing here may price a trade.</b> Measured over ~400 windows the WS ask ran a median 4c
/// BELOW the REST ask and was optimistic 95% of the time, and it is not staleness — 389 of those windows
/// opened on a book aged 0ms and those are the ones carrying the error, while the handful of older books
/// were exact. The error arrives on the tick. So this class exposes a top of book, the evaluator treats it
/// strictly as "look here", and the price that reaches EV comes from REST (EVBOT_TODO.md §4).</para>
///
/// <para><b>Why an optimistic feed is still a sound filter.</b> Its ask is too LOW, so the EV it implies is
/// too HIGH — an upper bound. A candidate the WS says is not +EV cannot be +EV at REST either, so
/// pre-screening here throws away only what REST would have rejected anyway, and buys a REST call per
/// signal instead of per tick. The small slack in the evaluator covers the 5% that run the other way.</para>
///
/// <para>Kalshi V2 publishes a YES-denominated book of BIDS on both sides: a "yes" level is an order to buy
/// YES, a "no" level an order to buy NO. Crossing therefore reads off the opposite side —
/// yes_ask = 1 − best_no_bid, no_ask = 1 − best_yes_bid.</para>
/// </summary>
public sealed class KalshiBookFeed
{
    private sealed class Book
    {
        public readonly ConcurrentDictionary<decimal, decimal> YesBids = new();
        public readonly ConcurrentDictionary<decimal, decimal> NoBids  = new();
        public long LastUpdateTicks = DateTime.UtcNow.Ticks;
        public volatile bool HasSnapshot;
    }

    private readonly KalshiOrderClient _client;
    private readonly KalshiApiConfig   _config;
    private readonly List<string>      _tickers;
    private readonly int               _batchSize;
    private readonly decimal           _minBookPrice;
    private readonly ConcurrentDictionary<string, Book> _books = new(StringComparer.Ordinal);
    private int _msgId = 1;

    public volatile bool IsConnected;
    private long _lastMessageTicks = DateTime.UtcNow.Ticks;
    public DateTime LastMessageAt => new(Volatile.Read(ref _lastMessageTicks), DateTimeKind.Utc);
    public long MessageCount;

    /// <summary>Fired with the ticker whose top of book just changed. Runs on the socket thread, so
    /// handlers must be cheap and must not block — the evaluator queues and returns.</summary>
    public event Action<string>? OnBookChanged;

    public KalshiBookFeed(KalshiOrderClient client, KalshiApiConfig config, IEnumerable<string> tickers,
                          int batchSize = 100, decimal minBookPrice = 0.01m)
    {
        _client       = client;
        _config       = config;
        _tickers      = tickers.Distinct(StringComparer.Ordinal).ToList();
        _batchSize    = batchSize;
        _minBookPrice = minBookPrice;
        foreach (var t in _tickers) _books[t] = new Book();
    }

    /// <summary>Current top of book. Empty side reads as an ask of 1.00, which no EV screen can clear —
    /// the safe direction for a missing price.</summary>
    public BookTop Top(string ticker)
    {
        if (!_books.TryGetValue(ticker, out var b)) return new BookTop(1m, 1m, 0m, 0m, -1, false);

        decimal bestNoBid = 0m, noBidSize = 0m, bestYesBid = 0m, yesBidSize = 0m;
        foreach (var kv in b.NoBids)  if (kv.Value > 0m && kv.Key > bestNoBid)  { bestNoBid  = kv.Key; noBidSize  = kv.Value; }
        foreach (var kv in b.YesBids) if (kv.Value > 0m && kv.Key > bestYesBid) { bestYesBid = kv.Key; yesBidSize = kv.Value; }

        return new BookTop(
            YesAsk      : bestNoBid  > 0m ? Math.Round(1m - bestNoBid,  4) : 1m,
            NoAsk       : bestYesBid > 0m ? Math.Round(1m - bestYesBid, 4) : 1m,
            YesAskDepth : noBidSize,
            NoAskDepth  : yesBidSize,
            AgeMs       : (DateTime.UtcNow - new DateTime(Volatile.Read(ref b.LastUpdateTicks), DateTimeKind.Utc)).TotalMilliseconds,
            HasSnapshot : b.HasSnapshot);
    }

    /// <summary>Top three ask levels on one side, for the book audit. Returns (price, size) descending
    /// by attractiveness (cheapest ask first).</summary>
    public List<(decimal Price, decimal Size)> AskLadder(string ticker, bool yesSide, int depth = 3)
    {
        if (!_books.TryGetValue(ticker, out var b)) return new();
        var src = yesSide ? b.NoBids : b.YesBids;           // asks are the complement of the opposite bids
        return src.Where(kv => kv.Value > 0m)
                  .OrderByDescending(kv => kv.Key)          // best bid = cheapest ask
                  .Take(depth)
                  .Select(kv => (Math.Round(1m - kv.Key, 4), kv.Value))
                  .ToList();
    }

    public async Task RunAsync(CancellationToken ct)
    {
        if (_tickers.Count == 0) { Console.WriteLine("[KALSHI WS] no tickers — feed idle."); return; }

        bool firstConnect = true;
        while (!ct.IsCancellationRequested)
        {
            try
            {
                using var ws = new ClientWebSocket();
                var (key, ts, sig) = _client.CreateAuthHeaders("GET", "/trade-api/ws/v2");
                ws.Options.SetRequestHeader("KALSHI-ACCESS-KEY",       key);
                ws.Options.SetRequestHeader("KALSHI-ACCESS-TIMESTAMP", ts);
                ws.Options.SetRequestHeader("KALSHI-ACCESS-SIGNATURE", sig);
                await ws.ConnectAsync(new Uri(_config.BaseWsUrl), ct);
                Console.WriteLine($"[KALSHI WS] connected to {_config.BaseWsUrl}");

                for (int i = 0; i < _tickers.Count; i += _batchSize)
                {
                    string arr = string.Join(",", _tickers.Skip(i).Take(_batchSize).Select(t => $"\"{t}\""));
                    string sub = $"{{\"id\":{_msgId++},\"cmd\":\"subscribe\",\"params\":{{\"channels\":[\"orderbook_delta\"],\"market_tickers\":[{arr}]}}}}";
                    await ws.SendAsync(Encoding.UTF8.GetBytes(sub), WebSocketMessageType.Text, true, ct);
                    await Task.Delay(100, ct);
                }
                Console.WriteLine($"[KALSHI WS] subscribed to {_tickers.Count} ticker(s)");
                IsConnected = true;

                // A reconnect leaves every book frozen at whatever it held when the socket died. Clearing
                // them costs one snapshot each and prevents an evaluation against a book from before the gap.
                if (!firstConnect)
                    foreach (var b in _books.Values) { b.YesBids.Clear(); b.NoBids.Clear(); b.HasSnapshot = false; }
                firstConnect = false;

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
                                break;
                            }
                            ms.Write(buf, 0, result.Count);
                        } while (!result.EndOfMessage);

                        if (ms.Length == 0) continue;
                        string message = Encoding.UTF8.GetString(ms.GetBuffer(), 0, (int)ms.Length);
                        if (message is "heartbeat" or "PONG" or "pong") continue;

                        Volatile.Write(ref _lastMessageTicks, DateTime.UtcNow.Ticks);
                        Interlocked.Increment(ref MessageCount);
                        Process(message);
                    }
                }
                finally { ArrayPool<byte>.Shared.Return(buf); }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception ex)
            {
                // Includes a socket-abort OperationCanceledException with ct NOT cancelled (machine
                // sleep/wake). Transient: reconnect, never exit — exiting used to take the whole bot down.
                Console.WriteLine($"[KALSHI WS] {ex.GetType().Name}: {ex.Message} — reconnecting in 5s");
            }

            IsConnected = false;
            if (!ct.IsCancellationRequested)
                await Task.Delay(5_000, ct).ContinueWith(_ => { });
        }
        IsConnected = false;
    }

    private void Process(string message)
    {
        try
        {
            using var doc = JsonDocument.Parse(message);
            var root = doc.RootElement;
            if (!root.TryGetProperty("type", out var typeEl)) return;
            if (!root.TryGetProperty("msg",  out var msgEl))  return;
            if (!msgEl.TryGetProperty("market_ticker", out var tEl)) return;

            string ticker = tEl.GetString() ?? "";
            if (!_books.TryGetValue(ticker, out var book)) return;

            string type = typeEl.GetString() ?? "";
            if      (type == "orderbook_snapshot") ApplySnapshot(book, msgEl);
            else if (type == "orderbook_delta")    ApplyDelta(book, msgEl);
            else return;

            Volatile.Write(ref book.LastUpdateTicks, DateTime.UtcNow.Ticks);
            try { OnBookChanged?.Invoke(ticker); } catch { /* a handler fault must not kill the socket */ }
        }
        catch (JsonException) { /* a malformed frame is not worth a reconnect */ }
    }

    private static bool TryLevel(JsonElement lvl, out decimal price, out decimal size)
    {
        price = 0; size = 0;
        if (lvl.ValueKind != JsonValueKind.Array) return false;
        var a = lvl.EnumerateArray().ToArray();
        return a.Length >= 2
            && decimal.TryParse(a[0].GetString(), NumberStyles.Any, CultureInfo.InvariantCulture, out price)
            && decimal.TryParse(a[1].GetString(), NumberStyles.Any, CultureInfo.InvariantCulture, out size);
    }

    /// <summary>Is this a price we will act on at all? Applied to snapshots AND deltas, deliberately.
    /// The arb bot guarded only the delta path, so a level admitted by a snapshot could never be removed
    /// by a delta that was filtered out — one of the two candidate causes of the +4c WS bias, retired here
    /// for free by applying the same rule on both paths.</summary>
    private bool InRange(decimal price) => price >= _minBookPrice && price <= 1m - _minBookPrice;

    private void ApplyDelta(Book b, JsonElement msg)
    {
        if (!msg.TryGetProperty("price_dollars", out var pEl)) return;
        if (!msg.TryGetProperty("delta_fp",      out var dEl)) return;
        if (!msg.TryGetProperty("side",          out var sEl)) return;
        if (!decimal.TryParse(pEl.GetString(), NumberStyles.Any, CultureInfo.InvariantCulture, out decimal price)) return;
        if (!decimal.TryParse(dEl.GetString(), NumberStyles.Any, CultureInfo.InvariantCulture, out decimal delta)) return;
        if (!InRange(price)) return;

        string side = sEl.GetString() ?? "";
        var map = side == "yes" ? b.YesBids : side == "no" ? b.NoBids : null;
        if (map is null) return;

        decimal now = (map.TryGetValue(price, out var cur) ? cur : 0m) + delta;
        if (now <= 0m) map.TryRemove(price, out _); else map[price] = now;
    }

    private void ApplySnapshot(Book b, JsonElement msg)
    {
        b.YesBids.Clear();
        b.NoBids.Clear();
        if (msg.TryGetProperty("yes_dollars_fp", out var y) && y.ValueKind == JsonValueKind.Array)
            foreach (var l in y.EnumerateArray())
                if (TryLevel(l, out var p, out var s) && s > 0m && InRange(p)) b.YesBids[p] = s;
        if (msg.TryGetProperty("no_dollars_fp", out var n) && n.ValueKind == JsonValueKind.Array)
            foreach (var l in n.EnumerateArray())
                if (TryLevel(l, out var p, out var s) && s > 0m && InRange(p)) b.NoBids[p] = s;
        b.HasSnapshot = true;
    }
}
