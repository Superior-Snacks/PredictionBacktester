using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;

namespace PredictionBacktester.Engine;

public readonly struct BookFill
{
    public readonly decimal Price;
    public readonly decimal Shares;
    public BookFill(decimal price, decimal shares) { Price = price; Shares = shares; }
}

public readonly struct WalkResult
{
    public readonly decimal TotalShares;
    public readonly decimal TotalCost;
    public readonly decimal Vwap;

    public WalkResult(decimal totalShares, decimal totalCost)
    {
        TotalShares = totalShares;
        TotalCost = totalCost;
        Vwap = totalShares > 0 ? totalCost / totalShares : 0m;
    }
}

public class LocalOrderBook
{
    public string AssetId { get; private set; }

    // Private dictionaries protected by a lock
    private readonly SortedDictionary<decimal, decimal> _bids;
    private readonly SortedDictionary<decimal, decimal> _asks;
    private readonly object _bookLock = new object();

    public LocalOrderBook(string assetId)
    {
        AssetId = assetId;
        _bids = new SortedDictionary<decimal, decimal>();
        _asks = new SortedDictionary<decimal, decimal>();
    }

    public void ProcessBookUpdate(JsonElement bidsEl, JsonElement asksEl)
    {
        lock (_bookLock)
        {
            // --- PROCESS BIDS (Buyers) ---
            if (bidsEl.ValueKind == JsonValueKind.Array)
            {
                foreach (var bid in bidsEl.EnumerateArray())
                {
                    decimal price = decimal.Parse(bid.GetProperty("price").GetString() ?? "0");
                    decimal size = decimal.Parse(bid.GetProperty("size").GetString() ?? "0");

                    if (size == 0) _bids.Remove(price);
                    else _bids[price] = size;
                }
            }

            // --- PROCESS ASKS (Sellers) ---
            if (asksEl.ValueKind == JsonValueKind.Array)
            {
                foreach (var ask in asksEl.EnumerateArray())
                {
                    decimal price = decimal.Parse(ask.GetProperty("price").GetString() ?? "0");
                    decimal size = decimal.Parse(ask.GetProperty("size").GetString() ?? "0");

                    if (size == 0) _asks.Remove(price);
                    else _asks[price] = size;
                }
            }
        }
    }

    public decimal GetBestBidPrice()
    {
        lock (_bookLock) return _bids.Count == 0 ? 0.00m : _bids.Keys.Max();
    }

    public decimal GetBestAskPrice()
    {
        lock (_bookLock) return _asks.Count == 0 ? 1.00m : _asks.Keys.Min();
    }

    public decimal GetBestBidSize()
    {
        lock (_bookLock) return _bids.Count == 0 ? 0.00m : _bids[_bids.Keys.Max()];
    }

    public decimal GetBestAskSize()
    {
        lock (_bookLock) return _asks.Count == 0 ? 0.00m : _asks[_asks.Keys.Min()];
    }

    public void ConsumeAskLiquidity(decimal shares)
    {
        lock (_bookLock)
        {
            if (_asks.Count == 0 || shares <= 0) return;
            decimal bestPrice = _asks.Keys.Min();
            decimal remaining = _asks[bestPrice] - shares;
            if (remaining <= 0) _asks.Remove(bestPrice);
            else _asks[bestPrice] = remaining;
        }
    }

    public void ConsumeBidLiquidity(decimal shares)
    {
        lock (_bookLock)
        {
            if (_bids.Count == 0 || shares <= 0) return;
            decimal bestPrice = _bids.Keys.Max();
            decimal remaining = _bids[bestPrice] - shares;
            if (remaining <= 0) _bids.Remove(bestPrice);
            else _bids[bestPrice] = remaining;
        }
    }

    /// <summary>Returns the total bid volume across the top N highest price levels.</summary>
    public decimal GetTopBidVolume(int levels)
    {
        lock (_bookLock)
            return _bids.OrderByDescending(kv => kv.Key).Take(levels).Sum(kv => kv.Value);
    }

    /// <summary>Returns the total ask volume across the top N lowest price levels.</summary>
    public decimal GetTopAskVolume(int levels)
    {
        lock (_bookLock)
            return _asks.OrderBy(kv => kv.Key).Take(levels).Sum(kv => kv.Value);
    }

    public void UpdatePriceLevel(string side, decimal price, decimal size)
    {
        lock (_bookLock)
        {
            if (side == "BUY")
            {
                if (size == 0) _bids.Remove(price);
                else _bids[price] = size;
            }
            else if (side == "SELL")
            {
                if (size == 0) _asks.Remove(price);
                else _asks[price] = size;
            }
        }
    }

    /// <summary>
    /// Simulates a FAK buy: walks ask levels lowest-to-highest,
    /// filling up to maxDollars or until price exceeds maxPrice.
    /// Consumes liquidity from the book.
    /// </summary>
    public WalkResult WalkAsks(decimal maxPrice, decimal maxDollars, decimal participationRate)
    {
        lock (_bookLock)
        {
            decimal totalShares = 0m;
            decimal totalCost = 0m;
            decimal dollarsRemaining = maxDollars;

            var levels = _asks.OrderBy(kv => kv.Key).ToList();
            foreach (var (price, size) in levels)
            {
                if (price > maxPrice || dollarsRemaining <= 0.01m) break;

                decimal availableShares = size * participationRate;
                decimal affordableShares = dollarsRemaining / price;
                decimal sharesToFill = Math.Min(availableShares, affordableShares);

                if (sharesToFill <= 0) continue;

                totalShares += sharesToFill;
                totalCost += sharesToFill * price;
                dollarsRemaining -= sharesToFill * price;

                decimal remaining = size - sharesToFill;
                if (remaining <= 0) _asks.Remove(price);
                else _asks[price] = remaining;
            }

            return new WalkResult(totalShares, totalCost);
        }
    }

    /// <summary>
    /// Simulates a FAK sell: walks bid levels highest-to-lowest,
    /// filling up to maxShares or until price drops below minPrice.
    /// Consumes liquidity from the book.
    /// </summary>
    public WalkResult WalkBids(decimal minPrice, decimal maxShares, decimal participationRate)
    {
        lock (_bookLock)
        {
            decimal totalShares = 0m;
            decimal totalCost = 0m;
            decimal sharesRemaining = maxShares;

            var levels = _bids.OrderByDescending(kv => kv.Key).ToList();
            foreach (var (price, size) in levels)
            {
                if (price < minPrice || sharesRemaining <= 0) break;

                decimal availableShares = size * participationRate;
                decimal sharesToFill = Math.Min(availableShares, sharesRemaining);

                if (sharesToFill <= 0) continue;

                totalShares += sharesToFill;
                totalCost += sharesToFill * price;
                sharesRemaining -= sharesToFill;

                decimal remaining = size - sharesToFill;
                if (remaining <= 0) _bids.Remove(price);
                else _bids[price] = remaining;
            }

            return new WalkResult(totalShares, totalCost);
        }
    }
}