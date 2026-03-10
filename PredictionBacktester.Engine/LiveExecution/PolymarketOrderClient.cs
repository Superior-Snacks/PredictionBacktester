using System;
using System.Collections.Generic;
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
using Nethereum.Signer.EIP712;
using Nethereum.Web3;
using Nethereum.Web3.Accounts;
using RestSharp;

namespace PredictionBacktester.Engine.LiveExecution;

public class PolymarketOrderClient
{
    private readonly PolymarketApiConfig _config;
    private readonly RestClient _httpClient;
    private readonly Account _account;

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
    public async Task<string> SubmitOrderAsync(string tokenId, decimal price, decimal size, int side, bool negRisk = false, string tickSize = "0.01")
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
        const long DECIMALS = 1_000_000;
        BigInteger makerAmount, takerAmount;

        if (side == 0) // BUY: pay USDC (maker), receive shares (taker)
        {
            takerAmount = new BigInteger((long)(size * DECIMALS));
            makerAmount = new BigInteger((long)(size * price * DECIMALS));
        }
        else // SELL: give shares (maker), receive USDC (taker)
        {
            makerAmount = new BigInteger((long)(size * DECIMALS));
            takerAmount = new BigInteger((long)(size * price * DECIMALS));
        }

        // 3. Build the Order Struct
        var order = new PolymarketOrder
        {
            Salt = GenerateSalt(),
            Maker = _config.ProxyAddress,
            Signer = _account.Address,
            Taker = "0x0000000000000000000000000000000000000000",
            TokenId = BigInteger.Parse(tokenId),
            MakerAmount = makerAmount,
            TakerAmount = takerAmount,
            Expiration = GetExpirationTimestamp(300), // 5 minutes
            Nonce = GenerateNonce(),
            FeeRateBps = 0,
            Side = side,
            SignatureType = 1 // POLY_PROXY (maker is proxy wallet, signer is EOA)
        };

        // 4. Sign the order (EIP-712) using the correct exchange contract
        string verifyingContract = negRisk ? NEG_RISK_EXCHANGE : CTF_EXCHANGE;
        string signature = SignOrder(order, verifyingContract);

        // 5. Build JSON body using JsonNode so salt (BigInteger) serializes as a JSON number
        var orderNode = new JsonObject
        {
            ["salt"] = JsonNode.Parse(order.Salt.ToString()),
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

        // 6. Final Payload (tickSize and negRisk are SDK-level options, NOT part of the POST body)
        var payloadNode = new JsonObject
        {
            ["order"] = orderNode,
            ["owner"] = _config.ApiKey,
            ["orderType"] = "GTC"
        };

        string jsonBody = payloadNode.ToJsonString();

        // Debug: log the exact payload so we can diagnose API rejections
        Console.ForegroundColor = ConsoleColor.DarkYellow;
        Console.WriteLine($"\n[ORDER DEBUG] POST /order payload:");
        Console.WriteLine(jsonBody);
        Console.ResetColor();

        // 5. Build request with L2 HMAC auth headers
        var request = new RestRequest("/order", Method.Post);
        string timestamp = DateTimeOffset.UtcNow.ToUnixTimeSeconds().ToString();
        string hmacSignature = BuildHmacSignature(_config.ApiSecret, timestamp, "POST", "/order", jsonBody);

        request.AddHeader("POLY_ADDRESS", _account.Address);
        request.AddHeader("POLY_SIGNATURE", hmacSignature);
        request.AddHeader("POLY_TIMESTAMP", timestamp);
        request.AddHeader("POLY_API_KEY", _config.ApiKey);
        request.AddHeader("POLY_PASSPHRASE", _config.ApiPassphrase);
        request.AddStringBody(jsonBody, ContentType.Json);

        // 6. Submit
        var response = await _httpClient.ExecuteAsync(request);

        if (!response.IsSuccessful)
        {
            throw new Exception($"[Polymarket API Error] {response.StatusCode}: {response.Content ?? "No response body"}");
        }

        return response.Content ?? "OK";
    }

    /// <summary>
    /// Queries the on-chain USDC balance for the proxy wallet.
    /// Returns the balance as a decimal in dollars (e.g. 250.50).
    /// </summary>
    public async Task<decimal> GetUsdcBalanceAsync()
    {
        var web3 = new Web3(_config.RpcUrl);
        var contract = web3.Eth.GetContract(BalanceOfAbi, USDC_CONTRACT);
        var balanceOf = contract.GetFunction("balanceOf");
        var rawBalance = await balanceOf.CallAsync<BigInteger>(_config.ProxyAddress);
        return (decimal)rawBalance / (decimal)Math.Pow(10, USDC_DECIMALS);
    }

    // Conditional Token Framework (ERC-1155) on Polygon — holds YES/NO position tokens
    private const string CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045";
    private const int CTF_DECIMALS = 6;

    /// <summary>
    /// Queries the on-chain balance of a conditional token (YES/NO position) for the proxy wallet.
    /// TokenId is the CLOB token ID (a large uint256). Returns shares as a decimal (e.g. 150.50 shares).
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

    private string SignOrder(PolymarketOrder order, string verifyingContract)
    {
        var typedData = new TypedData<Domain>
        {
            Domain = new Domain
            {
                Name = "Polymarket CTF Exchange",
                Version = "1",
                ChainId = BigInteger.Parse(_config.ChainId),
                VerifyingContract = verifyingContract
            },
            Types = MemberDescriptionFactory.GetTypesMemberDescription(typeof(Domain), typeof(PolymarketOrder)),
            PrimaryType = "Order"
        };

        var signer = new Eip712TypedDataSigner();
        return signer.SignTypedDataV4(order, typedData, new EthECKey(_account.PrivateKey));
    }

    /// <summary>
    /// HMAC-SHA256 signature for L2 API authentication.
    /// Message = timestamp + method + path + body
    /// Key = base64url-decoded API secret
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
        byte[] saltBytes = new byte[32];
        RandomNumberGenerator.Fill(saltBytes);
        return new BigInteger(saltBytes, isUnsigned: true, isBigEndian: true);
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
