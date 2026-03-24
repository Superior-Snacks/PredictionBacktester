using System;
using System.Collections.Generic;
using System.Linq;
using System.Numerics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Nethereum.ABI.EIP712;
using Nethereum.ABI.FunctionEncoding.Attributes;
using Nethereum.Contracts;
using Nethereum.Signer;
using Nethereum.Util;
using Nethereum.Web3;
using Nethereum.Web3.Accounts;
using RestSharp;

namespace PredictionBacktester.Engine.LiveExecution;

public class PolymarketOrderClient
{
    private readonly PolymarketApiConfig _config;
    private readonly RestClient _httpClient;
    private readonly Account _account;

    /// <summary>When true, prints [ORDER DEBUG] payload and [EIP712] intermediate hashes.</summary>
    public volatile bool DebugMode = false;

    // CTF Exchange for standard binary markets
    private const string CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E";
    // NegRisk CTF Exchange for multi-outcome markets
    private const string NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a";
    // USDC on Polygon (bridged USDC.e used by Polymarket)
    private const string USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";
    private const int USDC_DECIMALS = 6;

    public PolymarketOrderClient(PolymarketApiConfig config)
    {
        _config = config;
        _httpClient = new RestClient(new RestClientOptions(_config.Endpoint) { Timeout = TimeSpan.FromSeconds(10) });
        _account = new Account(_config.PrivateKey, BigInteger.Parse(_config.ChainId));
    }

