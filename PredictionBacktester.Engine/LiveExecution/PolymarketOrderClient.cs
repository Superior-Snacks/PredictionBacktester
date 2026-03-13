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
        _httpClient = new RestClient(_config.Endpoint);
        _account = new Account(_config.PrivateKey, BigInteger.Parse(_config.ChainId));
    }

    /// <summary>
    /// Creates, signs, and submits a live order to the Polymarket CLOB.
    /// </summary>
    /// <param name="tokenId">The CLOB token ID for the outcome</param>
    /// <param name="price">Price per share (0.01 to 0.99)</param>
    /// <param name="size">Number of shares</param>
    /// <param name="side">0 = Buy, 1 = Sell</param>
    /// <param name="negRisk">True for multi-outcome (NegRisk) markets</param>
    /// <param name="tickSize">The tick size of the market (e.g. 0.01)</param>
    /// <param name="feeRateBps">The exact taker fee in basis points</param>
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
        price = Math.Round(price, tickDecimals, MidpointRounding.AwayFromZero);
        size = Math.Round(size, tickDecimals, MidpointRounding.AwayFromZero);

        // 2. Convert to BigIntegers (USDC and conditional tokens both use 6 decimals)
        const decimal DECIMALS = 1_000_000m;
        BigInteger makerAmount, takerAmount;

        if (side == 0) // BUY: pay USDC (maker), receive shares (taker)
        {
            takerAmount = new BigInteger((long)Math.Round(size * DECIMALS));
            makerAmount = new BigInteger((long)Math.Round(size * price * DECIMALS));
        }
        else // SELL: give shares (maker), receive USDC (taker)
        {
            makerAmount = new BigInteger((long)Math.Round(size * DECIMALS));
            takerAmount = new BigInteger((long)Math.Round(size * price * DECIMALS));
        }

        // 3. Enforce Polymarket precision rules (DO NOT ALTER AMOUNTS FOR FEES!)
        //   BUY:  makerAmount (USDC) max 5 decimals → divisible by 10
        //         takerAmount (shares) max 2 decimals → divisible by 10000
        //   SELL: makerAmount (shares) max 2 decimals → divisible by 10000
        //         takerAmount (USDC) max 5 decimals → divisible by 10
        if (side == 0)
        {
            makerAmount = (makerAmount / 10) * 10;
            takerAmount = (takerAmount / 10000) * 10000;
        }
        else
        {
            makerAmount = (makerAmount / 10000) * 10000;
            takerAmount = (takerAmount / 10) * 10;
        }

        // 4. Build the Order Struct: POLY_GNOSIS_SAFE mode (maker=proxy, signer=EOA, signatureType=2)
        var order = new PolymarketOrder
        {
            Salt = GenerateSalt(),
            Maker = _config.ProxyAddress,  // proxy wallet holds the funds
            Signer = _account.Address,     // EOA signs the order
            Taker = "0x0000000000000000000000000000000000000000",
            TokenId = BigInteger.Parse(tokenId),
            MakerAmount = makerAmount,
            TakerAmount = takerAmount,
            Expiration = BigInteger.Zero,
            Nonce = BigInteger.Zero,
            FeeRateBps = new BigInteger(feeRateBps),
            Side = side,
            SignatureType = 2 // POLY_GNOSIS_SAFE: maker=proxy, signer=EOA
        };

        // 5. Sign the order (EIP-712) using the correct exchange contract
        string verifyingContract = negRisk ? NEG_RISK_EXCHANGE : CTF_EXCHANGE;
        string signature = SignOrder(order, verifyingContract);

        // 6. Build JSON body using JsonNode so salt (BigInteger) serializes as a JSON number
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
                Console.WriteLine($"[ORDER DEBUG] HTTP {(int)response.StatusCode} | Headers: {string.Join(", ", response.Headers?.Select(h => $"{h.Name}={h.Value}") ?? Array.Empty<string>())}");
                Console.ResetColor();
            }
            throw new Exception($"[Polymarket API Error] {response.StatusCode}: {response.Content ?? "No response body"}");
        }

        return response.Content ?? "OK";
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
        Array.Copy(signature.R, 0, sigBytes, 0, 32);
        Array.Copy(signature.S, 0, sigBytes, 32, 32);
        sigBytes[64] = (byte)(signature.V[0]);

        if (DebugMode)
        {
            Console.ForegroundColor = ConsoleColor.DarkGray;
            Console.WriteLine($"[EIP712] typeHash:   0x{BitConverter.ToString(orderTypeHash).Replace("-", "").ToLower()}");
            Console.WriteLine($"[EIP712] domainSep:  0x{BitConverter.ToString(domainSeparator).Replace("-", "").ToLower()}");
            Console.WriteLine($"[EIP712] structHash: 0x{BitConverter.ToString(structHash).Replace("-", "").ToLower()}");
            Console.WriteLine($"[EIP712] digest:     0x{BitConverter.ToString(digest).Replace("-", "").ToLower()}");
            Console.ResetColor();
        }

        return "0x" + BitConverter.ToString(sigBytes).Replace("-", "").ToLower();
    }

    private static byte[] PadUint256(BigInteger value)
    {
        byte[] raw = value.ToByteArray(isUnsigned: true, isBigEndian: true);
        byte[] padded = new byte[32];
        Array.Copy(raw, 0, padded, 32 - raw.Length, raw.Length);
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
    /// Call this ONCE during startup per market, not during a live trade!
    /// </summary>
    public async Task<int> GetTakerFeeAsync(string tokenId)
    {
        try
        {
            // The CLOB API provides market metadata by token ID under /markets/{tokenId}
            var request = new RestRequest($"/markets/{tokenId}", Method.Get);
            var response = await _httpClient.ExecuteAsync(request);
            if (response.IsSuccessful && !string.IsNullOrEmpty(response.Content))
            {
                using var doc = JsonDocument.Parse(response.Content);
                var root = doc.RootElement;
                
                if (root.TryGetProperty("taker_fee_bps", out var feeEl))
                {
                    if (feeEl.ValueKind == JsonValueKind.String)
                        return int.Parse(feeEl.GetString()!);
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