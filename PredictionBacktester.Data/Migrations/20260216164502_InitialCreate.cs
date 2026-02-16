using System;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace PredictionBacktester.Data.Migrations
{
    /// <inheritdoc />
    public partial class InitialCreate : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.CreateTable(
                name: "Markets",
                columns: table => new
                {
                    MarketId = table.Column<string>(type: "TEXT", nullable: false),
                    Exchange = table.Column<string>(type: "TEXT", nullable: false),
                    Title = table.Column<string>(type: "TEXT", nullable: false),
                    EndDate = table.Column<DateTime>(type: "TEXT", nullable: true),
                    IsClosed = table.Column<bool>(type: "INTEGER", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_Markets", x => x.MarketId);
                });

            migrationBuilder.CreateTable(
                name: "Trades",
                columns: table => new
                {
                    Id = table.Column<long>(type: "INTEGER", nullable: false)
                        .Annotation("Sqlite:Autoincrement", true),
                    OutcomeId = table.Column<string>(type: "TEXT", nullable: false),
                    ProxyWallet = table.Column<string>(type: "TEXT", nullable: false),
                    Side = table.Column<string>(type: "TEXT", nullable: false),
                    Price = table.Column<decimal>(type: "TEXT", nullable: false),
                    Size = table.Column<decimal>(type: "TEXT", nullable: false),
                    Timestamp = table.Column<long>(type: "INTEGER", nullable: false),
                    TransactionHash = table.Column<string>(type: "TEXT", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_Trades", x => x.Id);
                });

            migrationBuilder.CreateTable(
                name: "Outcomes",
                columns: table => new
                {
                    OutcomeId = table.Column<string>(type: "TEXT", nullable: false),
                    MarketId = table.Column<string>(type: "TEXT", nullable: false),
                    OutcomeName = table.Column<string>(type: "TEXT", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_Outcomes", x => x.OutcomeId);
                    table.ForeignKey(
                        name: "FK_Outcomes_Markets_MarketId",
                        column: x => x.MarketId,
                        principalTable: "Markets",
                        principalColumn: "MarketId",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateIndex(
                name: "IX_Outcomes_MarketId",
                table: "Outcomes",
                column: "MarketId");

            migrationBuilder.CreateIndex(
                name: "IX_Trades_OutcomeId_Timestamp",
                table: "Trades",
                columns: new[] { "OutcomeId", "Timestamp" });

            migrationBuilder.CreateIndex(
                name: "IX_Trades_ProxyWallet",
                table: "Trades",
                column: "ProxyWallet");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropTable(
                name: "Outcomes");

            migrationBuilder.DropTable(
                name: "Trades");

            migrationBuilder.DropTable(
                name: "Markets");
        }
    }
}
