namespace PredictionBacktester.Engine.LiveExecution
{
    public class PolymarketApiConfig
    {
        public string Endpoint { get; set; } = "https://clob.polymarket.com";
        public string ChainId { get; set; } = "137"; // Polygon Mainnet
        
        // You will generate these from the Polymarket CLOB UI
        public string ApiKey { get; set; } 
        public string ApiSecret { get; set; }
        public string ApiPassphrase { get; set; }
        
        // The private key of the actual Polygon wallet (for EIP-712 signing)
        public string PrivateKey { get; set; }
        public string ProxyAddress { get; set; } // Your Polymarket proxy wallet

        // Polygon RPC endpoint for on-chain balance queries
        public string RpcUrl { get; set; } = "https://polygon-rpc.com";

        // Optional SOCKS5 proxy for REST API calls (balance + order execution).
        // WS feeds connect directly and do NOT use this proxy.
        // Format: socks5://localhost:1080  — leave empty to connect directly.
        public string SocksProxy { get; set; } = "";
    }
}