    /// <summary>
    /// Creates, signs, and submits a live order to the Polymarket CLOB.
    /// </summary>
    public async Task<string> SubmitOrderAsync(string tokenId, decimal price, decimal size, int side, bool negRisk = false, string tickSize = "0.01", int feeRateBps = 0)
    {
        // 1. ROUND PRICE AND SIZE to match the market's tick size
        int tickDecimals = tickSize switch
        {
            "0.1" => 1,
            "0.001" => 3,
            "0.0001" => 4,
            _ => 2 // "0.01" default
        };
        price = Math.Round(price, tickDecimals, MidpointRounding.ToEven);
        
        // Prevent price from rounding down to absolute zero
        if (price <= 0) price = decimal.Parse(tickSize);

        // 2. THE FAK MATH FIX (Asymmetrical Decimal Rules)
        decimal makerAmountDec;
        decimal takerAmountDec;

        if (side == 0) // BUY: pay USDC (maker), receive shares (taker)
        {
            makerAmountDec = Math.Round(size * price, 2); // USDC Spent (Max 2)
            takerAmountDec = Math.Round(size, 4);         // Shares Received (Max 4)
            
            // Failsafe: Cannot send 0 USDC
            if (makerAmountDec <= 0) makerAmountDec = 0.01m; 
        }
        else // SELL: give shares (maker), receive USDC (taker)
        {
            makerAmountDec = Math.Round(size, 2);         // Shares Given (MAX 2 PER API)
            takerAmountDec = Math.Round(size * price, 5); // USDC Received (MAX 5 PER API)
            
            // Failsafe: Cannot expect 0 USDC in return
            if (takerAmountDec <= 0) takerAmountDec = 0.00001m; 
        }

        // 3. Convert to Blockchain Format (BigInteger scale by 10^6)
        BigInteger makerAmount = new BigInteger((long)(makerAmountDec * 1_000_000m));
        BigInteger takerAmount = new BigInteger((long)(takerAmountDec * 1_000_000m));

        // 4. Build the Order Struct: POLY_GNOSIS_SAFE mode
        var order = new PolymarketOrder
        {
            Salt = GenerateSalt(),
            Maker = _config.ProxyAddress,  
            Signer = _account.Address,     
            Taker = "0x0000000000000000000000000000000000000000",
            TokenId = BigInteger.Parse(tokenId),
            MakerAmount = makerAmount,
            TakerAmount = takerAmount,
            Expiration = BigInteger.Zero,
            Nonce = BigInteger.Zero,
            FeeRateBps = new BigInteger(feeRateBps),
            Side = side,
            SignatureType = 2 
        };

        // 5. Sign the order (EIP-712) using the correct exchange contract
        string verifyingContract = negRisk ? NEG_RISK_EXCHANGE : CTF_EXCHANGE;
        string signature = SignOrder(order, verifyingContract);

        // 6. Build JSON body using JsonNode
        var orderNode = new JsonObject
        {
            ["salt"] = (long)order.Salt,
            ["maker"] = order.Maker,
            ["signer"] = order.Signer,
            ["taker"] = order.Taker,
            ["tokenId"] = order.TokenId.ToString(),
            ["makerAmount"] = order.MakerAmount.ToString(),
            ["takerAmount"] = order.TakerAmount.ToString(),
            ["expiration"] = order.Expiration.ToString(),
            ["nonce"] = order.Nonce.ToString(),
            ["feeRateBps"] = order.FeeRateBps.ToString(),
            ["side"] = side == 0 ? "BUY" : "SELL",
            ["signatureType"] = order.SignatureType,
            ["signature"] = signature
        };

        var payloadNode = new JsonObject
        {
            ["order"] = orderNode,
            ["owner"] = _config.ApiKey,
            ["orderType"] = "FAK"
        };

        string jsonBody = payloadNode.ToJsonString();

        if (DebugMode)
        {
            Console.ForegroundColor = ConsoleColor.DarkYellow;
            Console.WriteLine($"\n[ORDER DEBUG] negRisk={negRisk} | tickSize={tickSize} | exchange={verifyingContract}");
            Console.WriteLine($"[ORDER DEBUG] maker={order.Maker} | signer={order.Signer}");
            Console.WriteLine($"[ORDER DEBUG] price={price} | size={size} | side={(side == 0 ? "BUY" : "SELL")}");
            Console.WriteLine($"[ORDER DEBUG] makerAmt={order.MakerAmount} | takerAmt={order.TakerAmount}");
            Console.WriteLine($"[ORDER DEBUG] feeRateBps={feeRateBps}");
            Console.WriteLine($"[ORDER DEBUG] POST /order payload:");
            Console.WriteLine(jsonBody);
            Console.ResetColor();
        }

        // 7. Build request with L2 HMAC auth headers
        var request = new RestRequest("/order", Method.Post);
        string timestamp = DateTimeOffset.UtcNow.ToUnixTimeSeconds().ToString();
        string hmacSignature = BuildHmacSignature(_config.ApiSecret, timestamp, "POST", "/order", jsonBody);

        request.AddHeader("POLY_ADDRESS", _account.Address);
        request.AddHeader("POLY_SIGNATURE", hmacSignature);
        request.AddHeader("POLY_TIMESTAMP", timestamp);
        request.AddHeader("POLY_API_KEY", _config.ApiKey);
        request.AddHeader("POLY_PASSPHRASE", _config.ApiPassphrase);
        request.AddStringBody(jsonBody, ContentType.Json);

        // 8. Submit
        var response = await _httpClient.ExecuteAsync(request);

        if (!response.IsSuccessful)
        {
            if (DebugMode)
            {
                Console.ForegroundColor = ConsoleColor.Red;
                Console.WriteLine($"[ORDER DEBUG] HTTP {(int)response.StatusCode} | Body: {response.Content ?? "(empty)"}");
                Console.ResetColor();
            }
            throw new Exception($"[Polymarket API Error] {response.StatusCode}: {response.Content ?? "No response body"}");
        }

        return response.Content ?? "OK";
    }

    /// <summary>
    /// Fetches the current status of a specific order by its ID.
    /// </summary>
    public async Task<string> GetOrderAsync(string orderId)
    {
        var request = new RestRequest($"/order/{orderId}", Method.Get);
        string timestamp = DateTimeOffset.UtcNow.ToUnixTimeSeconds().ToString();
        string hmacSignature = BuildHmacSignature(_config.ApiSecret, timestamp, "GET", $"/order/{orderId}");

        request.AddHeader("POLY_ADDRESS", _account.Address);
        request.AddHeader("POLY_SIGNATURE", hmacSignature);
        request.AddHeader("POLY_TIMESTAMP", timestamp);
        request.AddHeader("POLY_API_KEY", _config.ApiKey);
        request.AddHeader("POLY_PASSPHRASE", _config.ApiPassphrase);

        var response = await _httpClient.ExecuteAsync(request);

        if (!response.IsSuccessful)
        {
            throw new Exception($"[Polymarket API Error] {response.StatusCode}: {response.Content}");
        }

        return response.Content ?? "{}";
    }

