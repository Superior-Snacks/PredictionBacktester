using System.ComponentModel.DataAnnotations;
using System.ComponentModel.DataAnnotations.Schema;

namespace PredictionBacktester.Core.Entities.Database;

public class Market
{
    [Key]
    public string MarketId { get; set; }
    public string Exchange { get; set; } = "Polymarket";
    public string Title { get; set; }
    public DateTime? EndDate { get; set; }
    public bool IsClosed { get; set; }

    // This tells EF Core that one Market can have many Outcomes
    public List<Outcome> Outcomes { get; set; } = new();
}

public class Outcome
{
    [Key]
    public string OutcomeId { get; set; } // The ClobTokenId
    public string MarketId { get; set; }
    public string OutcomeName { get; set; }

    public Market Market { get; set; } // Navigation property
}

public class Trade
{
    [Key]
    [DatabaseGenerated(DatabaseGeneratedOption.Identity)]
    public long Id { get; set; } // Auto-incrementing ID

    public string OutcomeId { get; set; }
    public string ProxyWallet { get; set; }
    public string Side { get; set; }
    public decimal Price { get; set; }
    public decimal Size { get; set; }
    public long Timestamp { get; set; }
    public string TransactionHash { get; set; }
}