using Microsoft.EntityFrameworkCore;
using PredictionBacktester.Core.Entities.Database;

namespace PredictionBacktester.Data.Database;

public class PolymarketDbContext : DbContext
{
    // These DbSets represent your actual SQL tables
    public DbSet<Market> Markets { get; set; }
    public DbSet<Outcome> Outcomes { get; set; }
    public DbSet<Trade> Trades { get; set; }

    protected override void OnConfiguring(DbContextOptionsBuilder optionsBuilder)
    {
        // This tells EF Core to create a file named 'polymarket_backtest.db' in your root folder
        optionsBuilder.UseSqlite("Data Source=polymarket_backtest.db;");
    }

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        // Let's create an index on the Trades table so backtesting is lightning fast!
        modelBuilder.Entity<Trade>()
            .HasIndex(t => new { t.OutcomeId, t.Timestamp });

        modelBuilder.Entity<Trade>()
            .HasIndex(t => t.ProxyWallet);
    }
}