    /// <summary>
    /// Queries the on-chain USDC balance for the proxy wallet.
    /// </summary>
    public async Task<decimal> GetUsdcBalanceAsync()
    {
        var web3 = new Web3(_config.RpcUrl);
        var contract = web3.Eth.GetContract(BalanceOfAbi, USDC_CONTRACT);
        var balanceOf = contract.GetFunction("balanceOf");
        var rawBalance = await balanceOf.CallAsync<BigInteger>(_config.ProxyAddress);
        return (decimal)rawBalance / (decimal)Math.Pow(10, USDC_DECIMALS);
    }

    // Conditional Token Framework (ERC-1155) on Polygon
    private const string CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045";
    private const int CTF_DECIMALS = 6;

    /// <summary>
    /// Queries the on-chain balance of a conditional token for the proxy wallet.
    /// </summary>
    public async Task<decimal> GetTokenBalanceAsync(string tokenId)
    {
        var web3 = new Web3(_config.RpcUrl);
        var contract = web3.Eth.GetContract(BalanceOfErc1155Abi, CTF_CONTRACT);
        var balanceOf = contract.GetFunction("balanceOf");
        var rawBalance = await balanceOf.CallAsync<BigInteger>(_config.ProxyAddress, BigInteger.Parse(tokenId));
        return (decimal)rawBalance / (decimal)Math.Pow(10, CTF_DECIMALS);
    }

    private static readonly string BalanceOfAbi = @"[{""constant"":true,""inputs"":[{""name"":""account"",""type"":""address""}],""name"":""balanceOf"",""outputs"":[{""name"":"""",""type"":""uint256""}],""type"":""function""}]";

    private static readonly string BalanceOfErc1155Abi = @"[{""constant"":true,""inputs"":[{""name"":""account"",""type"":""address""},{""name"":""id"",""type"":""uint256""}],""name"":""balanceOf"",""outputs"":[{""name"":"""",""type"":""uint256""}],""type"":""function""}]";

    // Manual EIP-712 signing
    private string SignOrder(PolymarketOrder order, string verifyingContract)
    {
        byte[] orderTypeHash = Sha3Keccack.Current.CalculateHash(
            Encoding.UTF8.GetBytes(
                "Order(uint256 salt,address maker,address signer,address taker,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint256 expiration,uint256 nonce,uint256 feeRateBps,uint8 side,uint8 signatureType)"
            ));

        byte[] domainTypeHash = Sha3Keccack.Current.CalculateHash(
            Encoding.UTF8.GetBytes(
                "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
            ));
        byte[] nameHash = Sha3Keccack.Current.CalculateHash(
            Encoding.UTF8.GetBytes("Polymarket CTF Exchange"));
        byte[] versionHash = Sha3Keccack.Current.CalculateHash(
            Encoding.UTF8.GetBytes("1"));

        byte[] domainData = ConcatBytes(
            domainTypeHash,
            nameHash,
            versionHash,
            PadUint256(BigInteger.Parse(_config.ChainId)),
            PadAddress(verifyingContract)
        );
        byte[] domainSeparator = Sha3Keccack.Current.CalculateHash(domainData);

        byte[] structData = ConcatBytes(
            orderTypeHash,
            PadUint256(order.Salt),
            PadAddress(order.Maker),
            PadAddress(order.Signer),
            PadAddress(order.Taker),
            PadUint256(order.TokenId),
            PadUint256(order.MakerAmount),
            PadUint256(order.TakerAmount),
            PadUint256(order.Expiration),
            PadUint256(order.Nonce),
            PadUint256(order.FeeRateBps), // Properly padded as uint256
            PadUint256(new BigInteger(order.Side)),
            PadUint256(new BigInteger(order.SignatureType))
        );
        byte[] structHash = Sha3Keccack.Current.CalculateHash(structData);

        byte[] digest = Sha3Keccack.Current.CalculateHash(
            ConcatBytes(new byte[] { 0x19, 0x01 }, domainSeparator, structHash));

        var ecKey = new EthECKey(_account.PrivateKey);
        var signature = ecKey.SignAndCalculateV(digest);
        byte[] sigBytes = new byte[65];
        // R and S can be < 32 bytes when they have leading zeros — pad to 32
        Array.Copy(signature.R, 0, sigBytes, 32 - signature.R.Length, signature.R.Length);
        Array.Copy(signature.S, 0, sigBytes, 64 - signature.S.Length, signature.S.Length);
        sigBytes[64] = (byte)(signature.V[0]);

        return "0x" + BitConverter.ToString(sigBytes).Replace("-", "").ToLower();
    }

