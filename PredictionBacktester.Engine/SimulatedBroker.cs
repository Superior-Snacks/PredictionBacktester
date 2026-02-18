using System;
using System.Collections.Generic;
using System.Text;

namespace PredictionBacktester.Engine;

public class SimulatedBroker
{
    public decimal CashBalance { get; private set; }
    public decimal PositionShares { get; private set; }
    public int TotalTradesExecuted { get; private set; }

    public SimulatedBroker(decimal startingCash)
    {
        CashBalance = startingCash;
        PositionShares = 0;
        TotalTradesExecuted = 0;
    }

    /// <summary>
    /// Simulates buying shares of an outcome at the current tick price.
    /// </summary>
    public bool Buy(decimal currentPrice, decimal dollarsToInvest)
    {
        if (CashBalance < dollarsToInvest)
        {
            return false; // Insufficient funds!
        }

        // Calculate how many shares we get (ignoring fee slippage for now)
        decimal sharesBought = dollarsToInvest / currentPrice;

        CashBalance -= dollarsToInvest;
        PositionShares += sharesBought;
        TotalTradesExecuted++;

        return true;
    }

    /// <summary>
    /// Simulates selling all currently held shares at the current tick price.
    /// </summary>
    public bool SellAll(decimal currentPrice)
    {
        if (PositionShares <= 0)
        {
            return false; // Nothing to sell!
        }

        decimal cashReceived = PositionShares * currentPrice;

        CashBalance += cashReceived;
        PositionShares = 0;
        TotalTradesExecuted++;

        return true;
    }

    /// <summary>
    /// Calculates total portfolio value (Cash + Value of current shares).
    /// </summary>
    public decimal GetTotalPortfolioValue(decimal currentPrice)
    {
        return CashBalance + (PositionShares * currentPrice);
    }
}
