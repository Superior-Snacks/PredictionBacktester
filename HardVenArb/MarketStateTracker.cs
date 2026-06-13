using System.Collections.Concurrent;
using PredictionBacktester.Engine;

namespace HardVenArb;

/// <summary>
/// Owns the three concurrent state dictionaries shared between the WebSocket feeds
/// and the telemetry strategy. Centralises the K:/P: key conventions and
/// the init/clear logic so callers never construct key strings by hand.
/// </summary>
public class MarketStateTracker
{
    public ConcurrentDictionary<string, LocalOrderBook>                Books    { get; } = new(StringComparer.Ordinal);
    public ConcurrentDictionary<string, ConcurrentDictionary<decimal, decimal>>  YesSizes { get; } = new(StringComparer.Ordinal);
    public ConcurrentDictionary<string, ConcurrentDictionary<decimal, decimal>>  NoSizes  { get; } = new(StringComparer.Ordinal);

    /// <summary>Creates order books and size maps for a Kalshi YES/NO pair.</summary>
    public void InitKalshiMarket(string ticker)
    {
        Books[$"K:{ticker}"]    = new LocalOrderBook($"K:{ticker}");
        Books[$"K:{ticker}_NO"] = new LocalOrderBook($"K:{ticker}_NO");
        YesSizes[ticker]        = new ConcurrentDictionary<decimal, decimal>();
        NoSizes[ticker]         = new ConcurrentDictionary<decimal, decimal>();
    }

    /// <summary>Creates an order book for a HardVen token.</summary>
    public void InitHardVenToken(string token)
    {
        Books[$"H:{token}"] = new LocalOrderBook($"H:{token}");
    }

    /// <summary>Clears the books and size maps for a Kalshi market on reconnect.</summary>
    public void ClearKalshiMarket(string ticker)
    {
        if (Books.TryGetValue($"K:{ticker}",    out var yesBook)) yesBook.ClearBook();
        if (Books.TryGetValue($"K:{ticker}_NO", out var noBook))  noBook.ClearBook();
        if (YesSizes.TryGetValue(ticker, out var ySizes)) ySizes.Clear();
        if (NoSizes.TryGetValue(ticker,  out var nSizes)) nSizes.Clear();
    }

    /// <summary>Clears the book for a HardVen token on reconnect.</summary>
    public void ClearHardVenToken(string token)
    {
        if (Books.TryGetValue($"H:{token}", out var book)) book.ClearBook();
    }
}