    private static byte[] PadUint256(BigInteger value)
{
    byte[] bytes = value.ToByteArray(isUnsigned: true, isBigEndian: true);
    
    if (bytes.Length == 32) return bytes;
    
    byte[] padded = new byte[32];
    
    if (bytes.Length < 32)
    {
        // Pad with leading zeros (Big Endian standard)
        Buffer.BlockCopy(bytes, 0, padded, 32 - bytes.Length, bytes.Length);
    }
    else
    {
        // Protects against the 33-byte sign-bit crash on massive Token IDs
        Buffer.BlockCopy(bytes, bytes.Length - 32, padded, 0, 32);
    }
    
    return padded;
}

    private static byte[] PadAddress(string address)
    {
        string hex = address.StartsWith("0x") ? address[2..] : address;
        byte[] raw = Convert.FromHexString(hex);
        byte[] padded = new byte[32];
        Array.Copy(raw, 0, padded, 32 - raw.Length, raw.Length);
        return padded;
    }

    private static byte[] ConcatBytes(params byte[][] arrays)
    {
        int totalLen = 0;
        foreach (var a in arrays) totalLen += a.Length;
        byte[] result = new byte[totalLen];
        int offset = 0;
        foreach (var a in arrays)
        {
            Array.Copy(a, 0, result, offset, a.Length);
            offset += a.Length;
        }
        return result;
    }

    /// <summary>
    /// Fetches the exact taker fee (in basis points) from the CLOB API.
    /// </summary>
    public async Task<int> GetTakerFeeAsync(string tokenId)
    {
        try
        {
            // Correct endpoint per Polymarket docs: GET /fee-rate?token_id={token_id}
            var request = new RestRequest("/fee-rate", Method.Get);
            request.AddQueryParameter("token_id", tokenId);
            var response = await _httpClient.ExecuteAsync(request);
            if (response.IsSuccessful && !string.IsNullOrEmpty(response.Content))
            {
                using var doc = JsonDocument.Parse(response.Content);
                var root = doc.RootElement;

                // Response contains fee_rate_bps or feeRateBps
                if (root.TryGetProperty("fee_rate_bps", out var feeEl) ||
                    root.TryGetProperty("feeRateBps", out feeEl))
                {
                    if (feeEl.ValueKind == JsonValueKind.String)
                        return int.Parse(feeEl.GetString()!);
                    if (feeEl.ValueKind == JsonValueKind.Number)
                        return feeEl.GetInt32();
                }
            }
        }
        catch (Exception)
        {
            // Silent catch: if it fails, we fall back to 0
        }
        return 0;
    }

    /// <summary>
    /// HMAC-SHA256 signature for L2 API authentication.
    /// </summary>
    private static string BuildHmacSignature(string secret, string timestamp, string method, string requestPath, string? body = null)
    {
        byte[] key = Convert.FromBase64String(secret.Replace('-', '+').Replace('_', '/'));
        string message = timestamp + method + requestPath;
        if (!string.IsNullOrEmpty(body))
            message += body;

        using var hmac = new HMACSHA256(key);
        byte[] hash = hmac.ComputeHash(Encoding.UTF8.GetBytes(message));
        return Convert.ToBase64String(hash).Replace('+', '-').Replace('/', '_');
    }

    private static BigInteger GenerateSalt()
    {
        byte[] buf = new byte[8];
        RandomNumberGenerator.Fill(buf);
        long raw = BitConverter.ToInt64(buf) & long.MaxValue; 
        return new BigInteger(raw % 10_000_000_000L);
    }

    private static BigInteger GetExpirationTimestamp(int secondsFromNow)
    {
        return new BigInteger(DateTimeOffset.UtcNow.AddSeconds(secondsFromNow).ToUnixTimeSeconds());
    }

    private static BigInteger GenerateNonce()
    {
        return new BigInteger(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
    }
}