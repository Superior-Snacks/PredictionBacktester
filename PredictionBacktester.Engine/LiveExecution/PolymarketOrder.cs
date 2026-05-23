using System.Numerics;

namespace PredictionBacktester.Engine.LiveExecution
{
    public class PolymarketOrder
    {
        public BigInteger Salt          { get; set; }
        public string     Maker         { get; set; }
        public string     Signer        { get; set; }
        public BigInteger TokenId       { get; set; }
        public BigInteger MakerAmount   { get; set; }
        public BigInteger TakerAmount   { get; set; }
        public BigInteger Expiration    { get; set; } // wire body only, not signed in V2
        public BigInteger Timestamp     { get; set; } // Unix ms, signed in V2
        public int        Side          { get; set; }
        public int        SignatureType { get; set; }
    }
}
