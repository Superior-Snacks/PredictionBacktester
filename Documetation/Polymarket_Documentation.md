> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Introduction

> Overview of the Polymarket APIs

The Polymarket API provides programmatic access to the world's largest prediction market. The platform is served by three separate APIs, each handling a different domain.

***

## APIs

<CardGroup cols={1}>
  <Card title="Gamma API" icon="database">
    **`https://gamma-api.polymarket.com`**

    Markets, events, tags, series, comments, sports, search, and public profiles. This is the primary API for discovering and browsing market data.
  </Card>

  <Card title="Data API" icon="chart-line">
    **`https://data-api.polymarket.com`**

    User positions, trades, activity, holder data, open interest, leaderboards, and builder analytics.
  </Card>

  <Card title="CLOB API" icon="arrows-rotate">
    **`https://clob.polymarket.com`**

    Orderbook data, pricing, midpoints, spreads, and price history. Also handles order placement, cancellation, and other trading operations. Trading endpoints require [authentication](/api-reference/authentication).
  </Card>
</CardGroup>

<Info>
  A separate **Bridge API** (`https://bridge.polymarket.com`) handles deposits and withdrawals. Bridges are not handled by Polymarket, it is a proxy of fun.xyz service.
</Info>

***

## Authentication

The Gamma API and Data API are fully public — no authentication required.

The CLOB API has both public endpoints (orderbook, prices) and authenticated endpoints (order management). See [Authentication](/api-reference/authentication) for details.

***

## Next Steps

<CardGroup cols={2}>
  <Card title="Authentication" icon="key" href="/api-reference/authentication">
    Learn how to authenticate requests for trading endpoints.
  </Card>

  <Card title="Clients & SDKs" icon="cube" href="/api-reference/clients-sdks">
    Official TypeScript, Python, and Rust libraries.
  </Card>
</CardGroup>
> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Authentication

> How to authenticate requests to the CLOB API

The CLOB API uses two levels of authentication: **L1 (Private Key)** and **L2 (API Key)**. Either can be accomplished using the CLOB client or REST API.

## Public vs Authenticated

<CardGroup cols={1}>
  <Card title="Public (No Auth)" icon="unlock">
    The **Gamma API**, **Data API**, and CLOB read endpoints (orderbook, prices, spreads) require no authentication.
  </Card>

  <Card title="Authenticated (CLOB)" icon="lock">
    CLOB trading endpoints (placing orders, cancellations, heartbeat) require all 5 `POLY_*` L2 HTTP headers.
  </Card>
</CardGroup>

***

## Two-Level Authentication Model

The CLOB uses two levels of authentication: L1 (Private Key) and L2 (API Key). Either can be accomplished using the CLOB client or REST API

### L1 Authentication

L1 authentication uses the wallet's private key to sign an EIP-712 message used in the request header. It proves ownership and control over the private key. The private key stays in control of the user and all trading activity remains non-custodial.

**Used for:**

* Creating API credentials
* Deriving existing API credentials
* Signing and creating user's orders locally

### L2 Authentication

L2 uses API credentials (apiKey, secret, passphrase) generated from L1 authentication. These are used solely to authenticate requests made to the CLOB API. Requests are signed using HMAC-SHA256.

**Used for:**

* Cancel or get user's open orders
* Check user's balances and allowances
* Post user's signed orders

<Info>
  Even with L2 authentication headers, methods that create user orders still
  require the user to sign the order payload.
</Info>

***

## Getting API Credentials

Before making authenticated requests, you need to obtain API credentials using L1 authentication.

### Using the SDK

<Tabs>
  <Tab title="TypeScript">
    ```typescript theme={null}
    import { ClobClient } from "@polymarket/clob-client-v2";
    import { createWalletClient, http } from "viem";
    import { privateKeyToAccount } from "viem/accounts";

    const account = privateKeyToAccount(process.env.PRIVATE_KEY as `0x${string}`);
    const signer = createWalletClient({ account, transport: http() });

    const client = new ClobClient({
      host: "https://clob.polymarket.com",
      chain: 137, // Polygon mainnet
      signer,
    });

    // Creates new credentials or derives existing ones
    const credentials = await client.createOrDeriveApiKey();

    console.log(credentials);
    // {
    //   key: "550e8400-e29b-41d4-a716-446655440000",
    //   secret: "base64EncodedSecretString",
    //   passphrase: "randomPassphraseString"
    // }
    ```
  </Tab>

  <Tab title="Python">
    ```python theme={null}
    from py_clob_client_v2 import ClobClient
    import os

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,  # Polygon mainnet
        key=os.getenv("PRIVATE_KEY")
    )

    # Creates new credentials or derives existing ones
    credentials = client.create_or_derive_api_key()

    print(credentials)
    # {
    #     "apiKey": "550e8400-e29b-41d4-a716-446655440000",
    #     "secret": "base64EncodedSecretString",
    #     "passphrase": "randomPassphraseString"
    # }
    ```
  </Tab>

  <Tab title="Rust">
    ```rust theme={null}
    use std::str::FromStr;
    use polymarket_client_sdk_v2::POLYGON;
    use polymarket_client_sdk_v2::auth::{LocalSigner, Signer};
    use polymarket_client_sdk_v2::clob::{Client, Config};

    let private_key = std::env::var("POLYMARKET_PRIVATE_KEY")?;
    let signer = LocalSigner::from_str(&private_key)?
        .with_chain_id(Some(POLYGON));

    // Creates new credentials or derives existing ones,
    // then initializes the authenticated client — all in one step
    let client = Client::new("https://clob.polymarket.com", Config::default())?
        .authentication_builder(&signer)
        .authenticate()
        .await?;

    let credentials = client.credentials();
    println!("API Key: {}", credentials.key());
    ```
  </Tab>
</Tabs>

<Warning>
  **Never commit private keys to version control.** Always use environment
  variables or secure key management systems.
</Warning>

### Using the REST API

While we highly recommend using our provided clients to handle signing and authentication, the following is for developers who choose NOT to use our [Python](https://github.com/Polymarket/py-clob-client-v2) or [TypeScript](https://github.com/Polymarket/clob-client-v2) clients.

**Create API Credentials**

```bash theme={null}
POST https://clob.polymarket.com/auth/api-key
```

**Derive API Credentials**

```bash theme={null}
GET https://clob.polymarket.com/auth/derive-api-key
```

Required L1 headers:

| Header           | Description            |
| ---------------- | ---------------------- |
| `POLY_ADDRESS`   | Polygon signer address |
| `POLY_SIGNATURE` | CLOB EIP-712 signature |
| `POLY_TIMESTAMP` | Current UNIX timestamp |
| `POLY_NONCE`     | Nonce (default: 0)     |

The `POLY_SIGNATURE` is generated by signing the following EIP-712 struct:

<Accordion title="EIP-712 Signing Example">
  <CodeGroup>
    ```typescript TypeScript theme={null}
    const domain = {
      name: "ClobAuthDomain",
      version: "1",
      chainId: chainId, // Polygon Chain ID 137
    };

    const types = {
      ClobAuth: [
        { name: "address", type: "address" },
        { name: "timestamp", type: "string" },
        { name: "nonce", type: "uint256" },
        { name: "message", type: "string" },
      ],
    };

    const value = {
      address: signingAddress, // The Signing address
      timestamp: ts,            // The CLOB API server timestamp
      nonce: nonce,             // The nonce used
      message: "This message attests that I control the given wallet",
    };

    const sig = await signer._signTypedData(domain, types, value);
    ```

    ```python Python theme={null}
    domain = {
        "name": "ClobAuthDomain",
        "version": "1",
        "chainId": chainId,  # Polygon Chain ID 137
    }

    types = {
        "ClobAuth": [
            {"name": "address", "type": "address"},
            {"name": "timestamp", "type": "string"},
            {"name": "nonce", "type": "uint256"},
            {"name": "message", "type": "string"},
        ]
    }

    value = {
        "address": signingAddress,  # The signing address
        "timestamp": ts,            # The CLOB API server timestamp
        "nonce": nonce,             # The nonce used
        "message": "This message attests that I control the given wallet",
    }

    sig = signer.sign_typed_data(domain, types, value)
    ```
  </CodeGroup>
</Accordion>

Reference implementations:

* [TypeScript](https://github.com/Polymarket/clob-client-v2/blob/main/src/signing/eip712.ts)
* [Python](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/signing/eip712.py)

Response:

```json theme={null}
{
  "apiKey": "550e8400-e29b-41d4-a716-446655440000",
  "secret": "base64EncodedSecretString",
  "passphrase": "randomPassphraseString"
}
```

**You'll need all three values for L2 authentication.**

***

## L2 Authentication Headers

All trading endpoints require these 5 headers:

| Header            | Description                   |
| ----------------- | ----------------------------- |
| `POLY_ADDRESS`    | Polygon signer address        |
| `POLY_SIGNATURE`  | HMAC signature for request    |
| `POLY_TIMESTAMP`  | Current UNIX timestamp        |
| `POLY_API_KEY`    | User's API `apiKey` value     |
| `POLY_PASSPHRASE` | User's API `passphrase` value |

The `POLY_SIGNATURE` for L2 is an HMAC-SHA256 signature created using the user's API credentials `secret` value. Reference implementations can be found in the [TypeScript](https://github.com/Polymarket/clob-client-v2/blob/main/src/signing/hmac.ts) and [Python](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/signing/hmac.py) clients.

### CLOB Client

<Tabs>
  <Tab title="TypeScript">
    ```typescript theme={null}
    import { ClobClient, Side } from "@polymarket/clob-client-v2";
    import { createWalletClient, http } from "viem";
    import { privateKeyToAccount } from "viem/accounts";

    const account = privateKeyToAccount(process.env.PRIVATE_KEY as `0x${string}`);
    const signer = createWalletClient({ account, transport: http() });
    const depositWalletAddress = process.env.DEPOSIT_WALLET_ADDRESS!;

    const client = new ClobClient({
      host: "https://clob.polymarket.com",
      chain: 137,
      signer,
      creds: apiCreds, // Generated from L1 auth, API credentials enable L2 methods
      signatureType: 3, // POLY_1271, explained below
      funderAddress: depositWalletAddress, // deposit wallet funder
    });

    // Now you can trade!
    const order = await client.createAndPostOrder(
      { tokenID: "123456", price: 0.65, size: 100, side: Side.BUY },
      { tickSize: "0.01", negRisk: false }
    );
    ```
  </Tab>

  <Tab title="Python">
    ```python theme={null}
    from py_clob_client_v2 import ClobClient, OrderArgs, PartialCreateOrderOptions
    from py_clob_client_v2.order_builder.constants import BUY
    import os

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=os.getenv("PRIVATE_KEY"),
        creds=api_creds,  # Generated from L1 auth, API credentials enable L2 methods
        signature_type=3,  # POLY_1271, explained below
        funder=os.getenv("DEPOSIT_WALLET_ADDRESS")
    )

    # Now you can trade!
    order = client.create_and_post_order(
        OrderArgs(token_id="123456", price=0.65, size=100, side=BUY),
        options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
    )
    ```
  </Tab>

  <Tab title="Rust">
    ```rust theme={null}
    use polymarket_client_sdk_v2::clob::types::{Side, SignatureType};
    use polymarket_client_sdk_v2::types::dec;

    let deposit_wallet = std::env::var("DEPOSIT_WALLET_ADDRESS")?.parse()?;

    let client = Client::new("https://clob.polymarket.com", Config::default())?
        .authentication_builder(&signer)
        .funder(deposit_wallet)
        .signature_type(SignatureType::Poly1271)
        .authenticate()
        .await?;

    // Now you can trade!
    let order = client.limit_order()
        .token_id("123456".parse()?)
        .price(dec!(0.65))
        .size(dec!(100))
        .side(Side::Buy)
        .build().await?;
    let signed = client.sign(&signer, order).await?;
    let response = client.post_order(signed).await?;
    ```
  </Tab>
</Tabs>

<Info>
  Even with L2 authentication headers, methods that create user orders still
  require the user to sign the order payload.
</Info>

***

## Signature Types and Funder

When initializing the L2 client, you must specify your wallet **signatureType** and the **funder** address which holds the funds:

| Signature Type | Value | Description                                                                                                                |
| -------------- | ----- | -------------------------------------------------------------------------------------------------------------------------- |
| EOA            | `0`   | Standard Ethereum wallet (MetaMask). Funder is the EOA address and will need POL to pay gas on transactions.               |
| POLY\_PROXY    | `1`   | Existing Polymarket proxy wallet flow, commonly used by users who logged in via Magic Link email/Google.                   |
| GNOSIS\_SAFE   | `2`   | Existing Gnosis Safe wallet flow. Existing Safe users can continue using this type.                                        |
| POLY\_1271     | `3`   | Deposit wallet flow for new API users. The funder is the deposit wallet address and orders are validated through ERC-1271. |

<Tip>
  New API users should use deposit wallets with `POLY_1271`. Existing Safe and
  Proxy users are unaffected and can keep using their current funder address and
  signature type. See the [Deposit Wallet Guide](/trading/deposit-wallets) for
  setup details.
</Tip>

***

## Security Best Practices

<AccordionGroup>
  <Accordion title="Never expose private keys">
    Store private keys in environment variables or secure key management systems. Never commit them to version control.

    ```bash theme={null}
    # .env (never commit this file)
    PRIVATE_KEY=0x...
    ```
  </Accordion>

  <Accordion title="Implement request signing on the server">
    Never expose your API secret in client-side code. All authenticated requests should originate from your backend.
  </Accordion>
</AccordionGroup>

***

## Troubleshooting

<AccordionGroup>
  <Accordion title="Error - INVALID_SIGNATURE">
    Your wallet's private key is incorrect or improperly formatted.

    **Solutions:**

    * Verify your private key is a valid hex string (starts with "0x")
    * Ensure you're using the correct key for the intended address
    * Check that the key has proper permissions
  </Accordion>

  <Accordion title="Error - NONCE_ALREADY_USED">
    The nonce you provided has already been used to create an API key.

    **Solutions:**

    * Use `deriveApiKey()` with the same nonce to retrieve existing credentials
    * Or use a different nonce with `createApiKey()`
  </Accordion>

  <Accordion title="Error - Invalid Funder Address">
    Your funder address is incorrect or doesn't match your wallet.

    **Solution:** Check your Polymarket profile address at [polymarket.com/settings](https://polymarket.com/settings).

    If it does not exist or user has never logged into Polymarket.com, deploy it first before creating L2 authentication.
  </Accordion>

  <Accordion title="Lost both credentials and nonce">
    Unfortunately, there's no way to recover lost API credentials without the nonce. You'll need to create new credentials:

    ```typescript theme={null}
    // Create fresh credentials with a new nonce
    const newCreds = await client.createApiKey();
    // Save the nonce this time!
    ```
  </Accordion>
</AccordionGroup>

***

## Next Steps

<CardGroup cols={2}>
  <Card title="Place Your First Order" icon="plus" href="/trading/quickstart">
    Learn how to create and submit orders.
  </Card>

  <Card title="Geographic Restrictions" icon="globe" href="/api-reference/geoblock">
    Check trading availability by region.
  </Card>
</CardGroup>
> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Authentication

> How to authenticate requests to the CLOB API

The CLOB API uses two levels of authentication: **L1 (Private Key)** and **L2 (API Key)**. Either can be accomplished using the CLOB client or REST API.

## Public vs Authenticated

<CardGroup cols={1}>
  <Card title="Public (No Auth)" icon="unlock">
    The **Gamma API**, **Data API**, and CLOB read endpoints (orderbook, prices, spreads) require no authentication.
  </Card>

  <Card title="Authenticated (CLOB)" icon="lock">
    CLOB trading endpoints (placing orders, cancellations, heartbeat) require all 5 `POLY_*` L2 HTTP headers.
  </Card>
</CardGroup>

***

## Two-Level Authentication Model

The CLOB uses two levels of authentication: L1 (Private Key) and L2 (API Key). Either can be accomplished using the CLOB client or REST API

### L1 Authentication

L1 authentication uses the wallet's private key to sign an EIP-712 message used in the request header. It proves ownership and control over the private key. The private key stays in control of the user and all trading activity remains non-custodial.

**Used for:**

* Creating API credentials
* Deriving existing API credentials
* Signing and creating user's orders locally

### L2 Authentication

L2 uses API credentials (apiKey, secret, passphrase) generated from L1 authentication. These are used solely to authenticate requests made to the CLOB API. Requests are signed using HMAC-SHA256.

**Used for:**

* Cancel or get user's open orders
* Check user's balances and allowances
* Post user's signed orders

<Info>
  Even with L2 authentication headers, methods that create user orders still
  require the user to sign the order payload.
</Info>

***

## Getting API Credentials

Before making authenticated requests, you need to obtain API credentials using L1 authentication.

### Using the SDK

<Tabs>
  <Tab title="TypeScript">
    ```typescript theme={null}
    import { ClobClient } from "@polymarket/clob-client-v2";
    import { createWalletClient, http } from "viem";
    import { privateKeyToAccount } from "viem/accounts";

    const account = privateKeyToAccount(process.env.PRIVATE_KEY as `0x${string}`);
    const signer = createWalletClient({ account, transport: http() });

    const client = new ClobClient({
      host: "https://clob.polymarket.com",
      chain: 137, // Polygon mainnet
      signer,
    });

    // Creates new credentials or derives existing ones
    const credentials = await client.createOrDeriveApiKey();

    console.log(credentials);
    // {
    //   key: "550e8400-e29b-41d4-a716-446655440000",
    //   secret: "base64EncodedSecretString",
    //   passphrase: "randomPassphraseString"
    // }
    ```
  </Tab>

  <Tab title="Python">
    ```python theme={null}
    from py_clob_client_v2 import ClobClient
    import os

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,  # Polygon mainnet
        key=os.getenv("PRIVATE_KEY")
    )

    # Creates new credentials or derives existing ones
    credentials = client.create_or_derive_api_key()

    print(credentials)
    # {
    #     "apiKey": "550e8400-e29b-41d4-a716-446655440000",
    #     "secret": "base64EncodedSecretString",
    #     "passphrase": "randomPassphraseString"
    # }
    ```
  </Tab>

  <Tab title="Rust">
    ```rust theme={null}
    use std::str::FromStr;
    use polymarket_client_sdk_v2::POLYGON;
    use polymarket_client_sdk_v2::auth::{LocalSigner, Signer};
    use polymarket_client_sdk_v2::clob::{Client, Config};

    let private_key = std::env::var("POLYMARKET_PRIVATE_KEY")?;
    let signer = LocalSigner::from_str(&private_key)?
        .with_chain_id(Some(POLYGON));

    // Creates new credentials or derives existing ones,
    // then initializes the authenticated client — all in one step
    let client = Client::new("https://clob.polymarket.com", Config::default())?
        .authentication_builder(&signer)
        .authenticate()
        .await?;

    let credentials = client.credentials();
    println!("API Key: {}", credentials.key());
    ```
  </Tab>
</Tabs>

<Warning>
  **Never commit private keys to version control.** Always use environment
  variables or secure key management systems.
</Warning>

### Using the REST API

While we highly recommend using our provided clients to handle signing and authentication, the following is for developers who choose NOT to use our [Python](https://github.com/Polymarket/py-clob-client-v2) or [TypeScript](https://github.com/Polymarket/clob-client-v2) clients.

**Create API Credentials**

```bash theme={null}
POST https://clob.polymarket.com/auth/api-key
```

**Derive API Credentials**

```bash theme={null}
GET https://clob.polymarket.com/auth/derive-api-key
```

Required L1 headers:

| Header           | Description            |
| ---------------- | ---------------------- |
| `POLY_ADDRESS`   | Polygon signer address |
| `POLY_SIGNATURE` | CLOB EIP-712 signature |
| `POLY_TIMESTAMP` | Current UNIX timestamp |
| `POLY_NONCE`     | Nonce (default: 0)     |

The `POLY_SIGNATURE` is generated by signing the following EIP-712 struct:

<Accordion title="EIP-712 Signing Example">
  <CodeGroup>
    ```typescript TypeScript theme={null}
    const domain = {
      name: "ClobAuthDomain",
      version: "1",
      chainId: chainId, // Polygon Chain ID 137
    };

    const types = {
      ClobAuth: [
        { name: "address", type: "address" },
        { name: "timestamp", type: "string" },
        { name: "nonce", type: "uint256" },
        { name: "message", type: "string" },
      ],
    };

    const value = {
      address: signingAddress, // The Signing address
      timestamp: ts,            // The CLOB API server timestamp
      nonce: nonce,             // The nonce used
      message: "This message attests that I control the given wallet",
    };

    const sig = await signer._signTypedData(domain, types, value);
    ```

    ```python Python theme={null}
    domain = {
        "name": "ClobAuthDomain",
        "version": "1",
        "chainId": chainId,  # Polygon Chain ID 137
    }

    types = {
        "ClobAuth": [
            {"name": "address", "type": "address"},
            {"name": "timestamp", "type": "string"},
            {"name": "nonce", "type": "uint256"},
            {"name": "message", "type": "string"},
        ]
    }

    value = {
        "address": signingAddress,  # The signing address
        "timestamp": ts,            # The CLOB API server timestamp
        "nonce": nonce,             # The nonce used
        "message": "This message attests that I control the given wallet",
    }

    sig = signer.sign_typed_data(domain, types, value)
    ```
  </CodeGroup>
</Accordion>

Reference implementations:

* [TypeScript](https://github.com/Polymarket/clob-client-v2/blob/main/src/signing/eip712.ts)
* [Python](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/signing/eip712.py)

Response:

```json theme={null}
{
  "apiKey": "550e8400-e29b-41d4-a716-446655440000",
  "secret": "base64EncodedSecretString",
  "passphrase": "randomPassphraseString"
}
```

**You'll need all three values for L2 authentication.**

***

## L2 Authentication Headers

All trading endpoints require these 5 headers:

| Header            | Description                   |
| ----------------- | ----------------------------- |
| `POLY_ADDRESS`    | Polygon signer address        |
| `POLY_SIGNATURE`  | HMAC signature for request    |
| `POLY_TIMESTAMP`  | Current UNIX timestamp        |
| `POLY_API_KEY`    | User's API `apiKey` value     |
| `POLY_PASSPHRASE` | User's API `passphrase` value |

The `POLY_SIGNATURE` for L2 is an HMAC-SHA256 signature created using the user's API credentials `secret` value. Reference implementations can be found in the [TypeScript](https://github.com/Polymarket/clob-client-v2/blob/main/src/signing/hmac.ts) and [Python](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/signing/hmac.py) clients.

### CLOB Client

<Tabs>
  <Tab title="TypeScript">
    ```typescript theme={null}
    import { ClobClient, Side } from "@polymarket/clob-client-v2";
    import { createWalletClient, http } from "viem";
    import { privateKeyToAccount } from "viem/accounts";

    const account = privateKeyToAccount(process.env.PRIVATE_KEY as `0x${string}`);
    const signer = createWalletClient({ account, transport: http() });
    const depositWalletAddress = process.env.DEPOSIT_WALLET_ADDRESS!;

    const client = new ClobClient({
      host: "https://clob.polymarket.com",
      chain: 137,
      signer,
      creds: apiCreds, // Generated from L1 auth, API credentials enable L2 methods
      signatureType: 3, // POLY_1271, explained below
      funderAddress: depositWalletAddress, // deposit wallet funder
    });

    // Now you can trade!
    const order = await client.createAndPostOrder(
      { tokenID: "123456", price: 0.65, size: 100, side: Side.BUY },
      { tickSize: "0.01", negRisk: false }
    );
    ```
  </Tab>

  <Tab title="Python">
    ```python theme={null}
    from py_clob_client_v2 import ClobClient, OrderArgs, PartialCreateOrderOptions
    from py_clob_client_v2.order_builder.constants import BUY
    import os

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=os.getenv("PRIVATE_KEY"),
        creds=api_creds,  # Generated from L1 auth, API credentials enable L2 methods
        signature_type=3,  # POLY_1271, explained below
        funder=os.getenv("DEPOSIT_WALLET_ADDRESS")
    )

    # Now you can trade!
    order = client.create_and_post_order(
        OrderArgs(token_id="123456", price=0.65, size=100, side=BUY),
        options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
    )
    ```
  </Tab>

  <Tab title="Rust">
    ```rust theme={null}
    use polymarket_client_sdk_v2::clob::types::{Side, SignatureType};
    use polymarket_client_sdk_v2::types::dec;

    let deposit_wallet = std::env::var("DEPOSIT_WALLET_ADDRESS")?.parse()?;

    let client = Client::new("https://clob.polymarket.com", Config::default())?
        .authentication_builder(&signer)
        .funder(deposit_wallet)
        .signature_type(SignatureType::Poly1271)
        .authenticate()
        .await?;

    // Now you can trade!
    let order = client.limit_order()
        .token_id("123456".parse()?)
        .price(dec!(0.65))
        .size(dec!(100))
        .side(Side::Buy)
        .build().await?;
    let signed = client.sign(&signer, order).await?;
    let response = client.post_order(signed).await?;
    ```
  </Tab>
</Tabs>

<Info>
  Even with L2 authentication headers, methods that create user orders still
  require the user to sign the order payload.
</Info>

***

## Signature Types and Funder

When initializing the L2 client, you must specify your wallet **signatureType** and the **funder** address which holds the funds:

| Signature Type | Value | Description                                                                                                                |
| -------------- | ----- | -------------------------------------------------------------------------------------------------------------------------- |
| EOA            | `0`   | Standard Ethereum wallet (MetaMask). Funder is the EOA address and will need POL to pay gas on transactions.               |
| POLY\_PROXY    | `1`   | Existing Polymarket proxy wallet flow, commonly used by users who logged in via Magic Link email/Google.                   |
| GNOSIS\_SAFE   | `2`   | Existing Gnosis Safe wallet flow. Existing Safe users can continue using this type.                                        |
| POLY\_1271     | `3`   | Deposit wallet flow for new API users. The funder is the deposit wallet address and orders are validated through ERC-1271. |

<Tip>
  New API users should use deposit wallets with `POLY_1271`. Existing Safe and
  Proxy users are unaffected and can keep using their current funder address and
  signature type. See the [Deposit Wallet Guide](/trading/deposit-wallets) for
  setup details.
</Tip>

***

## Security Best Practices

<AccordionGroup>
  <Accordion title="Never expose private keys">
    Store private keys in environment variables or secure key management systems. Never commit them to version control.

    ```bash theme={null}
    # .env (never commit this file)
    PRIVATE_KEY=0x...
    ```
  </Accordion>

  <Accordion title="Implement request signing on the server">
    Never expose your API secret in client-side code. All authenticated requests should originate from your backend.
  </Accordion>
</AccordionGroup>

***

## Troubleshooting

<AccordionGroup>
  <Accordion title="Error - INVALID_SIGNATURE">
    Your wallet's private key is incorrect or improperly formatted.

    **Solutions:**

    * Verify your private key is a valid hex string (starts with "0x")
    * Ensure you're using the correct key for the intended address
    * Check that the key has proper permissions
  </Accordion>

  <Accordion title="Error - NONCE_ALREADY_USED">
    The nonce you provided has already been used to create an API key.

    **Solutions:**

    * Use `deriveApiKey()` with the same nonce to retrieve existing credentials
    * Or use a different nonce with `createApiKey()`
  </Accordion>

  <Accordion title="Error - Invalid Funder Address">
    Your funder address is incorrect or doesn't match your wallet.

    **Solution:** Check your Polymarket profile address at [polymarket.com/settings](https://polymarket.com/settings).

    If it does not exist or user has never logged into Polymarket.com, deploy it first before creating L2 authentication.
  </Accordion>

  <Accordion title="Lost both credentials and nonce">
    Unfortunately, there's no way to recover lost API credentials without the nonce. You'll need to create new credentials:

    ```typescript theme={null}
    // Create fresh credentials with a new nonce
    const newCreds = await client.createApiKey();
    // Save the nonce this time!
    ```
  </Accordion>
</AccordionGroup>

***

## Next Steps

<CardGroup cols={2}>
  <Card title="Place Your First Order" icon="plus" href="/trading/quickstart">
    Learn how to create and submit orders.
  </Card>

  <Card title="Geographic Restrictions" icon="globe" href="/api-reference/geoblock">
    Check trading availability by region.
  </Card>
</CardGroup>
> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Clients & SDKs

> Official open-source libraries for interacting with Polymarket

Polymarket provides official open-source clients in TypeScript, Python, and Rust. All three support the full CLOB API including market data, order management, and authentication.

## Installation

<CodeGroup>
  ```bash TypeScript theme={null}
  npm install @polymarket/clob-client-v2 viem
  ```

  ```bash Python theme={null}
  pip install py-clob-client-v2
  ```

  ```bash Rust theme={null}
  cargo add polymarket_client_sdk_v2 --features clob
  ```
</CodeGroup>

## Quick Example

<CodeGroup>
  ```typescript TypeScript theme={null}
  import { ClobClient } from "@polymarket/clob-client-v2";

  const client = new ClobClient({
    host: "https://clob.polymarket.com",
    chain: 137,
    signer,
    creds: apiCreds,
  });

  const markets = await client.getMarkets();
  ```

  ```python Python theme={null}
  from py_clob_client_v2 import ClobClient

  client = ClobClient(
      "https://clob.polymarket.com",
      key=private_key,
      chain_id=137,
      creds=api_creds,
  )

  markets = client.get_markets()
  ```

  ```rust Rust theme={null}
  use polymarket_client_sdk_v2::clob::{Client, Config};

  let client = Client::new("https://clob.polymarket.com", Config::default())?
      .authentication_builder(&signer)
      .authenticate()
      .await?;

  let markets = client.markets(None).await?;
  ```
</CodeGroup>

## Source Code

| Language   | Package                      | Repository                                                                                 |
| ---------- | ---------------------------- | ------------------------------------------------------------------------------------------ |
| TypeScript | `@polymarket/clob-client-v2` | [github.com/Polymarket/clob-client-v2](https://github.com/Polymarket/clob-client-v2)       |
| Python     | `py-clob-client-v2`          | [github.com/Polymarket/py-clob-client-v2](https://github.com/Polymarket/py-clob-client-v2) |
| Rust       | `polymarket_client_sdk_v2`   | [github.com/Polymarket/rs-clob-client-v2](https://github.com/Polymarket/rs-clob-client-v2) |

Each repository includes working examples in the `/examples` directory.

## Relayer SDK

For [gasless transactions](/trading/gasless), the relayer client handles deposit
wallet creation and signed wallet batches for new API users. Existing Safe and
Proxy wallet flows remain supported.

| Language   | Package                              | Repository                                                                                                 |
| ---------- | ------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| TypeScript | `@polymarket/builder-relayer-client` | [github.com/Polymarket/builder-relayer-client](https://github.com/Polymarket/builder-relayer-client)       |
| Python     | `py-builder-relayer-client`          | [github.com/Polymarket/py-builder-relayer-client](https://github.com/Polymarket/py-builder-relayer-client) |

## Next Steps

<CardGroup cols={2}>
  <Card title="Quickstart" icon="rocket" href="/quickstart">
    Set up your client and place your first order.
  </Card>

  <Card title="Authentication" icon="lock" href="/api-reference/authentication">
    Understand L1/L2 auth and API credentials.
  </Card>
</CardGroup>
> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Geographic Restrictions

> Check geographic restrictions before placing orders on the Polymarket API

Polymarket restricts order placement from certain geographic locations due to regulatory requirements and compliance with international sanctions. Before placing orders, builders should verify the location.

<Warning>
  Orders submitted from blocked regions will be rejected. Implement geoblock
  checks in your application to provide users with appropriate feedback before
  they attempt to trade.
</Warning>

***

## Geoblock Endpoint

Check the geographic eligibility of the requesting IP address:

```bash theme={null}
GET https://polymarket.com/api/geoblock
```

<Note>This endpoint is on `polymarket.com`, not the API servers.</Note>

### Response

```json theme={null}
{
  "blocked": true,
  "ip": "203.0.113.42",
  "country": "US",
  "region": "NY"
}
```

| Field     | Type    | Description                                     |
| --------- | ------- | ----------------------------------------------- |
| `blocked` | boolean | Whether the user is blocked from placing orders |
| `ip`      | string  | Detected IP address                             |
| `country` | string  | ISO 3166-1 alpha-2 country code                 |
| `region`  | string  | Region/state code                               |

***

## Blocked Countries

The following countries are restricted from placing orders on Polymarket. Countries marked as **close-only** can close existing positions but cannot open new ones. Countries marked as **frontend UI restricted** are blocked only on the Polymarket frontend; the API itself is not restricted:

| Country Code | Country Name                         | Status                 |
| ------------ | ------------------------------------ | ---------------------- |
| AU           | Australia                            | Blocked                |
| BE           | Belgium                              | Blocked                |
| BY           | Belarus                              | Blocked                |
| BI           | Burundi                              | Blocked                |
| CF           | Central African Republic             | Blocked                |
| CD           | Congo (Kinshasa)                     | Blocked                |
| CU           | Cuba                                 | Blocked                |
| DE           | Germany                              | Blocked                |
| ET           | Ethiopia                             | Blocked                |
| FR           | France                               | Blocked                |
| GB           | United Kingdom                       | Blocked                |
| IR           | Iran                                 | Blocked                |
| IQ           | Iraq                                 | Blocked                |
| IT           | Italy                                | Blocked                |
| JP           | Japan                                | Frontend UI restricted |
| KP           | North Korea                          | Blocked                |
| LB           | Lebanon                              | Blocked                |
| LY           | Libya                                | Blocked                |
| MM           | Myanmar                              | Blocked                |
| NI           | Nicaragua                            | Blocked                |
| NL           | Netherlands                          | Blocked                |
| PL           | Poland                               | Close-only             |
| RU           | Russia                               | Blocked                |
| SG           | Singapore                            | Close-only             |
| SO           | Somalia                              | Blocked                |
| SS           | South Sudan                          | Blocked                |
| SD           | Sudan                                | Blocked                |
| SY           | Syria                                | Blocked                |
| TH           | Thailand                             | Close-only             |
| TW           | Taiwan                               | Close-only             |
| UM           | United States Minor Outlying Islands | Blocked                |
| US           | United States                        | Blocked                |
| VE           | Venezuela                            | Blocked                |
| YE           | Yemen                                | Blocked                |
| ZW           | Zimbabwe                             | Blocked                |

***

## Blocked Regions

In addition to fully blocked countries, the following specific regions within otherwise accessible countries are also restricted:

| Country      | Region  | Region Code |
| ------------ | ------- | ----------- |
| Canada (CA)  | Ontario | ON          |
| Ukraine (UA) | Crimea  | 43          |
| Ukraine (UA) | Donetsk | 14          |
| Ukraine (UA) | Luhansk | 09          |

***

## Blocking Logic

The geoblocking system includes:

1. **OFAC-Sanctioned Countries**: Countries sanctioned by the U.S. Office of Foreign Assets Control (OFAC)
2. **Additional Regulatory Restrictions**: Countries added for specific regulatory compliance reasons

***

## Server Infrastructure

* **Primary Servers**: eu-west-2
* **Closest Non-Georestricted Region**: eu-west-1

<Tip>
  **Direct co-location available.** Users who complete the [KYC/KYB
  form](https://docs.google.com/forms/d/e/1FAIpQLSfY-3Dl3yxq8HKFjFad8YzKZmm0k3Gdg29HD6gL-K-AmI6KXw/viewform) can get access to co-locate
  directly in `eu-west-2` for the lowest possible latency to Polymarket's
  primary servers.
</Tip>

***

## Usage Examples

<Tabs>
  <Tab title="TypeScript">
    ```typescript theme={null}
    interface GeoblockResponse {
      blocked: boolean;
      ip: string;
      country: string;
      region: string;
    }

    async function checkGeoblock(): Promise<GeoblockResponse> {
      const response = await fetch("https://polymarket.com/api/geoblock");
      return response.json();
    }

    // Usage
    const geo = await checkGeoblock();

    if (geo.blocked) {
      console.log(`Trading not available in ${geo.country}`);
    } else {
      console.log("Trading available");
    }
    ```
  </Tab>

  <Tab title="Python">
    ```python theme={null}
    import requests

    def check_geoblock() -> dict:
        response = requests.get("https://polymarket.com/api/geoblock")
        return response.json()

    # Usage
    geo = check_geoblock()

    if geo["blocked"]:
        print(f"Trading not available in {geo['country']}")
    else:
        print("Trading available")
    ```
  </Tab>

  <Tab title="Rust">
    ```rust theme={null}
    use polymarket_client_sdk_v2::clob::{Client, Config};

    let client = Client::new("https://clob.polymarket.com", Config::default())?;
    let geo = client.check_geoblock().await?;

    if geo.blocked {
        println!("Trading not available in {}", geo.country);
    } else {
        println!("Trading available");
    }
    ```
  </Tab>
</Tabs>

***

## Why These Restrictions

Geographic restrictions are implemented to ensure compliance with:

* International sanctions and embargoes
* Local financial regulations
* Gambling and prediction market laws
* Anti-money laundering (AML) requirements
* Know Your Customer (KYC) regulations

If you believe you are incorrectly restricted or have questions about geographic availability, please contact [Polymarket Support](https://polymarket.com/support).

***

## Next Steps

<CardGroup cols={2}>
  <Card title="Authentication" icon="key" href="/api-reference/authentication">
    Learn how to authenticate trading requests.
  </Card>

  <Card title="Place Orders" icon="plus" href="/trading/quickstart">
    Start placing orders (from eligible regions).
  </Card>
</CardGroup>
> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Overview

> Learn about Polymarket's developer tooling roadmap.

We are improving Polymarket's developer integration surface across SDKs, APIs, and frontend tooling. The unified TypeScript and Python SDKs are the first beta release in this effort and are currently being hardened before a stable release. Once stable, these SDKs will supersede the existing SDKs, and we will provide a documented migration path.

<Steps>
  <Step title="Beta TypeScript and Python SDKs">
    Current beta release of the unified SDKs for early integrators.
  </Step>

  <Step title="Stable TypeScript and Python SDKs">
    Next, harden the beta SDKs, resolve feedback from integrators, and publish
    the documented migration path from the existing SDKs.
  </Step>

  <Step title="Unified API">
    A cohesive API surface across Polymarket developer interfaces.
  </Step>

  <Step title="Rust SDK">
    A unified SDK for systems-level integrations and backend services.
  </Step>

  <Step title="React SDK">
    Frontend-oriented tooling for React applications on top of the same unified
    model.
  </Step>
</Steps>

<CardGroup cols={2}>
  <Card title="TypeScript SDK" href="/dev-tooling/typescript">
    Beta documentation for the unified TypeScript SDK.
  </Card>

  <Card title="Python SDK" href="/dev-tooling/python">
    Beta documentation for the unified Python SDK.
  </Card>
</CardGroup>
> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Overview

> Learn about Polymarket's developer tooling roadmap.

We are improving Polymarket's developer integration surface across SDKs, APIs, and frontend tooling. The unified TypeScript and Python SDKs are the first beta release in this effort and are currently being hardened before a stable release. Once stable, these SDKs will supersede the existing SDKs, and we will provide a documented migration path.

<Steps>
  <Step title="Beta TypeScript and Python SDKs">
    Current beta release of the unified SDKs for early integrators.
  </Step>

  <Step title="Stable TypeScript and Python SDKs">
    Next, harden the beta SDKs, resolve feedback from integrators, and publish
    the documented migration path from the existing SDKs.
  </Step>

  <Step title="Unified API">
    A cohesive API surface across Polymarket developer interfaces.
  </Step>

  <Step title="Rust SDK">
    A unified SDK for systems-level integrations and backend services.
  </Step>

  <Step title="React SDK">
    Frontend-oriented tooling for React applications on top of the same unified
    model.
  </Step>
</Steps>

<CardGroup cols={2}>
  <Card title="TypeScript SDK" href="/dev-tooling/typescript">
    Beta documentation for the unified TypeScript SDK.
  </Card>

  <Card title="Python SDK" href="/dev-tooling/python">
    Beta documentation for the unified Python SDK.
  </Card>
</CardGroup>
> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Python SDK

> Build with the unified Polymarket Python SDK.

The unified Python SDK gives you a consistent surface across Polymarket discovery, market data, trading, account data, and realtime streams.

<Note>
  The Python SDK is currently in beta. We are keeping it in this beta phase
  while we address issues and harden the SDK before transitioning to a more
  stable release.
</Note>

The SDK ships parallel async and sync clients with matching method names and arguments: `AsyncPublicClient` / `PublicClient` for public reads, and `AsyncSecureClient` / `SecureClient` for authenticated reads and trading. Prefer the async clients for servers, bots, and any code that already runs inside an event loop. Use the sync clients for scripts, notebooks, and one-off tools where an event loop would just add ceremony. Realtime stream subscriptions are async only and require the async clients.

Examples below show the body of an `async def main()` function; wrap them with `asyncio.run(main())` to run as a script, as shown in [Quickstart](#quickstart). To switch a snippet to sync, swap `AsyncPublicClient` / `AsyncSecureClient` for `PublicClient` / `SecureClient`, drop `await`, replace `async with` with `with`, replace `async for` with `for`, and remove the `asyncio.run(...)` wrapper.

## Quickstart

<Steps>
  <Step title="Install the Package">
    Install the SDK from PyPI.

    <CodeGroup>
      ```bash uv theme={null}
      uv add polymarket-client
      ```

      ```bash pip theme={null}
      pip install polymarket-client
      ```

      ```bash poetry theme={null}
      poetry add polymarket-client
      ```
    </CodeGroup>
  </Step>

  <Step title="Create a Public Client">
    Create an instance of the `AsyncPublicClient` inside an `async with` block so its network transports are released when you are done.

    ```python theme={null}
    from polymarket import AsyncPublicClient

    async with AsyncPublicClient() as client:
        ...
    ```
  </Step>

  <Step title="Fetch Markets">
    Fetch a page of markets to discover active trading opportunities.

    <CodeGroup>
      ```python Async theme={null}
      import asyncio

      from polymarket import AsyncPublicClient


      async def main() -> None:
          async with AsyncPublicClient() as client:
              markets = client.list_markets(closed=False, page_size=5)
              first_page = await markets.first_page()

              for market in first_page.items:
                  # market: Market
                  ...


      asyncio.run(main())
      ```

      ```python Sync theme={null}
      from polymarket import PublicClient


      with PublicClient() as client:
          markets = client.list_markets(closed=False, page_size=5)
          first_page = markets.first_page()

          for market in first_page.items:
              # market: Market
              ...
      ```
    </CodeGroup>
  </Step>
</Steps>

## SDK Patterns

The SDK uses consistent patterns for pagination, Python-native model values, and structured SDK exceptions across public and authenticated workflows.

### Typed Primitives

Identifiers and EVM addresses are exposed as `typing.NewType` aliases (`MarketId`, `ConditionId`, `TokenId`, `EventId`, `EvmAddress`, …) so static type checkers can keep them distinct from plain strings. Precision-sensitive price, size, and amount fields generally use `decimal.Decimal`; date and time fields use `datetime.date` or `datetime.datetime`.

```python theme={null}
from datetime import datetime
from decimal import Decimal

from polymarket import ConditionId, EvmAddress, MarketId, TokenId


class Market:
    id: MarketId
    condition_id: ConditionId | None
    state: MarketState
    outcomes: MarketOutcomes
    resolution: MarketResolution
    # …


class MarketState:
    start_date: datetime | None
    end_date: datetime | None
    # …


class MarketOutcome:
    label: str
    token_id: TokenId | None
    price: Decimal | None


class MarketResolution:
    resolved_by: EvmAddress | None
    # …
```

### Market and Event Data

Market and event responses are returned as SDK models with snake\_case fields and nested submodels.

<CodeGroup>
  ```python Market theme={null}
  class Market:
      id: MarketId
      slug: str | None
      condition_id: ConditionId | None
      question: str | None
      description: str | None
      category: str | None
      image: str | None
      icon: str | None
      state: MarketState
      outcomes: MarketOutcomes
      metrics: MarketMetrics
      prices: MarketPrices
      trading: MarketTrading
      resolution: MarketResolution
      rewards: MarketRewards
      sports: MarketSportsMetadata
      tags: tuple[MarketTag, ...]
      # …
  ```

  ```python Event theme={null}
  class Event:
      id: EventId
      slug: str | None
      title: str | None
      subtitle: str | None
      description: str | None
      category: str | None
      subcategory: str | None
      image: str | None
      icon: str | None
      created_at: datetime | None
      updated_at: datetime | None
      published_at: datetime | None
      state: EventState
      schedule: EventSchedule
      metrics: EventMetrics
      trading: EventTrading
      estimation: EventEstimation
      sports: EventSportsMetadata
      partners: tuple[EventPartner, ...]
      markets: tuple[Market, ...]
      series: tuple[EventSeries, ...]
      # …
  ```
</CodeGroup>

### Environment Configuration

Production is the default environment. Pass an `Environment` object when your integration needs to target a different deployment or custom endpoint set. The client owns network transports, so use `async with` (or call `await client.close()`) to release them when you are done.

```python theme={null}
from polymarket import AsyncPublicClient, PRODUCTION


async with AsyncPublicClient(environment=PRODUCTION) as client:
    ...
```

### Pagination

With async clients, list methods return an `AsyncPaginator` across paginated endpoints. Use `async for` to iterate through pages.

```python theme={null}
async with AsyncPublicClient() as client:
    markets = client.list_markets(closed=False, page_size=10)

    async for page in markets:
        # page.items: tuple[Market, ...]
        ...
```

You can also fetch the first page directly and resume later from a cursor.

```python theme={null}
first_page = await markets.first_page()
# first_page.items: tuple[Market, ...]

async for page in markets.from_cursor(first_page.next_cursor):
    # page.items: tuple[Market, ...]
    ...
```

When you only care about the items and not page boundaries, iterate them directly.

```python theme={null}
async for market in markets.items():
    # market: Market
    ...
```

### Error Handling

All SDK exceptions inherit from `PolymarketError`. Catch specific subclasses to handle known cases, and catch `PolymarketError` as the final SDK fallback.

<Note>
  Catching `PolymarketError` last ensures error subclasses added in future SDK
  releases do not pass through unhandled.
</Note>

```python theme={null}
from polymarket import (
    AsyncPublicClient,
    PolymarketError,
    RateLimitError,
    UserInputError,
)

async with AsyncPublicClient() as client:
    try:
        markets = client.list_markets(closed=False, page_size=10)
        first_page = await markets.first_page()
        # first_page.items: tuple[Market, ...]
    except RateLimitError:
        # Retry later.
        ...
    except UserInputError:
        # Fix the request parameters.
        ...
    except PolymarketError:
        # Handle any other SDK error.
        ...
```

## Market Data

Use market data methods to fetch market and event details, order books, current prices, historical prices, and batch quotes.

<CodeGroup>
  ```python Market theme={null}
  market = await client.get_market(
      url="https://polymarket.com/market/eth-flipped-in-2026",
  )

  market_by_slug = await client.get_market(slug="eth-flipped-in-2026")

  market_by_id = await client.get_market(id="12345")
  ```

  ```python Event theme={null}
  event = await client.get_event(
      url="https://polymarket.com/event/presidential-election-2028",
  )

  event_by_slug = await client.get_event(slug="presidential-election-2028")

  event_by_id = await client.get_event(id="12345")
  ```
</CodeGroup>

Then fetch related tags, order books, prices, and history.

<CodeGroup>
  ```python Tags theme={null}
  market_tags = await client.get_market_tags(market.id)

  event_tags = await client.get_event_tags(event.id)
  ```

  ```python Order Book theme={null}
  yes_token_id = market.outcomes.yes.token_id
  if yes_token_id is None:
      raise RuntimeError("Market does not have a YES token id")

  book = await client.get_order_book(token_id=yes_token_id)
  ```

  ```python Prices theme={null}
  yes_token_id = market.outcomes.yes.token_id
  if yes_token_id is None:
      raise RuntimeError("Market does not have a YES token id")

  buy_price = await client.get_price(token_id=yes_token_id, side="BUY")

  midpoint = await client.get_midpoint(token_id=yes_token_id)

  spread = await client.get_spread(token_id=yes_token_id)

  last_trade = await client.get_last_trade_price(token_id=yes_token_id)
  ```

  ```python History theme={null}
  yes_token_id = market.outcomes.yes.token_id
  if yes_token_id is None:
      raise RuntimeError("Market does not have a YES token id")

  history = await client.get_price_history(token_id=yes_token_id, interval="1d")
  ```

  ```python Batch Reads theme={null}
  from polymarket import PriceRequest

  yes_token_id = market.outcomes.yes.token_id
  no_token_id = market.outcomes.no.token_id
  if yes_token_id is None or no_token_id is None:
      raise RuntimeError("Market does not have both outcome token ids")

  prices = await client.get_prices(
      requests=[
          PriceRequest(token_id=yes_token_id, side="BUY"),
          PriceRequest(token_id=no_token_id, side="BUY"),
      ],
  )

  midpoints = await client.get_midpoints(token_ids=[yes_token_id, no_token_id])
  ```
</CodeGroup>

## Discovery

Use discovery methods to browse events, markets, teams, tags, comments, sports metadata, and search results. The examples below show a few common entry points.

<Tabs>
  <Tab title="Events">
    ```python theme={null}
    events = client.list_events(page_size=10)

    async for page in events:
        # page.items: tuple[Event, ...]
        ...
    ```
  </Tab>

  <Tab title="Markets">
    ```python theme={null}
    markets = client.list_markets(closed=False, page_size=10)

    async for page in markets:
        # page.items: tuple[Market, ...]
        ...
    ```
  </Tab>

  <Tab title="Teams">
    ```python theme={null}
    teams = client.list_teams(league="NBA", page_size=10)

    async for page in teams:
        # page.items: tuple[Team, ...]
        ...
    ```
  </Tab>

  <Tab title="Tags">
    ```python theme={null}
    tags = client.list_tags(page_size=10)

    async for page in tags:
        # page.items: tuple[Tag, ...]
        ...

    tag = await client.get_tag(slug="politics")

    related_tags = await client.get_related_tags(slug="politics")

    related_resources = await client.get_related_tag_resources(
        slug="politics",
        status="active",
    )
    ```
  </Tab>

  <Tab title="Comments">
    ```python theme={null}
    import os

    comments = client.list_comments(
        parent_entity_id="12345",
        parent_entity_type="Event",
        page_size=20,
    )

    async for page in comments:
        # page.items: tuple[Comment, ...]
        ...

    thread = await client.get_comment_thread("456", get_positions=True)

    user_comments = client.list_comments_by_user_address(
        address=os.environ["POLYMARKET_TARGET_WALLET_ADDRESS"],
        page_size=10,
        order="DESC",
    )

    async for page in user_comments:
        # page.items: tuple[Comment, ...]
        ...
    ```
  </Tab>

  <Tab title="Sports">
    ```python theme={null}
    sports = await client.get_sports()

    # sports: tuple[SportsMetadata, ...]
    ```
  </Tab>

  <Tab title="Search">
    ```python theme={null}
    results = client.search(q="ethereum", page_size=10)

    async for page in results:
        for search_results in page.items:
            # search_results.events: tuple[Event, ...]
            # search_results.tags: tuple[SearchTag, ...]
            # search_results.profiles: tuple[Profile, ...]
            ...
    ```
  </Tab>
</Tabs>

## Realtime Streams

Subscribe through one SDK interface even when events come from different stream families. The SDK routes each subscription spec to the right stream and merges the results into one async iterator. Subscriptions are async only and require `AsyncPublicClient` or `AsyncSecureClient`.

```python theme={null}
from polymarket import AsyncPublicClient
from polymarket.streams import CryptoPricesSpec, MarketSpec


yes_token_id = market.outcomes.yes.token_id
if yes_token_id is None:
    raise RuntimeError("Market does not have a YES token id")

async with AsyncPublicClient() as client:
    stream = await client.subscribe(
        [
            MarketSpec(token_ids=[yes_token_id]),
            CryptoPricesSpec(
                topic="prices.crypto.binance",
                symbols=["btcusdt"],
            ),
        ],
    )

    async with stream:
        async for event in stream:
            # event:
            #   | MarketBookEvent
            #   | MarketPriceChangeEvent
            #   | MarketLastTradePriceEvent
            #   | MarketTickSizeChangeEvent
            #   | MarketBestBidAskEvent
            #   | NewMarketEvent
            #   | MarketResolvedEvent
            #   | CryptoPricesBinanceEvent
            print(type(event).__name__)
            break
```

`AsyncSecureClient.subscribe` accepts the same public subscription specs and adds `UserSpec` for user-scoped order and trade events on the authenticated wallet.

```python theme={null}
import os

from polymarket import AsyncSecureClient
from polymarket.streams import UserSpec


async with await AsyncSecureClient.create(
    private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
    wallet=os.environ.get("POLYMARKET_WALLET_ADDRESS"),
) as secure_client:
    user_stream = await secure_client.subscribe(UserSpec())

    async with user_stream:
        async for event in user_stream:
            # event:
            #   | UserOrderEvent
            #   | UserTradeEvent
            print(type(event).__name__)
            break
```

## Authenticated Client

Create a secure client when you need wallet-scoped reads or trading.

<Note>
  Secure clients own multiple network transports. Wrap them in `async with`,
  or call `await secure_client.close()` when you are done, to release the
  underlying connections. The snippets below show client creation and
  subsequent calls as a flat sequence for readability — in real code, keep
  the client inside an `async with` block or close it explicitly.
</Note>

### Private Key Setup

The Python SDK authenticates with a local private key. Pass `wallet` when you want to operate on a Polymarket wallet address that differs from the signer address; if omitted, the client uses the signer address as the account wallet.

```python theme={null}
import os

from polymarket import AsyncSecureClient


async with await AsyncSecureClient.create(
    private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
    wallet=os.environ.get("POLYMARKET_WALLET_ADDRESS"),
) as secure_client:
    ...
```

Keep private keys and API credentials in your secret manager or local environment. Do not commit them to source control.

### API Key Authentication

Configure an API key when the SDK needs to set up gasless wallet operations.

<CodeGroup>
  ```python Relayer API Key theme={null}
  import os

  from polymarket import AsyncSecureClient, RelayerApiKey


  secure_client = await AsyncSecureClient.create(
      private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
      wallet=os.environ.get("POLYMARKET_WALLET_ADDRESS"),
      api_key=RelayerApiKey(
          key=os.environ["POLYMARKET_RELAYER_API_KEY"],
          address=os.environ["POLYMARKET_RELAYER_API_KEY_ADDRESS"],
      ),
  )
  ```

  ```python Builder API Key theme={null}
  import os

  from polymarket import AsyncSecureClient, BuilderApiKey


  secure_client = await AsyncSecureClient.create(
      private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
      wallet=os.environ.get("POLYMARKET_WALLET_ADDRESS"),
      api_key=BuilderApiKey(
          key=os.environ["POLYMARKET_BUILDER_API_KEY"],
          secret=os.environ["POLYMARKET_BUILDER_SECRET"],
          passphrase=os.environ["POLYMARKET_BUILDER_PASSPHRASE"],
      ),
  )
  ```
</CodeGroup>

<Note>
  Builder API keys are supported for backwards compatibility with builders that
  still use them for gasless workflows. They are not used for order attribution.
  Use `builder_code` on orders for attribution.
</Note>

### Trading Setup

Before placing orders, make sure the authenticated wallet has the required trading approvals. If you use gasless wallet operations, configure API key authentication first.

<Note>
  From this point forward, snippets in Trading Setup, Trading, Position
  Lifecycle, and Wallet Operations submit real on-chain transactions or
  live orders against the configured environment when executed. Review
  each call before running it against a wallet that holds funds.
</Note>

<Steps>
  <Step title="Check Gasless Readiness">
    Optionally check whether the deposit wallet for the authenticated EOA is already deployed. The result is informational — call `setup_gasless_wallet()` in the next step regardless, since it is idempotent on deployment and is the call that binds the client to the deposit wallet.

    ```python theme={null}
    is_gasless_ready = await secure_client.is_gasless_ready()
    ```
  </Step>

  <Step title="Set Up Gasless Wallet">
    Always call `setup_gasless_wallet()` for gasless workflows. It deploys the deposit wallet if needed and returns a new client bound to the deposit wallet address. Close the previous client when you replace it.

    ```python theme={null}
    gasless_client = await secure_client.setup_gasless_wallet()
    await secure_client.close()
    secure_client = gasless_client
    ```
  </Step>

  <Step title="Set Up Trading Approvals">
    Set up the approvals required for trading and wait for the setup transaction to complete.

    ```python theme={null}
    handle = await secure_client.setup_trading_approvals()
    outcome = await handle.wait()

    # outcome.transaction_hash: TransactionHash
    ```
  </Step>
</Steps>

### Trading

Use a secure client to create, sign, and submit orders. Limit orders specify the price and size you want to trade. Market orders execute against resting liquidity immediately.

Order placement returns a discriminated response. Check `response.ok` before reading order details.

#### Place Orders

<Tabs>
  <Tab title="Limit Order">
    ```python theme={null}
    yes_token_id = market.outcomes.yes.token_id
    if yes_token_id is None:
        raise RuntimeError("Market does not have a YES token id")

    response = await secure_client.place_limit_order(
        token_id=yes_token_id,
        side="BUY",
        price="0.52",
        size="10",
    )

    if response.ok:
        # response.order_id: str
        ...
    else:
        # response.code: OrderResponseErrorCode
        # response.message: str
        ...
    ```
  </Tab>

  <Tab title="Expiring Limit Order">
    ```python theme={null}
    import time

    yes_token_id = market.outcomes.yes.token_id
    if yes_token_id is None:
        raise RuntimeError("Market does not have a YES token id")

    response = await secure_client.place_limit_order(
        token_id=yes_token_id,
        side="SELL",
        price="0.52",
        size="10",
        expiration=int(time.time()) + 60 * 60,
    )

    if response.ok:
        # response.order_id: str
        ...
    else:
        # response.code: OrderResponseErrorCode
        # response.message: str
        ...
    ```
  </Tab>

  <Tab title="Partial-Fill Market Order">
    ```python theme={null}
    yes_token_id = market.outcomes.yes.token_id
    if yes_token_id is None:
        raise RuntimeError("Market does not have a YES token id")

    response = await secure_client.place_market_order(
        token_id=yes_token_id,
        side="BUY",
        amount="10",
        max_spend="11",
        order_type="FAK",
    )

    if response.ok:
        # response.order_id: str
        ...
    else:
        # response.code: OrderResponseErrorCode
        # response.message: str
        ...
    ```
  </Tab>

  <Tab title="All-Or-Nothing Market Order">
    ```python theme={null}
    yes_token_id = market.outcomes.yes.token_id
    if yes_token_id is None:
        raise RuntimeError("Market does not have a YES token id")

    response = await secure_client.place_market_order(
        token_id=yes_token_id,
        side="SELL",
        shares="10",
        order_type="FOK",
    )

    if response.ok:
        # response.order_id: str
        ...
    else:
        # response.code: OrderResponseErrorCode
        # response.message: str
        ...
    ```
  </Tab>

  <Tab title="Builder Code">
    ```python theme={null}
    import os

    yes_token_id = market.outcomes.yes.token_id
    if yes_token_id is None:
        raise RuntimeError("Market does not have a YES token id")

    response = await secure_client.place_limit_order(
        token_id=yes_token_id,
        side="BUY",
        price="0.52",
        size="10",
        builder_code=os.environ["POLYMARKET_BUILDER_CODE"],
    )

    if response.ok:
        # response.order_id: str
        ...
    else:
        # response.code: OrderResponseErrorCode
        # response.message: str
        ...
    ```
  </Tab>
</Tabs>

#### Create, Then Post

Create signed orders separately when you want to review, store, or batch them before submitting.

<Tabs>
  <Tab title="Single Order">
    ```python theme={null}
    yes_token_id = market.outcomes.yes.token_id
    if yes_token_id is None:
        raise RuntimeError("Market does not have a YES token id")

    order = await secure_client.create_limit_order(
        token_id=yes_token_id,
        side="BUY",
        price="0.52",
        size="10",
    )

    response = await secure_client.post_order(order)

    if response.ok:
        # response.order_id: str
        ...
    else:
        # response.code: OrderResponseErrorCode
        # response.message: str
        ...
    ```
  </Tab>

  <Tab title="Batch Orders">
    ```python theme={null}
    yes_token_id = market.outcomes.yes.token_id
    if yes_token_id is None:
        raise RuntimeError("Market does not have a YES token id")

    first_order = await secure_client.create_limit_order(
        token_id=yes_token_id,
        side="BUY",
        price="0.52",
        size="10",
    )

    second_order = await secure_client.create_limit_order(
        token_id=yes_token_id,
        side="SELL",
        price="0.58",
        size="5",
    )

    responses = await secure_client.post_orders([first_order, second_order])

    for response in responses:
        if response.ok:
            # response.order_id: str
            ...
        else:
            # response.code: OrderResponseErrorCode
            # response.message: str
            ...
    ```
  </Tab>
</Tabs>

### Position Lifecycle

Use position lifecycle methods to split collateral into outcome tokens, merge complete sets back into collateral, or redeem resolved positions. These examples assume the secure client is configured with API key authentication as shown in [API Key Authentication](#api-key-authentication).

<Tabs>
  <Tab title="Split Position">
    ```python theme={null}
    condition_id = market.condition_id
    if condition_id is None:
        raise RuntimeError("Market does not have a condition id")

    handle = await secure_client.split_position(
        condition_id=condition_id,
        amount=1,
    )

    outcome = await handle.wait()

    # outcome.transaction_hash: TransactionHash
    ```
  </Tab>

  <Tab title="Merge Positions">
    ```python theme={null}
    condition_id = market.condition_id
    if condition_id is None:
        raise RuntimeError("Market does not have a condition id")

    handle = await secure_client.merge_positions(
        condition_id=condition_id,
        amount="max",
    )

    outcome = await handle.wait()

    # outcome.transaction_hash: TransactionHash
    ```
  </Tab>

  <Tab title="Redeem Positions">
    ```python theme={null}
    handle = await secure_client.redeem_positions(
        market_id=market.id,
    )

    outcome = await handle.wait()

    # outcome.transaction_hash: TransactionHash
    ```
  </Tab>
</Tabs>

### Wallet Operations

Use wallet operation methods for direct token movements from the authenticated wallet. Amounts are in base units. These examples assume the secure client is configured with API key authentication as shown in [API Key Authentication](#api-key-authentication).

```python theme={null}
import os

handle = await secure_client.transfer_erc20(
    token_address=secure_client.environment.collateral_token,
    recipient_address=os.environ["POLYMARKET_RECIPIENT_ADDRESS"],
    amount=1_000_000,
)

outcome = await handle.wait()

# outcome.transaction_hash: TransactionHash
```

### Order Management

Manage open orders for the authenticated wallet after placement. These examples assume `order_id` comes from an accepted order response.

<Tabs>
  <Tab title="Get Order">
    ```python theme={null}
    order = await secure_client.get_order(order_id=order_id)

    # order: OpenOrder
    ```
  </Tab>

  <Tab title="List Open Orders">
    ```python theme={null}
    condition_id = market.condition_id
    if condition_id is None:
        raise RuntimeError("Market does not have a condition id")

    open_orders = secure_client.list_open_orders(market=condition_id)

    async for page in open_orders:
        # page.items: tuple[OpenOrder, ...]
        ...
    ```
  </Tab>

  <Tab title="Cancel Order">
    ```python theme={null}
    response = await secure_client.cancel_order(order_id=order_id)

    # response.canceled: tuple[str, ...]
    ```
  </Tab>

  <Tab title="Cancel Market Orders">
    ```python theme={null}
    yes_token_id = market.outcomes.yes.token_id
    if yes_token_id is None:
        raise RuntimeError("Market does not have a YES token id")

    response = await secure_client.cancel_market_orders(token_id=yes_token_id)

    # response.canceled: tuple[str, ...]
    ```
  </Tab>
</Tabs>

### Rewards and Scoring

Use rewards methods to inspect active reward programs and scoring methods to check whether orders are eligible for scoring. `list_current_rewards` and `list_market_rewards` are public reads and are also available on `AsyncPublicClient` / `PublicClient`; `get_order_scoring` and `get_orders_scoring` require a secure client because they read account-scoped order data.

<Tabs>
  <Tab title="Current Rewards">
    ```python theme={null}
    rewards = secure_client.list_current_rewards()

    async for page in rewards:
        # page.items: tuple[CurrentReward, ...]
        ...
    ```
  </Tab>

  <Tab title="Market Rewards">
    ```python theme={null}
    condition_id = market.condition_id
    if condition_id is None:
        raise RuntimeError("Market does not have a condition id")

    rewards = secure_client.list_market_rewards(condition_id=condition_id)

    async for page in rewards:
        # page.items: tuple[MarketReward, ...]
        ...
    ```
  </Tab>

  <Tab title="Order Scoring">
    ```python theme={null}
    scoring = await secure_client.get_order_scoring(order_id=order_id)

    # scoring: bool
    ```
  </Tab>

  <Tab title="Batch Order Scoring">
    ```python theme={null}
    scoring = await secure_client.get_orders_scoring(
        order_ids=[first_order_id, second_order_id],
    )

    # scoring: dict[str, bool]
    ```
  </Tab>
</Tabs>

### Account Data

Secure clients read account-scoped data for the authenticated wallet by default. Methods that take a `user=` parameter (positions, portfolio value, activity) accept a different wallet address to read its data instead.

<Tabs>
  <Tab title="Positions">
    ```python theme={null}
    positions = secure_client.list_positions(
        market=[market.id],
        page_size=10,
    )

    async for page in positions:
        # page.items: tuple[Position, ...]
        ...
    ```
  </Tab>

  <Tab title="Portfolio Value">
    ```python theme={null}
    value = await secure_client.get_portfolio_values(market=[market.id])

    # value: tuple[PortfolioValue, ...]
    ```
  </Tab>

  <Tab title="Activity">
    ```python theme={null}
    activity = secure_client.list_activity(
        market=[market.id],
        page_size=10,
    )

    async for page in activity:
        for item in page.items:
            match item.type:
                case "TRADE":
                    # item.token_id: TokenId
                    # item.shares: Decimal
                    ...
                case "REWARD":
                    # item.amount: Decimal
                    ...
                case _:
                    # SPLIT / MERGE / REDEEM / CONVERSION / MAKER_REBATE
                    # / REFERRAL_REWARD / YIELD
                    ...
    ```
  </Tab>

  <Tab title="Trades">
    ```python theme={null}
    yes_token_id = market.outcomes.yes.token_id
    if yes_token_id is None:
        raise RuntimeError("Market does not have a YES token id")

    trades = secure_client.list_account_trades(token_id=yes_token_id)

    async for page in trades:
        # page.items: tuple[ClobTrade, ...]
        ...
    ```
  </Tab>

  <Tab title="Notifications">
    ```python theme={null}
    notifications = await secure_client.get_notifications()

    # notifications: tuple[Notification, ...]
    ```
  </Tab>
</Tabs>

### Authentication Sessions

Secure clients expose the API credentials created for the authenticated session. Store them securely if you want to reuse the session later without producing a new authentication signature while the credentials remain valid.

<Tabs>
  <Tab title="Save Credentials">
    ```python theme={null}
    import os

    from polymarket import AsyncSecureClient


    secure_client = await AsyncSecureClient.create(
        private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
        wallet=os.environ.get("POLYMARKET_WALLET_ADDRESS"),
    )

    saved_credentials = secure_client.credentials.model_dump(mode="json")
    ```
  </Tab>

  <Tab title="Reuse Credentials">
    ```python theme={null}
    import os

    from polymarket import ApiKeyCreds, AsyncSecureClient


    credentials = ApiKeyCreds.model_validate(saved_credentials)

    secure_client = await AsyncSecureClient.create(
        private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
        wallet=os.environ.get("POLYMARKET_WALLET_ADDRESS"),
        credentials=credentials,
    )
    ```
  </Tab>
</Tabs>
> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# List events (keyset pagination)

> Returns events using cursor-based (keyset) pagination for stable, efficient paging through large result sets. Use `next_cursor` from each response as `after_cursor` in the next request. The `offset` parameter is explicitly rejected; use `after_cursor` instead.




## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /events/keyset
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /events/keyset:
    get:
      tags:
        - Events
      summary: List events (keyset pagination)
      description: >
        Returns events using cursor-based (keyset) pagination for stable,
        efficient paging through large result sets. Use `next_cursor` from each
        response as `after_cursor` in the next request. The `offset` parameter
        is explicitly rejected; use `after_cursor` instead.
      operationId: listEventsKeyset
      parameters:
        - name: limit
          in: query
          description: Maximum number of results to return (max 500)
          schema:
            type: integer
            minimum: 1
            maximum: 500
            default: 20
        - name: order
          in: query
          description: Comma-separated list of JSON field names to order by
          schema:
            type: string
        - name: ascending
          in: query
          description: Sort direction. Only used when order is set.
          schema:
            type: boolean
            default: true
        - name: after_cursor
          in: query
          description: Opaque cursor token from a previous response's next_cursor
          schema:
            type: string
        - name: offset
          in: query
          description: Not allowed. Returns 422 if provided.
          schema:
            type: integer
        - name: id
          in: query
          schema:
            type: array
            items:
              type: integer
        - name: slug
          in: query
          schema:
            type: array
            items:
              type: string
        - name: closed
          in: query
          schema:
            type: boolean
        - name: live
          in: query
          schema:
            type: boolean
        - name: featured
          in: query
          schema:
            type: boolean
        - name: cyom
          in: query
          schema:
            type: boolean
        - name: title_search
          in: query
          schema:
            type: string
        - name: liquidity_min
          in: query
          schema:
            type: number
        - name: liquidity_max
          in: query
          schema:
            type: number
        - name: volume_min
          in: query
          schema:
            type: number
        - name: volume_max
          in: query
          schema:
            type: number
        - name: start_date_min
          in: query
          schema:
            type: string
            format: date-time
        - name: start_date_max
          in: query
          schema:
            type: string
            format: date-time
        - name: end_date_min
          in: query
          schema:
            type: string
            format: date-time
        - name: end_date_max
          in: query
          schema:
            type: string
            format: date-time
        - name: start_time_min
          in: query
          schema:
            type: string
            format: date-time
        - name: start_time_max
          in: query
          schema:
            type: string
            format: date-time
        - name: tag_id
          in: query
          schema:
            type: array
            items:
              type: integer
        - name: tag_slug
          in: query
          schema:
            type: string
        - name: exclude_tag_id
          in: query
          description: Tag IDs to exclude. Cannot overlap with tag_id.
          schema:
            type: array
            items:
              type: integer
        - name: related_tags
          in: query
          schema:
            type: boolean
        - name: tag_match
          in: query
          schema:
            type: string
        - name: series_id
          in: query
          schema:
            type: array
            items:
              type: integer
        - name: game_id
          in: query
          schema:
            type: array
            items:
              type: integer
        - name: event_date
          in: query
          schema:
            type: string
            format: date-time
        - name: event_week
          in: query
          schema:
            type: integer
        - name: featured_order
          in: query
          schema:
            type: boolean
        - name: recurrence
          in: query
          schema:
            type: string
        - name: created_by
          in: query
          schema:
            type: array
            items:
              type: string
        - name: parent_event_id
          in: query
          schema:
            type: integer
        - name: include_children
          in: query
          schema:
            type: boolean
        - name: partner_slug
          in: query
          description: When set, external_partners are attached to matching events
          schema:
            type: string
        - name: include_chat
          in: query
          description: When true, includes Chats and Series.Chats relations
          schema:
            type: boolean
        - name: include_template
          in: query
          description: When true, includes Templates relation
          schema:
            type: boolean
        - name: include_best_lines
          in: query
          description: When true, includes BestLines relation
          schema:
            type: boolean
        - name: locale
          in: query
          schema:
            type: string
      responses:
        '200':
          description: >
            Paginated list of events. Always includes Series, Tags, Markets, and
            EventCreators relations. Chats/Series.Chats, Templates, and
            BestLines are optional via their respective include_ flags. Nested
            markets include clob_rewards and fee_schedule. Teams are enriched
            automatically. external_partners attached only when partner_slug is
            set.
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/KeysetEventsResponse'
        '422':
          description: >
            Validation error. Returned when offset is provided, cursor is
            invalid, order fields are not valid, tag_id overlaps with
            exclude_tag_id, invalid recurrence, or other filter validation
            fails.
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ValidationError'
        '500':
          description: Internal server error (DB failures, cursor encode failures)
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/InternalError'
        '503':
          description: Service unavailable when keyset pagination is not configured
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ServiceUnavailableError'
components:
  schemas:
    KeysetEventsResponse:
      type: object
      properties:
        events:
          type: array
          description: Array of Event objects. Empty array if none found.
          items:
            $ref: '#/components/schemas/Event'
        next_cursor:
          type: string
          description: >
            Opaque cursor token for fetching the next page. Present only when
            the number of returned events equals the effective limit. Omitted on
            the last page.
    ValidationError:
      type: object
      properties:
        type:
          type: string
          example: validation error
        error:
          type: string
          example: offset is not allowed on keyset endpoints
    InternalError:
      type: object
      properties:
        type:
          type: string
          example: internal error
        error:
          type: string
          example: cannot get the information
    ServiceUnavailableError:
      type: object
      properties:
        type:
          type: string
          example: service unavailable
        error:
          type: string
          example: keyset pagination is not configured
    Event:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        creationDate:
          type: string
          format: date-time
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        liquidity:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        openInterest:
          type: number
          nullable: true
        sortBy:
          type: string
          nullable: true
        category:
          type: string
          nullable: true
        subcategory:
          type: string
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        published_at:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: number
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        featuredImage:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        parentEvent:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        negRiskMarketID:
          type: string
          nullable: true
        negRiskFeeBips:
          type: integer
          nullable: true
        commentCount:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        featuredImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        subEvents:
          type: array
          items:
            type: string
          nullable: true
        markets:
          type: array
          items:
            $ref: '#/components/schemas/Market'
        series:
          type: array
          items:
            $ref: '#/components/schemas/Series'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        cyom:
          type: boolean
          nullable: true
        closedTime:
          type: string
          format: date-time
          nullable: true
        showAllOutcomes:
          type: boolean
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        enableNegRisk:
          type: boolean
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        eventDate:
          type: string
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        eventWeek:
          type: integer
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        score:
          type: string
          nullable: true
        elapsed:
          type: string
          nullable: true
        period:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        ended:
          type: boolean
          nullable: true
        finishedTimestamp:
          type: string
          format: date-time
          nullable: true
        gmpChartMode:
          type: string
          nullable: true
        eventCreators:
          type: array
          items:
            $ref: '#/components/schemas/EventCreator'
        tweetCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
        featuredOrder:
          type: integer
          nullable: true
        estimateValue:
          type: boolean
          nullable: true
        cantEstimate:
          type: boolean
          nullable: true
        estimatedValue:
          type: string
          nullable: true
        templates:
          type: array
          items:
            $ref: '#/components/schemas/Template'
        spreadsMainLine:
          type: number
          nullable: true
        totalsMainLine:
          type: number
          nullable: true
        carouselMap:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        gameStatus:
          type: string
          nullable: true
    ImageOptimization:
      type: object
      properties:
        id:
          type: string
        imageUrlSource:
          type: string
          nullable: true
        imageUrlOptimized:
          type: string
          nullable: true
        imageSizeKbSource:
          type: number
          nullable: true
        imageSizeKbOptimized:
          type: number
          nullable: true
        imageOptimizedComplete:
          type: boolean
          nullable: true
        imageOptimizedLastUpdated:
          type: string
          nullable: true
        relID:
          type: integer
          nullable: true
        field:
          type: string
          nullable: true
        relname:
          type: string
          nullable: true
    Market:
      type: object
      properties:
        id:
          type: string
        question:
          type: string
          nullable: true
        conditionId:
          type: string
        slug:
          type: string
          nullable: true
        twitterCardImage:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        category:
          type: string
          nullable: true
        ammType:
          type: string
          nullable: true
        liquidity:
          type: string
          nullable: true
        sponsorName:
          type: string
          nullable: true
        sponsorImage:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        xAxisValue:
          type: string
          nullable: true
        yAxisValue:
          type: string
          nullable: true
        denominationToken:
          type: string
          nullable: true
        fee:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        lowerBound:
          type: string
          nullable: true
        upperBound:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
        outcomePrices:
          type: string
          nullable: true
        volume:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        marketType:
          type: string
          nullable: true
        formatType:
          type: string
          nullable: true
        lowerBoundDate:
          type: string
          nullable: true
        upperBoundDate:
          type: string
          nullable: true
        closed:
          type: boolean
          nullable: true
        marketMakerAddress:
          type: string
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        closedTime:
          type: string
          nullable: true
        wideFormat:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        mailchimpTag:
          type: string
          nullable: true
        featured:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        resolvedBy:
          type: string
          nullable: true
        restricted:
          type: boolean
          nullable: true
        marketGroup:
          type: integer
          nullable: true
        groupItemTitle:
          type: string
          nullable: true
        groupItemThreshold:
          type: string
          nullable: true
        questionID:
          type: string
          nullable: true
        umaEndDate:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        orderPriceMinTickSize:
          type: number
          nullable: true
        orderMinSize:
          type: number
          nullable: true
        umaResolutionStatus:
          type: string
          nullable: true
        curationOrder:
          type: integer
          nullable: true
        volumeNum:
          type: number
          nullable: true
        liquidityNum:
          type: number
          nullable: true
        endDateIso:
          type: string
          nullable: true
        startDateIso:
          type: string
          nullable: true
        umaEndDateIso:
          type: string
          nullable: true
        hasReviewedDates:
          type: boolean
          nullable: true
        readyForCron:
          type: boolean
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        gameStartTime:
          type: string
          nullable: true
        secondsDelay:
          type: integer
          nullable: true
        clobTokenIds:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        shortOutcomes:
          type: string
          nullable: true
        teamAID:
          type: string
          nullable: true
        teamBID:
          type: string
          nullable: true
        umaBond:
          type: string
          nullable: true
        umaReward:
          type: string
          nullable: true
        fpmmLive:
          type: boolean
          nullable: true
        volume24hrAmm:
          type: number
          nullable: true
        volume1wkAmm:
          type: number
          nullable: true
        volume1moAmm:
          type: number
          nullable: true
        volume1yrAmm:
          type: number
          nullable: true
        volume24hrClob:
          type: number
          nullable: true
        volume1wkClob:
          type: number
          nullable: true
        volume1moClob:
          type: number
          nullable: true
        volume1yrClob:
          type: number
          nullable: true
        volumeAmm:
          type: number
          nullable: true
        volumeClob:
          type: number
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        makerBaseFee:
          type: integer
          nullable: true
        takerBaseFee:
          type: integer
          nullable: true
        customLiveness:
          type: integer
          nullable: true
        acceptingOrders:
          type: boolean
          nullable: true
        notificationsEnabled:
          type: boolean
          nullable: true
        score:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        creator:
          type: string
          nullable: true
        ready:
          type: boolean
          nullable: true
        funded:
          type: boolean
          nullable: true
        pastSlugs:
          type: string
          nullable: true
        readyTimestamp:
          type: string
          format: date-time
          nullable: true
        fundedTimestamp:
          type: string
          format: date-time
          nullable: true
        acceptingOrdersTimestamp:
          type: string
          format: date-time
          nullable: true
        competitive:
          type: number
          nullable: true
        rewardsMinSize:
          type: number
          nullable: true
        rewardsMaxSpread:
          type: number
          nullable: true
        spread:
          type: number
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        oneDayPriceChange:
          type: number
          nullable: true
        oneHourPriceChange:
          type: number
          nullable: true
        oneWeekPriceChange:
          type: number
          nullable: true
        oneMonthPriceChange:
          type: number
          nullable: true
        oneYearPriceChange:
          type: number
          nullable: true
        lastTradePrice:
          type: number
          nullable: true
        bestBid:
          type: number
          nullable: true
        bestAsk:
          type: number
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        clearBookOnStart:
          type: boolean
          nullable: true
        chartColor:
          type: string
          nullable: true
        seriesColor:
          type: string
          nullable: true
        showGmpSeries:
          type: boolean
          nullable: true
        showGmpOutcome:
          type: boolean
          nullable: true
        manualActivation:
          type: boolean
          nullable: true
        negRiskOther:
          type: boolean
          nullable: true
        gameId:
          type: string
          nullable: true
        groupItemRange:
          type: string
          nullable: true
        sportsMarketType:
          type: string
          nullable: true
        line:
          type: number
          nullable: true
        umaResolutionStatuses:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        rfqEnabled:
          type: boolean
          nullable: true
        eventStartTime:
          type: string
          format: date-time
          nullable: true
        feesEnabled:
          type: boolean
          nullable: true
        feeSchedule:
          $ref: '#/components/schemas/FeeSchedule'
    Series:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        seriesType:
          type: string
          nullable: true
        recurrence:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: string
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        liquidity:
          type: number
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        pythTokenID:
          type: string
          nullable: true
        cgAssetName:
          type: string
          nullable: true
        score:
          type: integer
          nullable: true
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        commentCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
    Category:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        parentCategory:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Collection:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        collectionType:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        tags:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        headerImage:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        headerImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true
    EventCreator:
      type: object
      properties:
        id:
          type: string
        creatorName:
          type: string
          nullable: true
        creatorHandle:
          type: string
          nullable: true
        creatorUrl:
          type: string
          nullable: true
        creatorImage:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Chat:
      type: object
      properties:
        id:
          type: string
        channelId:
          type: string
          nullable: true
        channelName:
          type: string
          nullable: true
        channelImage:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        endTime:
          type: string
          format: date-time
          nullable: true
    Template:
      type: object
      properties:
        id:
          type: string
        eventTitle:
          type: string
          nullable: true
        eventSlug:
          type: string
          nullable: true
        eventImage:
          type: string
          nullable: true
        marketTitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        sortBy:
          type: string
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
    FeeSchedule:
      type: object
      properties:
        exponent:
          type: number
          nullable: true
        rate:
          type: number
          nullable: true
        takerOnly:
          type: boolean
          nullable: true
        rebateRate:
          type: number
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# List events



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /events
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /events:
    get:
      tags:
        - Events
      summary: List events
      operationId: listEvents
      parameters:
        - $ref: '#/components/parameters/limit'
        - $ref: '#/components/parameters/offset'
        - $ref: '#/components/parameters/order'
        - $ref: '#/components/parameters/ascending'
        - name: id
          in: query
          schema:
            type: array
            items:
              type: integer
        - name: tag_id
          in: query
          schema:
            type: integer
        - name: exclude_tag_id
          in: query
          schema:
            type: array
            items:
              type: integer
        - name: slug
          in: query
          schema:
            type: array
            items:
              type: string
        - name: tag_slug
          in: query
          schema:
            type: string
        - name: related_tags
          in: query
          schema:
            type: boolean
        - name: active
          in: query
          schema:
            type: boolean
        - name: archived
          in: query
          schema:
            type: boolean
        - name: featured
          in: query
          schema:
            type: boolean
        - name: cyom
          in: query
          schema:
            type: boolean
        - name: include_chat
          in: query
          schema:
            type: boolean
        - name: include_template
          in: query
          schema:
            type: boolean
        - name: recurrence
          in: query
          schema:
            type: string
        - name: closed
          in: query
          schema:
            type: boolean
        - name: liquidity_min
          in: query
          schema:
            type: number
        - name: liquidity_max
          in: query
          schema:
            type: number
        - name: volume_min
          in: query
          schema:
            type: number
        - name: volume_max
          in: query
          schema:
            type: number
        - name: start_date_min
          in: query
          schema:
            type: string
            format: date-time
        - name: start_date_max
          in: query
          schema:
            type: string
            format: date-time
        - name: end_date_min
          in: query
          schema:
            type: string
            format: date-time
        - name: end_date_max
          in: query
          schema:
            type: string
            format: date-time
      responses:
        '200':
          description: List of events
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Event'
components:
  parameters:
    limit:
      name: limit
      in: query
      schema:
        type: integer
        minimum: 0
    offset:
      name: offset
      in: query
      schema:
        type: integer
        minimum: 0
    order:
      name: order
      in: query
      schema:
        type: string
      description: Comma-separated list of fields to order by
    ascending:
      name: ascending
      in: query
      schema:
        type: boolean
  schemas:
    Event:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        creationDate:
          type: string
          format: date-time
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        liquidity:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        openInterest:
          type: number
          nullable: true
        sortBy:
          type: string
          nullable: true
        category:
          type: string
          nullable: true
        subcategory:
          type: string
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        published_at:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: number
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        featuredImage:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        parentEvent:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        negRiskMarketID:
          type: string
          nullable: true
        negRiskFeeBips:
          type: integer
          nullable: true
        commentCount:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        featuredImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        subEvents:
          type: array
          items:
            type: string
          nullable: true
        markets:
          type: array
          items:
            $ref: '#/components/schemas/Market'
        series:
          type: array
          items:
            $ref: '#/components/schemas/Series'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        cyom:
          type: boolean
          nullable: true
        closedTime:
          type: string
          format: date-time
          nullable: true
        showAllOutcomes:
          type: boolean
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        enableNegRisk:
          type: boolean
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        eventDate:
          type: string
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        eventWeek:
          type: integer
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        score:
          type: string
          nullable: true
        elapsed:
          type: string
          nullable: true
        period:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        ended:
          type: boolean
          nullable: true
        finishedTimestamp:
          type: string
          format: date-time
          nullable: true
        gmpChartMode:
          type: string
          nullable: true
        eventCreators:
          type: array
          items:
            $ref: '#/components/schemas/EventCreator'
        tweetCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
        featuredOrder:
          type: integer
          nullable: true
        estimateValue:
          type: boolean
          nullable: true
        cantEstimate:
          type: boolean
          nullable: true
        estimatedValue:
          type: string
          nullable: true
        templates:
          type: array
          items:
            $ref: '#/components/schemas/Template'
        spreadsMainLine:
          type: number
          nullable: true
        totalsMainLine:
          type: number
          nullable: true
        carouselMap:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        gameStatus:
          type: string
          nullable: true
    ImageOptimization:
      type: object
      properties:
        id:
          type: string
        imageUrlSource:
          type: string
          nullable: true
        imageUrlOptimized:
          type: string
          nullable: true
        imageSizeKbSource:
          type: number
          nullable: true
        imageSizeKbOptimized:
          type: number
          nullable: true
        imageOptimizedComplete:
          type: boolean
          nullable: true
        imageOptimizedLastUpdated:
          type: string
          nullable: true
        relID:
          type: integer
          nullable: true
        field:
          type: string
          nullable: true
        relname:
          type: string
          nullable: true
    Market:
      type: object
      properties:
        id:
          type: string
        question:
          type: string
          nullable: true
        conditionId:
          type: string
        slug:
          type: string
          nullable: true
        twitterCardImage:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        category:
          type: string
          nullable: true
        ammType:
          type: string
          nullable: true
        liquidity:
          type: string
          nullable: true
        sponsorName:
          type: string
          nullable: true
        sponsorImage:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        xAxisValue:
          type: string
          nullable: true
        yAxisValue:
          type: string
          nullable: true
        denominationToken:
          type: string
          nullable: true
        fee:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        lowerBound:
          type: string
          nullable: true
        upperBound:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
        outcomePrices:
          type: string
          nullable: true
        volume:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        marketType:
          type: string
          nullable: true
        formatType:
          type: string
          nullable: true
        lowerBoundDate:
          type: string
          nullable: true
        upperBoundDate:
          type: string
          nullable: true
        closed:
          type: boolean
          nullable: true
        marketMakerAddress:
          type: string
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        closedTime:
          type: string
          nullable: true
        wideFormat:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        mailchimpTag:
          type: string
          nullable: true
        featured:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        resolvedBy:
          type: string
          nullable: true
        restricted:
          type: boolean
          nullable: true
        marketGroup:
          type: integer
          nullable: true
        groupItemTitle:
          type: string
          nullable: true
        groupItemThreshold:
          type: string
          nullable: true
        questionID:
          type: string
          nullable: true
        umaEndDate:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        orderPriceMinTickSize:
          type: number
          nullable: true
        orderMinSize:
          type: number
          nullable: true
        umaResolutionStatus:
          type: string
          nullable: true
        curationOrder:
          type: integer
          nullable: true
        volumeNum:
          type: number
          nullable: true
        liquidityNum:
          type: number
          nullable: true
        endDateIso:
          type: string
          nullable: true
        startDateIso:
          type: string
          nullable: true
        umaEndDateIso:
          type: string
          nullable: true
        hasReviewedDates:
          type: boolean
          nullable: true
        readyForCron:
          type: boolean
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        gameStartTime:
          type: string
          nullable: true
        secondsDelay:
          type: integer
          nullable: true
        clobTokenIds:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        shortOutcomes:
          type: string
          nullable: true
        teamAID:
          type: string
          nullable: true
        teamBID:
          type: string
          nullable: true
        umaBond:
          type: string
          nullable: true
        umaReward:
          type: string
          nullable: true
        fpmmLive:
          type: boolean
          nullable: true
        volume24hrAmm:
          type: number
          nullable: true
        volume1wkAmm:
          type: number
          nullable: true
        volume1moAmm:
          type: number
          nullable: true
        volume1yrAmm:
          type: number
          nullable: true
        volume24hrClob:
          type: number
          nullable: true
        volume1wkClob:
          type: number
          nullable: true
        volume1moClob:
          type: number
          nullable: true
        volume1yrClob:
          type: number
          nullable: true
        volumeAmm:
          type: number
          nullable: true
        volumeClob:
          type: number
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        makerBaseFee:
          type: integer
          nullable: true
        takerBaseFee:
          type: integer
          nullable: true
        customLiveness:
          type: integer
          nullable: true
        acceptingOrders:
          type: boolean
          nullable: true
        notificationsEnabled:
          type: boolean
          nullable: true
        score:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        creator:
          type: string
          nullable: true
        ready:
          type: boolean
          nullable: true
        funded:
          type: boolean
          nullable: true
        pastSlugs:
          type: string
          nullable: true
        readyTimestamp:
          type: string
          format: date-time
          nullable: true
        fundedTimestamp:
          type: string
          format: date-time
          nullable: true
        acceptingOrdersTimestamp:
          type: string
          format: date-time
          nullable: true
        competitive:
          type: number
          nullable: true
        rewardsMinSize:
          type: number
          nullable: true
        rewardsMaxSpread:
          type: number
          nullable: true
        spread:
          type: number
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        oneDayPriceChange:
          type: number
          nullable: true
        oneHourPriceChange:
          type: number
          nullable: true
        oneWeekPriceChange:
          type: number
          nullable: true
        oneMonthPriceChange:
          type: number
          nullable: true
        oneYearPriceChange:
          type: number
          nullable: true
        lastTradePrice:
          type: number
          nullable: true
        bestBid:
          type: number
          nullable: true
        bestAsk:
          type: number
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        clearBookOnStart:
          type: boolean
          nullable: true
        chartColor:
          type: string
          nullable: true
        seriesColor:
          type: string
          nullable: true
        showGmpSeries:
          type: boolean
          nullable: true
        showGmpOutcome:
          type: boolean
          nullable: true
        manualActivation:
          type: boolean
          nullable: true
        negRiskOther:
          type: boolean
          nullable: true
        gameId:
          type: string
          nullable: true
        groupItemRange:
          type: string
          nullable: true
        sportsMarketType:
          type: string
          nullable: true
        line:
          type: number
          nullable: true
        umaResolutionStatuses:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        rfqEnabled:
          type: boolean
          nullable: true
        eventStartTime:
          type: string
          format: date-time
          nullable: true
        feesEnabled:
          type: boolean
          nullable: true
        feeSchedule:
          $ref: '#/components/schemas/FeeSchedule'
    Series:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        seriesType:
          type: string
          nullable: true
        recurrence:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: string
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        liquidity:
          type: number
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        pythTokenID:
          type: string
          nullable: true
        cgAssetName:
          type: string
          nullable: true
        score:
          type: integer
          nullable: true
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        commentCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
    Category:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        parentCategory:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Collection:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        collectionType:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        tags:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        headerImage:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        headerImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true
    EventCreator:
      type: object
      properties:
        id:
          type: string
        creatorName:
          type: string
          nullable: true
        creatorHandle:
          type: string
          nullable: true
        creatorUrl:
          type: string
          nullable: true
        creatorImage:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Chat:
      type: object
      properties:
        id:
          type: string
        channelId:
          type: string
          nullable: true
        channelName:
          type: string
          nullable: true
        channelImage:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        endTime:
          type: string
          format: date-time
          nullable: true
    Template:
      type: object
      properties:
        id:
          type: string
        eventTitle:
          type: string
          nullable: true
        eventSlug:
          type: string
          nullable: true
        eventImage:
          type: string
          nullable: true
        marketTitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        sortBy:
          type: string
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
    FeeSchedule:
      type: object
      properties:
        exponent:
          type: number
          nullable: true
        rate:
          type: number
          nullable: true
        takerOnly:
          type: boolean
          nullable: true
        rebateRate:
          type: number
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get event by id



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /events/{id}
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /events/{id}:
    get:
      tags:
        - Events
      summary: Get event by id
      operationId: getEvent
      parameters:
        - $ref: '#/components/parameters/pathId'
        - name: include_chat
          in: query
          schema:
            type: boolean
        - name: include_template
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: Event
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Event'
        '404':
          description: Not found
components:
  parameters:
    pathId:
      name: id
      in: path
      required: true
      schema:
        type: integer
  schemas:
    Event:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        creationDate:
          type: string
          format: date-time
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        liquidity:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        openInterest:
          type: number
          nullable: true
        sortBy:
          type: string
          nullable: true
        category:
          type: string
          nullable: true
        subcategory:
          type: string
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        published_at:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: number
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        featuredImage:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        parentEvent:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        negRiskMarketID:
          type: string
          nullable: true
        negRiskFeeBips:
          type: integer
          nullable: true
        commentCount:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        featuredImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        subEvents:
          type: array
          items:
            type: string
          nullable: true
        markets:
          type: array
          items:
            $ref: '#/components/schemas/Market'
        series:
          type: array
          items:
            $ref: '#/components/schemas/Series'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        cyom:
          type: boolean
          nullable: true
        closedTime:
          type: string
          format: date-time
          nullable: true
        showAllOutcomes:
          type: boolean
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        enableNegRisk:
          type: boolean
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        eventDate:
          type: string
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        eventWeek:
          type: integer
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        score:
          type: string
          nullable: true
        elapsed:
          type: string
          nullable: true
        period:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        ended:
          type: boolean
          nullable: true
        finishedTimestamp:
          type: string
          format: date-time
          nullable: true
        gmpChartMode:
          type: string
          nullable: true
        eventCreators:
          type: array
          items:
            $ref: '#/components/schemas/EventCreator'
        tweetCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
        featuredOrder:
          type: integer
          nullable: true
        estimateValue:
          type: boolean
          nullable: true
        cantEstimate:
          type: boolean
          nullable: true
        estimatedValue:
          type: string
          nullable: true
        templates:
          type: array
          items:
            $ref: '#/components/schemas/Template'
        spreadsMainLine:
          type: number
          nullable: true
        totalsMainLine:
          type: number
          nullable: true
        carouselMap:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        gameStatus:
          type: string
          nullable: true
    ImageOptimization:
      type: object
      properties:
        id:
          type: string
        imageUrlSource:
          type: string
          nullable: true
        imageUrlOptimized:
          type: string
          nullable: true
        imageSizeKbSource:
          type: number
          nullable: true
        imageSizeKbOptimized:
          type: number
          nullable: true
        imageOptimizedComplete:
          type: boolean
          nullable: true
        imageOptimizedLastUpdated:
          type: string
          nullable: true
        relID:
          type: integer
          nullable: true
        field:
          type: string
          nullable: true
        relname:
          type: string
          nullable: true
    Market:
      type: object
      properties:
        id:
          type: string
        question:
          type: string
          nullable: true
        conditionId:
          type: string
        slug:
          type: string
          nullable: true
        twitterCardImage:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        category:
          type: string
          nullable: true
        ammType:
          type: string
          nullable: true
        liquidity:
          type: string
          nullable: true
        sponsorName:
          type: string
          nullable: true
        sponsorImage:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        xAxisValue:
          type: string
          nullable: true
        yAxisValue:
          type: string
          nullable: true
        denominationToken:
          type: string
          nullable: true
        fee:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        lowerBound:
          type: string
          nullable: true
        upperBound:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
        outcomePrices:
          type: string
          nullable: true
        volume:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        marketType:
          type: string
          nullable: true
        formatType:
          type: string
          nullable: true
        lowerBoundDate:
          type: string
          nullable: true
        upperBoundDate:
          type: string
          nullable: true
        closed:
          type: boolean
          nullable: true
        marketMakerAddress:
          type: string
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        closedTime:
          type: string
          nullable: true
        wideFormat:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        mailchimpTag:
          type: string
          nullable: true
        featured:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        resolvedBy:
          type: string
          nullable: true
        restricted:
          type: boolean
          nullable: true
        marketGroup:
          type: integer
          nullable: true
        groupItemTitle:
          type: string
          nullable: true
        groupItemThreshold:
          type: string
          nullable: true
        questionID:
          type: string
          nullable: true
        umaEndDate:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        orderPriceMinTickSize:
          type: number
          nullable: true
        orderMinSize:
          type: number
          nullable: true
        umaResolutionStatus:
          type: string
          nullable: true
        curationOrder:
          type: integer
          nullable: true
        volumeNum:
          type: number
          nullable: true
        liquidityNum:
          type: number
          nullable: true
        endDateIso:
          type: string
          nullable: true
        startDateIso:
          type: string
          nullable: true
        umaEndDateIso:
          type: string
          nullable: true
        hasReviewedDates:
          type: boolean
          nullable: true
        readyForCron:
          type: boolean
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        gameStartTime:
          type: string
          nullable: true
        secondsDelay:
          type: integer
          nullable: true
        clobTokenIds:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        shortOutcomes:
          type: string
          nullable: true
        teamAID:
          type: string
          nullable: true
        teamBID:
          type: string
          nullable: true
        umaBond:
          type: string
          nullable: true
        umaReward:
          type: string
          nullable: true
        fpmmLive:
          type: boolean
          nullable: true
        volume24hrAmm:
          type: number
          nullable: true
        volume1wkAmm:
          type: number
          nullable: true
        volume1moAmm:
          type: number
          nullable: true
        volume1yrAmm:
          type: number
          nullable: true
        volume24hrClob:
          type: number
          nullable: true
        volume1wkClob:
          type: number
          nullable: true
        volume1moClob:
          type: number
          nullable: true
        volume1yrClob:
          type: number
          nullable: true
        volumeAmm:
          type: number
          nullable: true
        volumeClob:
          type: number
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        makerBaseFee:
          type: integer
          nullable: true
        takerBaseFee:
          type: integer
          nullable: true
        customLiveness:
          type: integer
          nullable: true
        acceptingOrders:
          type: boolean
          nullable: true
        notificationsEnabled:
          type: boolean
          nullable: true
        score:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        creator:
          type: string
          nullable: true
        ready:
          type: boolean
          nullable: true
        funded:
          type: boolean
          nullable: true
        pastSlugs:
          type: string
          nullable: true
        readyTimestamp:
          type: string
          format: date-time
          nullable: true
        fundedTimestamp:
          type: string
          format: date-time
          nullable: true
        acceptingOrdersTimestamp:
          type: string
          format: date-time
          nullable: true
        competitive:
          type: number
          nullable: true
        rewardsMinSize:
          type: number
          nullable: true
        rewardsMaxSpread:
          type: number
          nullable: true
        spread:
          type: number
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        oneDayPriceChange:
          type: number
          nullable: true
        oneHourPriceChange:
          type: number
          nullable: true
        oneWeekPriceChange:
          type: number
          nullable: true
        oneMonthPriceChange:
          type: number
          nullable: true
        oneYearPriceChange:
          type: number
          nullable: true
        lastTradePrice:
          type: number
          nullable: true
        bestBid:
          type: number
          nullable: true
        bestAsk:
          type: number
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        clearBookOnStart:
          type: boolean
          nullable: true
        chartColor:
          type: string
          nullable: true
        seriesColor:
          type: string
          nullable: true
        showGmpSeries:
          type: boolean
          nullable: true
        showGmpOutcome:
          type: boolean
          nullable: true
        manualActivation:
          type: boolean
          nullable: true
        negRiskOther:
          type: boolean
          nullable: true
        gameId:
          type: string
          nullable: true
        groupItemRange:
          type: string
          nullable: true
        sportsMarketType:
          type: string
          nullable: true
        line:
          type: number
          nullable: true
        umaResolutionStatuses:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        rfqEnabled:
          type: boolean
          nullable: true
        eventStartTime:
          type: string
          format: date-time
          nullable: true
        feesEnabled:
          type: boolean
          nullable: true
        feeSchedule:
          $ref: '#/components/schemas/FeeSchedule'
    Series:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        seriesType:
          type: string
          nullable: true
        recurrence:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: string
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        liquidity:
          type: number
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        pythTokenID:
          type: string
          nullable: true
        cgAssetName:
          type: string
          nullable: true
        score:
          type: integer
          nullable: true
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        commentCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
    Category:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        parentCategory:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Collection:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        collectionType:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        tags:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        headerImage:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        headerImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true
    EventCreator:
      type: object
      properties:
        id:
          type: string
        creatorName:
          type: string
          nullable: true
        creatorHandle:
          type: string
          nullable: true
        creatorUrl:
          type: string
          nullable: true
        creatorImage:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Chat:
      type: object
      properties:
        id:
          type: string
        channelId:
          type: string
          nullable: true
        channelName:
          type: string
          nullable: true
        channelImage:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        endTime:
          type: string
          format: date-time
          nullable: true
    Template:
      type: object
      properties:
        id:
          type: string
        eventTitle:
          type: string
          nullable: true
        eventSlug:
          type: string
          nullable: true
        eventImage:
          type: string
          nullable: true
        marketTitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        sortBy:
          type: string
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
    FeeSchedule:
      type: object
      properties:
        exponent:
          type: number
          nullable: true
        rate:
          type: number
          nullable: true
        takerOnly:
          type: boolean
          nullable: true
        rebateRate:
          type: number
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get event by slug



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /events/slug/{slug}
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /events/slug/{slug}:
    get:
      tags:
        - Events
      summary: Get event by slug
      operationId: getEventBySlug
      parameters:
        - $ref: '#/components/parameters/pathSlug'
        - name: include_chat
          in: query
          schema:
            type: boolean
        - name: include_template
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: Event
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Event'
        '404':
          description: Not found
components:
  parameters:
    pathSlug:
      name: slug
      in: path
      required: true
      schema:
        type: string
  schemas:
    Event:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        creationDate:
          type: string
          format: date-time
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        liquidity:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        openInterest:
          type: number
          nullable: true
        sortBy:
          type: string
          nullable: true
        category:
          type: string
          nullable: true
        subcategory:
          type: string
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        published_at:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: number
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        featuredImage:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        parentEvent:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        negRiskMarketID:
          type: string
          nullable: true
        negRiskFeeBips:
          type: integer
          nullable: true
        commentCount:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        featuredImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        subEvents:
          type: array
          items:
            type: string
          nullable: true
        markets:
          type: array
          items:
            $ref: '#/components/schemas/Market'
        series:
          type: array
          items:
            $ref: '#/components/schemas/Series'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        cyom:
          type: boolean
          nullable: true
        closedTime:
          type: string
          format: date-time
          nullable: true
        showAllOutcomes:
          type: boolean
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        enableNegRisk:
          type: boolean
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        eventDate:
          type: string
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        eventWeek:
          type: integer
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        score:
          type: string
          nullable: true
        elapsed:
          type: string
          nullable: true
        period:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        ended:
          type: boolean
          nullable: true
        finishedTimestamp:
          type: string
          format: date-time
          nullable: true
        gmpChartMode:
          type: string
          nullable: true
        eventCreators:
          type: array
          items:
            $ref: '#/components/schemas/EventCreator'
        tweetCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
        featuredOrder:
          type: integer
          nullable: true
        estimateValue:
          type: boolean
          nullable: true
        cantEstimate:
          type: boolean
          nullable: true
        estimatedValue:
          type: string
          nullable: true
        templates:
          type: array
          items:
            $ref: '#/components/schemas/Template'
        spreadsMainLine:
          type: number
          nullable: true
        totalsMainLine:
          type: number
          nullable: true
        carouselMap:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        gameStatus:
          type: string
          nullable: true
    ImageOptimization:
      type: object
      properties:
        id:
          type: string
        imageUrlSource:
          type: string
          nullable: true
        imageUrlOptimized:
          type: string
          nullable: true
        imageSizeKbSource:
          type: number
          nullable: true
        imageSizeKbOptimized:
          type: number
          nullable: true
        imageOptimizedComplete:
          type: boolean
          nullable: true
        imageOptimizedLastUpdated:
          type: string
          nullable: true
        relID:
          type: integer
          nullable: true
        field:
          type: string
          nullable: true
        relname:
          type: string
          nullable: true
    Market:
      type: object
      properties:
        id:
          type: string
        question:
          type: string
          nullable: true
        conditionId:
          type: string
        slug:
          type: string
          nullable: true
        twitterCardImage:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        category:
          type: string
          nullable: true
        ammType:
          type: string
          nullable: true
        liquidity:
          type: string
          nullable: true
        sponsorName:
          type: string
          nullable: true
        sponsorImage:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        xAxisValue:
          type: string
          nullable: true
        yAxisValue:
          type: string
          nullable: true
        denominationToken:
          type: string
          nullable: true
        fee:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        lowerBound:
          type: string
          nullable: true
        upperBound:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
        outcomePrices:
          type: string
          nullable: true
        volume:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        marketType:
          type: string
          nullable: true
        formatType:
          type: string
          nullable: true
        lowerBoundDate:
          type: string
          nullable: true
        upperBoundDate:
          type: string
          nullable: true
        closed:
          type: boolean
          nullable: true
        marketMakerAddress:
          type: string
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        closedTime:
          type: string
          nullable: true
        wideFormat:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        mailchimpTag:
          type: string
          nullable: true
        featured:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        resolvedBy:
          type: string
          nullable: true
        restricted:
          type: boolean
          nullable: true
        marketGroup:
          type: integer
          nullable: true
        groupItemTitle:
          type: string
          nullable: true
        groupItemThreshold:
          type: string
          nullable: true
        questionID:
          type: string
          nullable: true
        umaEndDate:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        orderPriceMinTickSize:
          type: number
          nullable: true
        orderMinSize:
          type: number
          nullable: true
        umaResolutionStatus:
          type: string
          nullable: true
        curationOrder:
          type: integer
          nullable: true
        volumeNum:
          type: number
          nullable: true
        liquidityNum:
          type: number
          nullable: true
        endDateIso:
          type: string
          nullable: true
        startDateIso:
          type: string
          nullable: true
        umaEndDateIso:
          type: string
          nullable: true
        hasReviewedDates:
          type: boolean
          nullable: true
        readyForCron:
          type: boolean
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        gameStartTime:
          type: string
          nullable: true
        secondsDelay:
          type: integer
          nullable: true
        clobTokenIds:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        shortOutcomes:
          type: string
          nullable: true
        teamAID:
          type: string
          nullable: true
        teamBID:
          type: string
          nullable: true
        umaBond:
          type: string
          nullable: true
        umaReward:
          type: string
          nullable: true
        fpmmLive:
          type: boolean
          nullable: true
        volume24hrAmm:
          type: number
          nullable: true
        volume1wkAmm:
          type: number
          nullable: true
        volume1moAmm:
          type: number
          nullable: true
        volume1yrAmm:
          type: number
          nullable: true
        volume24hrClob:
          type: number
          nullable: true
        volume1wkClob:
          type: number
          nullable: true
        volume1moClob:
          type: number
          nullable: true
        volume1yrClob:
          type: number
          nullable: true
        volumeAmm:
          type: number
          nullable: true
        volumeClob:
          type: number
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        makerBaseFee:
          type: integer
          nullable: true
        takerBaseFee:
          type: integer
          nullable: true
        customLiveness:
          type: integer
          nullable: true
        acceptingOrders:
          type: boolean
          nullable: true
        notificationsEnabled:
          type: boolean
          nullable: true
        score:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        creator:
          type: string
          nullable: true
        ready:
          type: boolean
          nullable: true
        funded:
          type: boolean
          nullable: true
        pastSlugs:
          type: string
          nullable: true
        readyTimestamp:
          type: string
          format: date-time
          nullable: true
        fundedTimestamp:
          type: string
          format: date-time
          nullable: true
        acceptingOrdersTimestamp:
          type: string
          format: date-time
          nullable: true
        competitive:
          type: number
          nullable: true
        rewardsMinSize:
          type: number
          nullable: true
        rewardsMaxSpread:
          type: number
          nullable: true
        spread:
          type: number
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        oneDayPriceChange:
          type: number
          nullable: true
        oneHourPriceChange:
          type: number
          nullable: true
        oneWeekPriceChange:
          type: number
          nullable: true
        oneMonthPriceChange:
          type: number
          nullable: true
        oneYearPriceChange:
          type: number
          nullable: true
        lastTradePrice:
          type: number
          nullable: true
        bestBid:
          type: number
          nullable: true
        bestAsk:
          type: number
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        clearBookOnStart:
          type: boolean
          nullable: true
        chartColor:
          type: string
          nullable: true
        seriesColor:
          type: string
          nullable: true
        showGmpSeries:
          type: boolean
          nullable: true
        showGmpOutcome:
          type: boolean
          nullable: true
        manualActivation:
          type: boolean
          nullable: true
        negRiskOther:
          type: boolean
          nullable: true
        gameId:
          type: string
          nullable: true
        groupItemRange:
          type: string
          nullable: true
        sportsMarketType:
          type: string
          nullable: true
        line:
          type: number
          nullable: true
        umaResolutionStatuses:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        rfqEnabled:
          type: boolean
          nullable: true
        eventStartTime:
          type: string
          format: date-time
          nullable: true
        feesEnabled:
          type: boolean
          nullable: true
        feeSchedule:
          $ref: '#/components/schemas/FeeSchedule'
    Series:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        seriesType:
          type: string
          nullable: true
        recurrence:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: string
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        liquidity:
          type: number
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        pythTokenID:
          type: string
          nullable: true
        cgAssetName:
          type: string
          nullable: true
        score:
          type: integer
          nullable: true
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        commentCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
    Category:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        parentCategory:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Collection:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        collectionType:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        tags:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        headerImage:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        headerImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true
    EventCreator:
      type: object
      properties:
        id:
          type: string
        creatorName:
          type: string
          nullable: true
        creatorHandle:
          type: string
          nullable: true
        creatorUrl:
          type: string
          nullable: true
        creatorImage:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Chat:
      type: object
      properties:
        id:
          type: string
        channelId:
          type: string
          nullable: true
        channelName:
          type: string
          nullable: true
        channelImage:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        endTime:
          type: string
          format: date-time
          nullable: true
    Template:
      type: object
      properties:
        id:
          type: string
        eventTitle:
          type: string
          nullable: true
        eventSlug:
          type: string
          nullable: true
        eventImage:
          type: string
          nullable: true
        marketTitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        sortBy:
          type: string
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
    FeeSchedule:
      type: object
      properties:
        exponent:
          type: number
          nullable: true
        rate:
          type: number
          nullable: true
        takerOnly:
          type: boolean
          nullable: true
        rebateRate:
          type: number
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get event tags



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /events/{id}/tags
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /events/{id}/tags:
    get:
      tags:
        - Events
        - Tags
      summary: Get event tags
      operationId: getEventTags
      parameters:
        - $ref: '#/components/parameters/pathId'
      responses:
        '200':
          description: Tags attached to the event
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Tag'
        '404':
          description: Not found
components:
  parameters:
    pathId:
      name: id
      in: path
      required: true
      schema:
        type: integer
  schemas:
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# List markets (keyset pagination)

> Returns markets using cursor-based (keyset) pagination for stable, efficient paging through large result sets. Use `next_cursor` from each response as `after_cursor` in the next request. The `offset` parameter is explicitly rejected; use `after_cursor` instead.




## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /markets/keyset
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /markets/keyset:
    get:
      tags:
        - Markets
      summary: List markets (keyset pagination)
      description: >
        Returns markets using cursor-based (keyset) pagination for stable,
        efficient paging through large result sets. Use `next_cursor` from each
        response as `after_cursor` in the next request. The `offset` parameter
        is explicitly rejected; use `after_cursor` instead.
      operationId: listMarketsKeyset
      parameters:
        - name: limit
          in: query
          description: Maximum number of results to return (max 100)
          schema:
            type: integer
            minimum: 1
            maximum: 100
            default: 20
        - name: order
          in: query
          description: >-
            Comma-separated list of JSON field names to order by, e.g.
            volume_num,liquidity_num
          schema:
            type: string
        - name: ascending
          in: query
          description: Sort direction. Only used when order is set.
          schema:
            type: boolean
            default: true
        - name: after_cursor
          in: query
          description: Opaque cursor token from a previous response's next_cursor
          schema:
            type: string
        - name: offset
          in: query
          description: Not allowed. Returns 422 if provided.
          schema:
            type: integer
        - name: id
          in: query
          schema:
            type: array
            items:
              type: integer
        - name: slug
          in: query
          schema:
            type: array
            items:
              type: string
        - name: closed
          in: query
          schema:
            type: boolean
            default: false
        - name: decimalized
          in: query
          schema:
            type: boolean
        - name: clob_token_ids
          in: query
          schema:
            type: array
            items:
              type: string
        - name: condition_ids
          in: query
          schema:
            type: array
            items:
              type: string
        - name: question_ids
          in: query
          schema:
            type: array
            items:
              type: string
        - name: market_maker_address
          in: query
          schema:
            type: array
            items:
              type: string
        - name: liquidity_num_min
          in: query
          schema:
            type: number
        - name: liquidity_num_max
          in: query
          schema:
            type: number
        - name: volume_num_min
          in: query
          schema:
            type: number
        - name: volume_num_max
          in: query
          schema:
            type: number
        - name: start_date_min
          in: query
          schema:
            type: string
            format: date-time
        - name: start_date_max
          in: query
          schema:
            type: string
            format: date-time
        - name: end_date_min
          in: query
          schema:
            type: string
            format: date-time
        - name: end_date_max
          in: query
          schema:
            type: string
            format: date-time
        - name: tag_id
          in: query
          schema:
            type: array
            items:
              type: integer
        - name: related_tags
          in: query
          schema:
            type: boolean
        - name: tag_match
          in: query
          schema:
            type: string
        - name: cyom
          in: query
          schema:
            type: boolean
        - name: rfq_enabled
          in: query
          schema:
            type: boolean
        - name: uma_resolution_status
          in: query
          schema:
            type: string
        - name: game_id
          in: query
          schema:
            type: string
        - name: sports_market_types
          in: query
          schema:
            type: array
            items:
              type: string
        - name: include_tag
          in: query
          description: When true, includes Tags relation on each market
          schema:
            type: boolean
        - name: locale
          in: query
          schema:
            type: string
      responses:
        '200':
          description: >
            Paginated list of markets. Includes Events and Events.Series
            relations. Tags included only when include_tag=true. Nested
            clob_rewards and fee_schedule are populated on each market.
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/KeysetMarketsResponse'
        '422':
          description: >
            Validation error. Returned when offset is provided, cursor is
            invalid, order fields are not valid, or other filter validation
            fails.
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ValidationError'
        '500':
          description: Internal server error (DB failures, cursor encode failures)
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/InternalError'
        '503':
          description: Service unavailable when keyset pagination is not configured
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ServiceUnavailableError'
components:
  schemas:
    KeysetMarketsResponse:
      type: object
      properties:
        markets:
          type: array
          description: Array of Market objects. Empty array if none found.
          items:
            $ref: '#/components/schemas/Market'
        next_cursor:
          type: string
          description: >
            Opaque cursor token for fetching the next page. Present only when
            the number of returned markets equals the effective limit. Omitted
            on the last page.
    ValidationError:
      type: object
      properties:
        type:
          type: string
          example: validation error
        error:
          type: string
          example: offset is not allowed on keyset endpoints
    InternalError:
      type: object
      properties:
        type:
          type: string
          example: internal error
        error:
          type: string
          example: cannot get the information
    ServiceUnavailableError:
      type: object
      properties:
        type:
          type: string
          example: service unavailable
        error:
          type: string
          example: keyset pagination is not configured
    Market:
      type: object
      properties:
        id:
          type: string
        question:
          type: string
          nullable: true
        conditionId:
          type: string
        slug:
          type: string
          nullable: true
        twitterCardImage:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        category:
          type: string
          nullable: true
        ammType:
          type: string
          nullable: true
        liquidity:
          type: string
          nullable: true
        sponsorName:
          type: string
          nullable: true
        sponsorImage:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        xAxisValue:
          type: string
          nullable: true
        yAxisValue:
          type: string
          nullable: true
        denominationToken:
          type: string
          nullable: true
        fee:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        lowerBound:
          type: string
          nullable: true
        upperBound:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
        outcomePrices:
          type: string
          nullable: true
        volume:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        marketType:
          type: string
          nullable: true
        formatType:
          type: string
          nullable: true
        lowerBoundDate:
          type: string
          nullable: true
        upperBoundDate:
          type: string
          nullable: true
        closed:
          type: boolean
          nullable: true
        marketMakerAddress:
          type: string
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        closedTime:
          type: string
          nullable: true
        wideFormat:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        mailchimpTag:
          type: string
          nullable: true
        featured:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        resolvedBy:
          type: string
          nullable: true
        restricted:
          type: boolean
          nullable: true
        marketGroup:
          type: integer
          nullable: true
        groupItemTitle:
          type: string
          nullable: true
        groupItemThreshold:
          type: string
          nullable: true
        questionID:
          type: string
          nullable: true
        umaEndDate:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        orderPriceMinTickSize:
          type: number
          nullable: true
        orderMinSize:
          type: number
          nullable: true
        umaResolutionStatus:
          type: string
          nullable: true
        curationOrder:
          type: integer
          nullable: true
        volumeNum:
          type: number
          nullable: true
        liquidityNum:
          type: number
          nullable: true
        endDateIso:
          type: string
          nullable: true
        startDateIso:
          type: string
          nullable: true
        umaEndDateIso:
          type: string
          nullable: true
        hasReviewedDates:
          type: boolean
          nullable: true
        readyForCron:
          type: boolean
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        gameStartTime:
          type: string
          nullable: true
        secondsDelay:
          type: integer
          nullable: true
        clobTokenIds:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        shortOutcomes:
          type: string
          nullable: true
        teamAID:
          type: string
          nullable: true
        teamBID:
          type: string
          nullable: true
        umaBond:
          type: string
          nullable: true
        umaReward:
          type: string
          nullable: true
        fpmmLive:
          type: boolean
          nullable: true
        volume24hrAmm:
          type: number
          nullable: true
        volume1wkAmm:
          type: number
          nullable: true
        volume1moAmm:
          type: number
          nullable: true
        volume1yrAmm:
          type: number
          nullable: true
        volume24hrClob:
          type: number
          nullable: true
        volume1wkClob:
          type: number
          nullable: true
        volume1moClob:
          type: number
          nullable: true
        volume1yrClob:
          type: number
          nullable: true
        volumeAmm:
          type: number
          nullable: true
        volumeClob:
          type: number
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        makerBaseFee:
          type: integer
          nullable: true
        takerBaseFee:
          type: integer
          nullable: true
        customLiveness:
          type: integer
          nullable: true
        acceptingOrders:
          type: boolean
          nullable: true
        notificationsEnabled:
          type: boolean
          nullable: true
        score:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        creator:
          type: string
          nullable: true
        ready:
          type: boolean
          nullable: true
        funded:
          type: boolean
          nullable: true
        pastSlugs:
          type: string
          nullable: true
        readyTimestamp:
          type: string
          format: date-time
          nullable: true
        fundedTimestamp:
          type: string
          format: date-time
          nullable: true
        acceptingOrdersTimestamp:
          type: string
          format: date-time
          nullable: true
        competitive:
          type: number
          nullable: true
        rewardsMinSize:
          type: number
          nullable: true
        rewardsMaxSpread:
          type: number
          nullable: true
        spread:
          type: number
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        oneDayPriceChange:
          type: number
          nullable: true
        oneHourPriceChange:
          type: number
          nullable: true
        oneWeekPriceChange:
          type: number
          nullable: true
        oneMonthPriceChange:
          type: number
          nullable: true
        oneYearPriceChange:
          type: number
          nullable: true
        lastTradePrice:
          type: number
          nullable: true
        bestBid:
          type: number
          nullable: true
        bestAsk:
          type: number
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        clearBookOnStart:
          type: boolean
          nullable: true
        chartColor:
          type: string
          nullable: true
        seriesColor:
          type: string
          nullable: true
        showGmpSeries:
          type: boolean
          nullable: true
        showGmpOutcome:
          type: boolean
          nullable: true
        manualActivation:
          type: boolean
          nullable: true
        negRiskOther:
          type: boolean
          nullable: true
        gameId:
          type: string
          nullable: true
        groupItemRange:
          type: string
          nullable: true
        sportsMarketType:
          type: string
          nullable: true
        line:
          type: number
          nullable: true
        umaResolutionStatuses:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        rfqEnabled:
          type: boolean
          nullable: true
        eventStartTime:
          type: string
          format: date-time
          nullable: true
        feesEnabled:
          type: boolean
          nullable: true
        feeSchedule:
          $ref: '#/components/schemas/FeeSchedule'
    ImageOptimization:
      type: object
      properties:
        id:
          type: string
        imageUrlSource:
          type: string
          nullable: true
        imageUrlOptimized:
          type: string
          nullable: true
        imageSizeKbSource:
          type: number
          nullable: true
        imageSizeKbOptimized:
          type: number
          nullable: true
        imageOptimizedComplete:
          type: boolean
          nullable: true
        imageOptimizedLastUpdated:
          type: string
          nullable: true
        relID:
          type: integer
          nullable: true
        field:
          type: string
          nullable: true
        relname:
          type: string
          nullable: true
    Event:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        creationDate:
          type: string
          format: date-time
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        liquidity:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        openInterest:
          type: number
          nullable: true
        sortBy:
          type: string
          nullable: true
        category:
          type: string
          nullable: true
        subcategory:
          type: string
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        published_at:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: number
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        featuredImage:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        parentEvent:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        negRiskMarketID:
          type: string
          nullable: true
        negRiskFeeBips:
          type: integer
          nullable: true
        commentCount:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        featuredImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        subEvents:
          type: array
          items:
            type: string
          nullable: true
        markets:
          type: array
          items:
            $ref: '#/components/schemas/Market'
        series:
          type: array
          items:
            $ref: '#/components/schemas/Series'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        cyom:
          type: boolean
          nullable: true
        closedTime:
          type: string
          format: date-time
          nullable: true
        showAllOutcomes:
          type: boolean
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        enableNegRisk:
          type: boolean
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        eventDate:
          type: string
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        eventWeek:
          type: integer
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        score:
          type: string
          nullable: true
        elapsed:
          type: string
          nullable: true
        period:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        ended:
          type: boolean
          nullable: true
        finishedTimestamp:
          type: string
          format: date-time
          nullable: true
        gmpChartMode:
          type: string
          nullable: true
        eventCreators:
          type: array
          items:
            $ref: '#/components/schemas/EventCreator'
        tweetCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
        featuredOrder:
          type: integer
          nullable: true
        estimateValue:
          type: boolean
          nullable: true
        cantEstimate:
          type: boolean
          nullable: true
        estimatedValue:
          type: string
          nullable: true
        templates:
          type: array
          items:
            $ref: '#/components/schemas/Template'
        spreadsMainLine:
          type: number
          nullable: true
        totalsMainLine:
          type: number
          nullable: true
        carouselMap:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        gameStatus:
          type: string
          nullable: true
    Category:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        parentCategory:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true
    FeeSchedule:
      type: object
      properties:
        exponent:
          type: number
          nullable: true
        rate:
          type: number
          nullable: true
        takerOnly:
          type: boolean
          nullable: true
        rebateRate:
          type: number
          nullable: true
    Series:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        seriesType:
          type: string
          nullable: true
        recurrence:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: string
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        liquidity:
          type: number
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        pythTokenID:
          type: string
          nullable: true
        cgAssetName:
          type: string
          nullable: true
        score:
          type: integer
          nullable: true
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        commentCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
    Collection:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        collectionType:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        tags:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        headerImage:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        headerImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
    EventCreator:
      type: object
      properties:
        id:
          type: string
        creatorName:
          type: string
          nullable: true
        creatorHandle:
          type: string
          nullable: true
        creatorUrl:
          type: string
          nullable: true
        creatorImage:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Chat:
      type: object
      properties:
        id:
          type: string
        channelId:
          type: string
          nullable: true
        channelName:
          type: string
          nullable: true
        channelImage:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        endTime:
          type: string
          format: date-time
          nullable: true
    Template:
      type: object
      properties:
        id:
          type: string
        eventTitle:
          type: string
          nullable: true
        eventSlug:
          type: string
          nullable: true
        eventImage:
          type: string
          nullable: true
        marketTitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        sortBy:
          type: string
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# List markets



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /markets
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /markets:
    get:
      tags:
        - Markets
      summary: List markets
      operationId: listMarkets
      parameters:
        - $ref: '#/components/parameters/limit'
        - $ref: '#/components/parameters/offset'
        - $ref: '#/components/parameters/order'
        - $ref: '#/components/parameters/ascending'
        - name: id
          in: query
          schema:
            type: array
            items:
              type: integer
        - name: slug
          in: query
          schema:
            type: array
            items:
              type: string
        - name: clob_token_ids
          in: query
          schema:
            type: array
            items:
              type: string
        - name: condition_ids
          in: query
          schema:
            type: array
            items:
              type: string
        - name: market_maker_address
          in: query
          schema:
            type: array
            items:
              type: string
        - name: liquidity_num_min
          in: query
          schema:
            type: number
        - name: liquidity_num_max
          in: query
          schema:
            type: number
        - name: volume_num_min
          in: query
          schema:
            type: number
        - name: volume_num_max
          in: query
          schema:
            type: number
        - name: start_date_min
          in: query
          schema:
            type: string
            format: date-time
        - name: start_date_max
          in: query
          schema:
            type: string
            format: date-time
        - name: end_date_min
          in: query
          schema:
            type: string
            format: date-time
        - name: end_date_max
          in: query
          schema:
            type: string
            format: date-time
        - name: tag_id
          in: query
          schema:
            type: integer
        - name: related_tags
          in: query
          schema:
            type: boolean
        - name: cyom
          in: query
          schema:
            type: boolean
        - name: uma_resolution_status
          in: query
          schema:
            type: string
        - name: game_id
          in: query
          schema:
            type: string
        - name: sports_market_types
          in: query
          schema:
            type: array
            items:
              type: string
        - name: rewards_min_size
          in: query
          schema:
            type: number
        - name: question_ids
          in: query
          schema:
            type: array
            items:
              type: string
        - name: include_tag
          in: query
          schema:
            type: boolean
        - name: closed
          in: query
          schema:
            type: boolean
            default: false
      responses:
        '200':
          description: List of markets
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Market'
components:
  parameters:
    limit:
      name: limit
      in: query
      schema:
        type: integer
        minimum: 0
    offset:
      name: offset
      in: query
      schema:
        type: integer
        minimum: 0
    order:
      name: order
      in: query
      schema:
        type: string
      description: Comma-separated list of fields to order by
    ascending:
      name: ascending
      in: query
      schema:
        type: boolean
  schemas:
    Market:
      type: object
      properties:
        id:
          type: string
        question:
          type: string
          nullable: true
        conditionId:
          type: string
        slug:
          type: string
          nullable: true
        twitterCardImage:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        category:
          type: string
          nullable: true
        ammType:
          type: string
          nullable: true
        liquidity:
          type: string
          nullable: true
        sponsorName:
          type: string
          nullable: true
        sponsorImage:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        xAxisValue:
          type: string
          nullable: true
        yAxisValue:
          type: string
          nullable: true
        denominationToken:
          type: string
          nullable: true
        fee:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        lowerBound:
          type: string
          nullable: true
        upperBound:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
        outcomePrices:
          type: string
          nullable: true
        volume:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        marketType:
          type: string
          nullable: true
        formatType:
          type: string
          nullable: true
        lowerBoundDate:
          type: string
          nullable: true
        upperBoundDate:
          type: string
          nullable: true
        closed:
          type: boolean
          nullable: true
        marketMakerAddress:
          type: string
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        closedTime:
          type: string
          nullable: true
        wideFormat:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        mailchimpTag:
          type: string
          nullable: true
        featured:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        resolvedBy:
          type: string
          nullable: true
        restricted:
          type: boolean
          nullable: true
        marketGroup:
          type: integer
          nullable: true
        groupItemTitle:
          type: string
          nullable: true
        groupItemThreshold:
          type: string
          nullable: true
        questionID:
          type: string
          nullable: true
        umaEndDate:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        orderPriceMinTickSize:
          type: number
          nullable: true
        orderMinSize:
          type: number
          nullable: true
        umaResolutionStatus:
          type: string
          nullable: true
        curationOrder:
          type: integer
          nullable: true
        volumeNum:
          type: number
          nullable: true
        liquidityNum:
          type: number
          nullable: true
        endDateIso:
          type: string
          nullable: true
        startDateIso:
          type: string
          nullable: true
        umaEndDateIso:
          type: string
          nullable: true
        hasReviewedDates:
          type: boolean
          nullable: true
        readyForCron:
          type: boolean
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        gameStartTime:
          type: string
          nullable: true
        secondsDelay:
          type: integer
          nullable: true
        clobTokenIds:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        shortOutcomes:
          type: string
          nullable: true
        teamAID:
          type: string
          nullable: true
        teamBID:
          type: string
          nullable: true
        umaBond:
          type: string
          nullable: true
        umaReward:
          type: string
          nullable: true
        fpmmLive:
          type: boolean
          nullable: true
        volume24hrAmm:
          type: number
          nullable: true
        volume1wkAmm:
          type: number
          nullable: true
        volume1moAmm:
          type: number
          nullable: true
        volume1yrAmm:
          type: number
          nullable: true
        volume24hrClob:
          type: number
          nullable: true
        volume1wkClob:
          type: number
          nullable: true
        volume1moClob:
          type: number
          nullable: true
        volume1yrClob:
          type: number
          nullable: true
        volumeAmm:
          type: number
          nullable: true
        volumeClob:
          type: number
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        makerBaseFee:
          type: integer
          nullable: true
        takerBaseFee:
          type: integer
          nullable: true
        customLiveness:
          type: integer
          nullable: true
        acceptingOrders:
          type: boolean
          nullable: true
        notificationsEnabled:
          type: boolean
          nullable: true
        score:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        creator:
          type: string
          nullable: true
        ready:
          type: boolean
          nullable: true
        funded:
          type: boolean
          nullable: true
        pastSlugs:
          type: string
          nullable: true
        readyTimestamp:
          type: string
          format: date-time
          nullable: true
        fundedTimestamp:
          type: string
          format: date-time
          nullable: true
        acceptingOrdersTimestamp:
          type: string
          format: date-time
          nullable: true
        competitive:
          type: number
          nullable: true
        rewardsMinSize:
          type: number
          nullable: true
        rewardsMaxSpread:
          type: number
          nullable: true
        spread:
          type: number
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        oneDayPriceChange:
          type: number
          nullable: true
        oneHourPriceChange:
          type: number
          nullable: true
        oneWeekPriceChange:
          type: number
          nullable: true
        oneMonthPriceChange:
          type: number
          nullable: true
        oneYearPriceChange:
          type: number
          nullable: true
        lastTradePrice:
          type: number
          nullable: true
        bestBid:
          type: number
          nullable: true
        bestAsk:
          type: number
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        clearBookOnStart:
          type: boolean
          nullable: true
        chartColor:
          type: string
          nullable: true
        seriesColor:
          type: string
          nullable: true
        showGmpSeries:
          type: boolean
          nullable: true
        showGmpOutcome:
          type: boolean
          nullable: true
        manualActivation:
          type: boolean
          nullable: true
        negRiskOther:
          type: boolean
          nullable: true
        gameId:
          type: string
          nullable: true
        groupItemRange:
          type: string
          nullable: true
        sportsMarketType:
          type: string
          nullable: true
        line:
          type: number
          nullable: true
        umaResolutionStatuses:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        rfqEnabled:
          type: boolean
          nullable: true
        eventStartTime:
          type: string
          format: date-time
          nullable: true
        feesEnabled:
          type: boolean
          nullable: true
        feeSchedule:
          $ref: '#/components/schemas/FeeSchedule'
    ImageOptimization:
      type: object
      properties:
        id:
          type: string
        imageUrlSource:
          type: string
          nullable: true
        imageUrlOptimized:
          type: string
          nullable: true
        imageSizeKbSource:
          type: number
          nullable: true
        imageSizeKbOptimized:
          type: number
          nullable: true
        imageOptimizedComplete:
          type: boolean
          nullable: true
        imageOptimizedLastUpdated:
          type: string
          nullable: true
        relID:
          type: integer
          nullable: true
        field:
          type: string
          nullable: true
        relname:
          type: string
          nullable: true
    Event:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        creationDate:
          type: string
          format: date-time
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        liquidity:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        openInterest:
          type: number
          nullable: true
        sortBy:
          type: string
          nullable: true
        category:
          type: string
          nullable: true
        subcategory:
          type: string
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        published_at:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: number
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        featuredImage:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        parentEvent:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        negRiskMarketID:
          type: string
          nullable: true
        negRiskFeeBips:
          type: integer
          nullable: true
        commentCount:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        featuredImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        subEvents:
          type: array
          items:
            type: string
          nullable: true
        markets:
          type: array
          items:
            $ref: '#/components/schemas/Market'
        series:
          type: array
          items:
            $ref: '#/components/schemas/Series'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        cyom:
          type: boolean
          nullable: true
        closedTime:
          type: string
          format: date-time
          nullable: true
        showAllOutcomes:
          type: boolean
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        enableNegRisk:
          type: boolean
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        eventDate:
          type: string
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        eventWeek:
          type: integer
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        score:
          type: string
          nullable: true
        elapsed:
          type: string
          nullable: true
        period:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        ended:
          type: boolean
          nullable: true
        finishedTimestamp:
          type: string
          format: date-time
          nullable: true
        gmpChartMode:
          type: string
          nullable: true
        eventCreators:
          type: array
          items:
            $ref: '#/components/schemas/EventCreator'
        tweetCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
        featuredOrder:
          type: integer
          nullable: true
        estimateValue:
          type: boolean
          nullable: true
        cantEstimate:
          type: boolean
          nullable: true
        estimatedValue:
          type: string
          nullable: true
        templates:
          type: array
          items:
            $ref: '#/components/schemas/Template'
        spreadsMainLine:
          type: number
          nullable: true
        totalsMainLine:
          type: number
          nullable: true
        carouselMap:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        gameStatus:
          type: string
          nullable: true
    Category:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        parentCategory:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true
    FeeSchedule:
      type: object
      properties:
        exponent:
          type: number
          nullable: true
        rate:
          type: number
          nullable: true
        takerOnly:
          type: boolean
          nullable: true
        rebateRate:
          type: number
          nullable: true
    Series:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        seriesType:
          type: string
          nullable: true
        recurrence:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: string
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        liquidity:
          type: number
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        pythTokenID:
          type: string
          nullable: true
        cgAssetName:
          type: string
          nullable: true
        score:
          type: integer
          nullable: true
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        commentCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
    Collection:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        collectionType:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        tags:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        headerImage:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        headerImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
    EventCreator:
      type: object
      properties:
        id:
          type: string
        creatorName:
          type: string
          nullable: true
        creatorHandle:
          type: string
          nullable: true
        creatorUrl:
          type: string
          nullable: true
        creatorImage:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Chat:
      type: object
      properties:
        id:
          type: string
        channelId:
          type: string
          nullable: true
        channelName:
          type: string
          nullable: true
        channelImage:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        endTime:
          type: string
          format: date-time
          nullable: true
    Template:
      type: object
      properties:
        id:
          type: string
        eventTitle:
          type: string
          nullable: true
        eventSlug:
          type: string
          nullable: true
        eventImage:
          type: string
          nullable: true
        marketTitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        sortBy:
          type: string
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get market by id



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /markets/{id}
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /markets/{id}:
    get:
      tags:
        - Markets
      summary: Get market by id
      operationId: getMarket
      parameters:
        - $ref: '#/components/parameters/pathId'
        - name: include_tag
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: Market
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Market'
        '404':
          description: Not found
components:
  parameters:
    pathId:
      name: id
      in: path
      required: true
      schema:
        type: integer
  schemas:
    Market:
      type: object
      properties:
        id:
          type: string
        question:
          type: string
          nullable: true
        conditionId:
          type: string
        slug:
          type: string
          nullable: true
        twitterCardImage:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        category:
          type: string
          nullable: true
        ammType:
          type: string
          nullable: true
        liquidity:
          type: string
          nullable: true
        sponsorName:
          type: string
          nullable: true
        sponsorImage:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        xAxisValue:
          type: string
          nullable: true
        yAxisValue:
          type: string
          nullable: true
        denominationToken:
          type: string
          nullable: true
        fee:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        lowerBound:
          type: string
          nullable: true
        upperBound:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
        outcomePrices:
          type: string
          nullable: true
        volume:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        marketType:
          type: string
          nullable: true
        formatType:
          type: string
          nullable: true
        lowerBoundDate:
          type: string
          nullable: true
        upperBoundDate:
          type: string
          nullable: true
        closed:
          type: boolean
          nullable: true
        marketMakerAddress:
          type: string
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        closedTime:
          type: string
          nullable: true
        wideFormat:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        mailchimpTag:
          type: string
          nullable: true
        featured:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        resolvedBy:
          type: string
          nullable: true
        restricted:
          type: boolean
          nullable: true
        marketGroup:
          type: integer
          nullable: true
        groupItemTitle:
          type: string
          nullable: true
        groupItemThreshold:
          type: string
          nullable: true
        questionID:
          type: string
          nullable: true
        umaEndDate:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        orderPriceMinTickSize:
          type: number
          nullable: true
        orderMinSize:
          type: number
          nullable: true
        umaResolutionStatus:
          type: string
          nullable: true
        curationOrder:
          type: integer
          nullable: true
        volumeNum:
          type: number
          nullable: true
        liquidityNum:
          type: number
          nullable: true
        endDateIso:
          type: string
          nullable: true
        startDateIso:
          type: string
          nullable: true
        umaEndDateIso:
          type: string
          nullable: true
        hasReviewedDates:
          type: boolean
          nullable: true
        readyForCron:
          type: boolean
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        gameStartTime:
          type: string
          nullable: true
        secondsDelay:
          type: integer
          nullable: true
        clobTokenIds:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        shortOutcomes:
          type: string
          nullable: true
        teamAID:
          type: string
          nullable: true
        teamBID:
          type: string
          nullable: true
        umaBond:
          type: string
          nullable: true
        umaReward:
          type: string
          nullable: true
        fpmmLive:
          type: boolean
          nullable: true
        volume24hrAmm:
          type: number
          nullable: true
        volume1wkAmm:
          type: number
          nullable: true
        volume1moAmm:
          type: number
          nullable: true
        volume1yrAmm:
          type: number
          nullable: true
        volume24hrClob:
          type: number
          nullable: true
        volume1wkClob:
          type: number
          nullable: true
        volume1moClob:
          type: number
          nullable: true
        volume1yrClob:
          type: number
          nullable: true
        volumeAmm:
          type: number
          nullable: true
        volumeClob:
          type: number
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        makerBaseFee:
          type: integer
          nullable: true
        takerBaseFee:
          type: integer
          nullable: true
        customLiveness:
          type: integer
          nullable: true
        acceptingOrders:
          type: boolean
          nullable: true
        notificationsEnabled:
          type: boolean
          nullable: true
        score:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        creator:
          type: string
          nullable: true
        ready:
          type: boolean
          nullable: true
        funded:
          type: boolean
          nullable: true
        pastSlugs:
          type: string
          nullable: true
        readyTimestamp:
          type: string
          format: date-time
          nullable: true
        fundedTimestamp:
          type: string
          format: date-time
          nullable: true
        acceptingOrdersTimestamp:
          type: string
          format: date-time
          nullable: true
        competitive:
          type: number
          nullable: true
        rewardsMinSize:
          type: number
          nullable: true
        rewardsMaxSpread:
          type: number
          nullable: true
        spread:
          type: number
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        oneDayPriceChange:
          type: number
          nullable: true
        oneHourPriceChange:
          type: number
          nullable: true
        oneWeekPriceChange:
          type: number
          nullable: true
        oneMonthPriceChange:
          type: number
          nullable: true
        oneYearPriceChange:
          type: number
          nullable: true
        lastTradePrice:
          type: number
          nullable: true
        bestBid:
          type: number
          nullable: true
        bestAsk:
          type: number
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        clearBookOnStart:
          type: boolean
          nullable: true
        chartColor:
          type: string
          nullable: true
        seriesColor:
          type: string
          nullable: true
        showGmpSeries:
          type: boolean
          nullable: true
        showGmpOutcome:
          type: boolean
          nullable: true
        manualActivation:
          type: boolean
          nullable: true
        negRiskOther:
          type: boolean
          nullable: true
        gameId:
          type: string
          nullable: true
        groupItemRange:
          type: string
          nullable: true
        sportsMarketType:
          type: string
          nullable: true
        line:
          type: number
          nullable: true
        umaResolutionStatuses:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        rfqEnabled:
          type: boolean
          nullable: true
        eventStartTime:
          type: string
          format: date-time
          nullable: true
        feesEnabled:
          type: boolean
          nullable: true
        feeSchedule:
          $ref: '#/components/schemas/FeeSchedule'
    ImageOptimization:
      type: object
      properties:
        id:
          type: string
        imageUrlSource:
          type: string
          nullable: true
        imageUrlOptimized:
          type: string
          nullable: true
        imageSizeKbSource:
          type: number
          nullable: true
        imageSizeKbOptimized:
          type: number
          nullable: true
        imageOptimizedComplete:
          type: boolean
          nullable: true
        imageOptimizedLastUpdated:
          type: string
          nullable: true
        relID:
          type: integer
          nullable: true
        field:
          type: string
          nullable: true
        relname:
          type: string
          nullable: true
    Event:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        creationDate:
          type: string
          format: date-time
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        liquidity:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        openInterest:
          type: number
          nullable: true
        sortBy:
          type: string
          nullable: true
        category:
          type: string
          nullable: true
        subcategory:
          type: string
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        published_at:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: number
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        featuredImage:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        parentEvent:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        negRiskMarketID:
          type: string
          nullable: true
        negRiskFeeBips:
          type: integer
          nullable: true
        commentCount:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        featuredImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        subEvents:
          type: array
          items:
            type: string
          nullable: true
        markets:
          type: array
          items:
            $ref: '#/components/schemas/Market'
        series:
          type: array
          items:
            $ref: '#/components/schemas/Series'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        cyom:
          type: boolean
          nullable: true
        closedTime:
          type: string
          format: date-time
          nullable: true
        showAllOutcomes:
          type: boolean
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        enableNegRisk:
          type: boolean
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        eventDate:
          type: string
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        eventWeek:
          type: integer
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        score:
          type: string
          nullable: true
        elapsed:
          type: string
          nullable: true
        period:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        ended:
          type: boolean
          nullable: true
        finishedTimestamp:
          type: string
          format: date-time
          nullable: true
        gmpChartMode:
          type: string
          nullable: true
        eventCreators:
          type: array
          items:
            $ref: '#/components/schemas/EventCreator'
        tweetCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
        featuredOrder:
          type: integer
          nullable: true
        estimateValue:
          type: boolean
          nullable: true
        cantEstimate:
          type: boolean
          nullable: true
        estimatedValue:
          type: string
          nullable: true
        templates:
          type: array
          items:
            $ref: '#/components/schemas/Template'
        spreadsMainLine:
          type: number
          nullable: true
        totalsMainLine:
          type: number
          nullable: true
        carouselMap:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        gameStatus:
          type: string
          nullable: true
    Category:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        parentCategory:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true
    FeeSchedule:
      type: object
      properties:
        exponent:
          type: number
          nullable: true
        rate:
          type: number
          nullable: true
        takerOnly:
          type: boolean
          nullable: true
        rebateRate:
          type: number
          nullable: true
    Series:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        seriesType:
          type: string
          nullable: true
        recurrence:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: string
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        liquidity:
          type: number
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        pythTokenID:
          type: string
          nullable: true
        cgAssetName:
          type: string
          nullable: true
        score:
          type: integer
          nullable: true
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        commentCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
    Collection:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        collectionType:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        tags:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        headerImage:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        headerImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
    EventCreator:
      type: object
      properties:
        id:
          type: string
        creatorName:
          type: string
          nullable: true
        creatorHandle:
          type: string
          nullable: true
        creatorUrl:
          type: string
          nullable: true
        creatorImage:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Chat:
      type: object
      properties:
        id:
          type: string
        channelId:
          type: string
          nullable: true
        channelName:
          type: string
          nullable: true
        channelImage:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        endTime:
          type: string
          format: date-time
          nullable: true
    Template:
      type: object
      properties:
        id:
          type: string
        eventTitle:
          type: string
          nullable: true
        eventSlug:
          type: string
          nullable: true
        eventImage:
          type: string
          nullable: true
        marketTitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        sortBy:
          type: string
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get market by slug



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /markets/slug/{slug}
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /markets/slug/{slug}:
    get:
      tags:
        - Markets
      summary: Get market by slug
      operationId: getMarketBySlug
      parameters:
        - $ref: '#/components/parameters/pathSlug'
        - name: include_tag
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: Market
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Market'
        '404':
          description: Not found
components:
  parameters:
    pathSlug:
      name: slug
      in: path
      required: true
      schema:
        type: string
  schemas:
    Market:
      type: object
      properties:
        id:
          type: string
        question:
          type: string
          nullable: true
        conditionId:
          type: string
        slug:
          type: string
          nullable: true
        twitterCardImage:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        category:
          type: string
          nullable: true
        ammType:
          type: string
          nullable: true
        liquidity:
          type: string
          nullable: true
        sponsorName:
          type: string
          nullable: true
        sponsorImage:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        xAxisValue:
          type: string
          nullable: true
        yAxisValue:
          type: string
          nullable: true
        denominationToken:
          type: string
          nullable: true
        fee:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        lowerBound:
          type: string
          nullable: true
        upperBound:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
        outcomePrices:
          type: string
          nullable: true
        volume:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        marketType:
          type: string
          nullable: true
        formatType:
          type: string
          nullable: true
        lowerBoundDate:
          type: string
          nullable: true
        upperBoundDate:
          type: string
          nullable: true
        closed:
          type: boolean
          nullable: true
        marketMakerAddress:
          type: string
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        closedTime:
          type: string
          nullable: true
        wideFormat:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        mailchimpTag:
          type: string
          nullable: true
        featured:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        resolvedBy:
          type: string
          nullable: true
        restricted:
          type: boolean
          nullable: true
        marketGroup:
          type: integer
          nullable: true
        groupItemTitle:
          type: string
          nullable: true
        groupItemThreshold:
          type: string
          nullable: true
        questionID:
          type: string
          nullable: true
        umaEndDate:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        orderPriceMinTickSize:
          type: number
          nullable: true
        orderMinSize:
          type: number
          nullable: true
        umaResolutionStatus:
          type: string
          nullable: true
        curationOrder:
          type: integer
          nullable: true
        volumeNum:
          type: number
          nullable: true
        liquidityNum:
          type: number
          nullable: true
        endDateIso:
          type: string
          nullable: true
        startDateIso:
          type: string
          nullable: true
        umaEndDateIso:
          type: string
          nullable: true
        hasReviewedDates:
          type: boolean
          nullable: true
        readyForCron:
          type: boolean
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        gameStartTime:
          type: string
          nullable: true
        secondsDelay:
          type: integer
          nullable: true
        clobTokenIds:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        shortOutcomes:
          type: string
          nullable: true
        teamAID:
          type: string
          nullable: true
        teamBID:
          type: string
          nullable: true
        umaBond:
          type: string
          nullable: true
        umaReward:
          type: string
          nullable: true
        fpmmLive:
          type: boolean
          nullable: true
        volume24hrAmm:
          type: number
          nullable: true
        volume1wkAmm:
          type: number
          nullable: true
        volume1moAmm:
          type: number
          nullable: true
        volume1yrAmm:
          type: number
          nullable: true
        volume24hrClob:
          type: number
          nullable: true
        volume1wkClob:
          type: number
          nullable: true
        volume1moClob:
          type: number
          nullable: true
        volume1yrClob:
          type: number
          nullable: true
        volumeAmm:
          type: number
          nullable: true
        volumeClob:
          type: number
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        makerBaseFee:
          type: integer
          nullable: true
        takerBaseFee:
          type: integer
          nullable: true
        customLiveness:
          type: integer
          nullable: true
        acceptingOrders:
          type: boolean
          nullable: true
        notificationsEnabled:
          type: boolean
          nullable: true
        score:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        creator:
          type: string
          nullable: true
        ready:
          type: boolean
          nullable: true
        funded:
          type: boolean
          nullable: true
        pastSlugs:
          type: string
          nullable: true
        readyTimestamp:
          type: string
          format: date-time
          nullable: true
        fundedTimestamp:
          type: string
          format: date-time
          nullable: true
        acceptingOrdersTimestamp:
          type: string
          format: date-time
          nullable: true
        competitive:
          type: number
          nullable: true
        rewardsMinSize:
          type: number
          nullable: true
        rewardsMaxSpread:
          type: number
          nullable: true
        spread:
          type: number
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        oneDayPriceChange:
          type: number
          nullable: true
        oneHourPriceChange:
          type: number
          nullable: true
        oneWeekPriceChange:
          type: number
          nullable: true
        oneMonthPriceChange:
          type: number
          nullable: true
        oneYearPriceChange:
          type: number
          nullable: true
        lastTradePrice:
          type: number
          nullable: true
        bestBid:
          type: number
          nullable: true
        bestAsk:
          type: number
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        clearBookOnStart:
          type: boolean
          nullable: true
        chartColor:
          type: string
          nullable: true
        seriesColor:
          type: string
          nullable: true
        showGmpSeries:
          type: boolean
          nullable: true
        showGmpOutcome:
          type: boolean
          nullable: true
        manualActivation:
          type: boolean
          nullable: true
        negRiskOther:
          type: boolean
          nullable: true
        gameId:
          type: string
          nullable: true
        groupItemRange:
          type: string
          nullable: true
        sportsMarketType:
          type: string
          nullable: true
        line:
          type: number
          nullable: true
        umaResolutionStatuses:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        rfqEnabled:
          type: boolean
          nullable: true
        eventStartTime:
          type: string
          format: date-time
          nullable: true
        feesEnabled:
          type: boolean
          nullable: true
        feeSchedule:
          $ref: '#/components/schemas/FeeSchedule'
    ImageOptimization:
      type: object
      properties:
        id:
          type: string
        imageUrlSource:
          type: string
          nullable: true
        imageUrlOptimized:
          type: string
          nullable: true
        imageSizeKbSource:
          type: number
          nullable: true
        imageSizeKbOptimized:
          type: number
          nullable: true
        imageOptimizedComplete:
          type: boolean
          nullable: true
        imageOptimizedLastUpdated:
          type: string
          nullable: true
        relID:
          type: integer
          nullable: true
        field:
          type: string
          nullable: true
        relname:
          type: string
          nullable: true
    Event:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        creationDate:
          type: string
          format: date-time
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        liquidity:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        openInterest:
          type: number
          nullable: true
        sortBy:
          type: string
          nullable: true
        category:
          type: string
          nullable: true
        subcategory:
          type: string
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        published_at:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: number
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        featuredImage:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        parentEvent:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        negRiskMarketID:
          type: string
          nullable: true
        negRiskFeeBips:
          type: integer
          nullable: true
        commentCount:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        featuredImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        subEvents:
          type: array
          items:
            type: string
          nullable: true
        markets:
          type: array
          items:
            $ref: '#/components/schemas/Market'
        series:
          type: array
          items:
            $ref: '#/components/schemas/Series'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        cyom:
          type: boolean
          nullable: true
        closedTime:
          type: string
          format: date-time
          nullable: true
        showAllOutcomes:
          type: boolean
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        enableNegRisk:
          type: boolean
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        eventDate:
          type: string
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        eventWeek:
          type: integer
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        score:
          type: string
          nullable: true
        elapsed:
          type: string
          nullable: true
        period:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        ended:
          type: boolean
          nullable: true
        finishedTimestamp:
          type: string
          format: date-time
          nullable: true
        gmpChartMode:
          type: string
          nullable: true
        eventCreators:
          type: array
          items:
            $ref: '#/components/schemas/EventCreator'
        tweetCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
        featuredOrder:
          type: integer
          nullable: true
        estimateValue:
          type: boolean
          nullable: true
        cantEstimate:
          type: boolean
          nullable: true
        estimatedValue:
          type: string
          nullable: true
        templates:
          type: array
          items:
            $ref: '#/components/schemas/Template'
        spreadsMainLine:
          type: number
          nullable: true
        totalsMainLine:
          type: number
          nullable: true
        carouselMap:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        gameStatus:
          type: string
          nullable: true
    Category:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        parentCategory:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true
    FeeSchedule:
      type: object
      properties:
        exponent:
          type: number
          nullable: true
        rate:
          type: number
          nullable: true
        takerOnly:
          type: boolean
          nullable: true
        rebateRate:
          type: number
          nullable: true
    Series:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        seriesType:
          type: string
          nullable: true
        recurrence:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: string
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        liquidity:
          type: number
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        pythTokenID:
          type: string
          nullable: true
        cgAssetName:
          type: string
          nullable: true
        score:
          type: integer
          nullable: true
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        commentCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
    Collection:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        collectionType:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        tags:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        headerImage:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        headerImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
    EventCreator:
      type: object
      properties:
        id:
          type: string
        creatorName:
          type: string
          nullable: true
        creatorHandle:
          type: string
          nullable: true
        creatorUrl:
          type: string
          nullable: true
        creatorImage:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Chat:
      type: object
      properties:
        id:
          type: string
        channelId:
          type: string
          nullable: true
        channelName:
          type: string
          nullable: true
        channelImage:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        endTime:
          type: string
          format: date-time
          nullable: true
    Template:
      type: object
      properties:
        id:
          type: string
        eventTitle:
          type: string
          nullable: true
        eventSlug:
          type: string
          nullable: true
        eventImage:
          type: string
          nullable: true
        marketTitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        sortBy:
          type: string
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get market tags by id



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /markets/{id}/tags
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /markets/{id}/tags:
    get:
      tags:
        - Markets
        - Tags
      summary: Get market tags by id
      operationId: getMarketTags
      parameters:
        - $ref: '#/components/parameters/pathId'
      responses:
        '200':
          description: Tags attached to the market
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Tag'
        '404':
          description: Not found
components:
  parameters:
    pathId:
      name: id
      in: path
      required: true
      schema:
        type: integer
  schemas:
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get market by token

> Returns the parent market for a given token ID. Useful when you have
a token ID and need to resolve its parent market without knowing the
condition ID in advance.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /markets-by-token/{token_id}
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /markets-by-token/{token_id}:
    get:
      tags:
        - Markets
      summary: Get market by token
      description: |
        Returns the parent market for a given token ID. Useful when you have
        a token ID and need to resolve its parent market without knowing the
        condition ID in advance.
      operationId: getMarketByToken
      parameters:
        - name: token_id
          in: path
          required: true
          description: The token ID to look up the parent market for
          schema:
            type: string
      responses:
        '200':
          description: Successfully retrieved market
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/MarketByTokenResponse'
        '400':
          description: Invalid market - empty token_id
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '404':
          description: Market not found for token
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    MarketByTokenResponse:
      type: object
      description: >-
        Response for GET /markets-by-token/{token_id} — condition ID and both
        token IDs in the market.
      required:
        - condition_id
        - primary_token_id
        - secondary_token_id
      properties:
        condition_id:
          type: string
          description: The condition ID of the market containing the given token
          example: '0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af'
        primary_token_id:
          type: string
          description: The primary (Yes) token ID
          example: >-
            71321045679252212594626385532706912750332728571942532289631379312455583992563
        secondary_token_id:
          type: string
          description: The secondary (No) token ID
          example: >-
            52114319501245915516055106046884209969926127482827954674443846427813813222426
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get top holders for markets



## OpenAPI

````yaml /api-spec/data-openapi.yaml get /holders
openapi: 3.0.3
info:
  title: Polymarket Data API
  version: 1.0.0
  description: >
    HTTP API for Polymarket data. This specification documents all public
    routes.
servers:
  - url: https://data-api.polymarket.com
    description: Relative server (same host)
security: []
tags:
  - name: Data API Status
    description: Data API health check
  - name: Core
  - name: Builders
  - name: Misc
paths:
  /holders:
    get:
      tags:
        - Core
      summary: Get top holders for markets
      parameters:
        - in: query
          name: limit
          schema:
            type: integer
            default: 20
            minimum: 0
            maximum: 20
          description: Maximum number of holders to return per token. Capped at 20.
        - in: query
          name: market
          required: true
          style: form
          explode: false
          schema:
            type: array
            items:
              $ref: '#/components/schemas/Hash64'
          description: Comma-separated list of condition IDs.
        - in: query
          name: minBalance
          schema:
            type: integer
            default: 1
            minimum: 0
            maximum: 999999
      responses:
        '200':
          description: Success
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/MetaHolder'
        '400':
          description: Bad Request
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '401':
          description: Unauthorized
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Server Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    Hash64:
      type: string
      description: 0x-prefixed 64-hex string
      pattern: ^0x[a-fA-F0-9]{64}$
      example: '0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917'
    MetaHolder:
      type: object
      properties:
        token:
          type: string
        holders:
          type: array
          items:
            $ref: '#/components/schemas/Holder'
    ErrorResponse:
      type: object
      properties:
        error:
          type: string
      required:
        - error
    Holder:
      type: object
      properties:
        proxyWallet:
          $ref: '#/components/schemas/Address'
        bio:
          type: string
        asset:
          type: string
        pseudonym:
          type: string
        amount:
          type: number
        displayUsernamePublic:
          type: boolean
        outcomeIndex:
          type: integer
        name:
          type: string
        profileImage:
          type: string
        profileImageOptimized:
          type: string
    Address:
      type: string
      description: User Profile Address (0x-prefixed, 40 hex chars)
      pattern: ^0x[a-fA-F0-9]{40}$
      example: '0x56687bf447db6ffa42ffe2204a05edaa20f55839'

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get open interest



## OpenAPI

````yaml /api-spec/data-openapi.yaml get /oi
openapi: 3.0.3
info:
  title: Polymarket Data API
  version: 1.0.0
  description: >
    HTTP API for Polymarket data. This specification documents all public
    routes.
servers:
  - url: https://data-api.polymarket.com
    description: Relative server (same host)
security: []
tags:
  - name: Data API Status
    description: Data API health check
  - name: Core
  - name: Builders
  - name: Misc
paths:
  /oi:
    get:
      tags:
        - Misc
      summary: Get open interest
      parameters:
        - in: query
          name: market
          style: form
          explode: false
          schema:
            type: array
            items:
              $ref: '#/components/schemas/Hash64'
      responses:
        '200':
          description: Success
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/OpenInterest'
        '400':
          description: Bad Request
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Server Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    Hash64:
      type: string
      description: 0x-prefixed 64-hex string
      pattern: ^0x[a-fA-F0-9]{64}$
      example: '0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917'
    OpenInterest:
      type: object
      properties:
        market:
          $ref: '#/components/schemas/Hash64'
        value:
          type: number
    ErrorResponse:
      type: object
      properties:
        error:
          type: string
      required:
        - error

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get live volume for an event



## OpenAPI

````yaml /api-spec/data-openapi.yaml get /live-volume
openapi: 3.0.3
info:
  title: Polymarket Data API
  version: 1.0.0
  description: >
    HTTP API for Polymarket data. This specification documents all public
    routes.
servers:
  - url: https://data-api.polymarket.com
    description: Relative server (same host)
security: []
tags:
  - name: Data API Status
    description: Data API health check
  - name: Core
  - name: Builders
  - name: Misc
paths:
  /live-volume:
    get:
      tags:
        - Misc
      summary: Get live volume for an event
      parameters:
        - in: query
          name: id
          required: true
          schema:
            type: integer
            minimum: 1
      responses:
        '200':
          description: Success
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/LiveVolume'
        '400':
          description: Bad Request
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Server Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    LiveVolume:
      type: object
      properties:
        total:
          type: number
        markets:
          type: array
          items:
            $ref: '#/components/schemas/MarketVolume'
    ErrorResponse:
      type: object
      properties:
        error:
          type: string
      required:
        - error
    MarketVolume:
      type: object
      properties:
        market:
          $ref: '#/components/schemas/Hash64'
        value:
          type: number
    Hash64:
      type: string
      description: 0x-prefixed 64-hex string
      pattern: ^0x[a-fA-F0-9]{64}$
      example: '0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917'

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get order book

> Retrieves the order book summary for a specific token ID.
Includes bids, asks, market details, and last trade price.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /book
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /book:
    get:
      tags:
        - Market Data
      summary: Get order book
      description: |
        Retrieves the order book summary for a specific token ID.
        Includes bids, asks, market details, and last trade price.
      operationId: getBook
      parameters:
        - name: token_id
          in: query
          description: Token ID (asset ID)
          required: true
          schema:
            type: string
          example: 0xabc123def456...
      responses:
        '200':
          description: Successfully retrieved order book
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/OrderBookSummary'
              example:
                market: '0x1234567890123456789012345678901234567890'
                asset_id: 0xabc123def456...
                timestamp: '1234567890'
                hash: a1b2c3d4e5f6...
                bids:
                  - price: '0.45'
                    size: '100'
                  - price: '0.44'
                    size: '200'
                asks:
                  - price: '0.46'
                    size: '150'
                  - price: '0.47'
                    size: '250'
                min_order_size: '1'
                tick_size: '0.01'
                neg_risk: false
                last_trade_price: '0.45'
        '400':
          description: Bad request - Invalid token id
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid token id
        '404':
          description: Not found - No orderbook exists for the requested token id
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: No orderbook exists for the requested token id
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: error getting the orderbook
      security: []
components:
  schemas:
    OrderBookSummary:
      type: object
      required:
        - market
        - asset_id
        - timestamp
        - hash
        - bids
        - asks
        - min_order_size
        - tick_size
        - neg_risk
        - last_trade_price
      properties:
        market:
          type: string
          description: Market condition ID
          example: '0x1234567890123456789012345678901234567890'
        asset_id:
          type: string
          description: Token ID (asset ID)
          example: 0xabc123def456...
        timestamp:
          type: string
          description: Timestamp of the order book snapshot
          example: '1234567890'
        hash:
          type: string
          description: Hash of the order book summary
          example: a1b2c3d4e5f6...
        bids:
          type: array
          description: List of bid orders (sorted by price descending)
          items:
            $ref: '#/components/schemas/OrderSummary'
        asks:
          type: array
          description: List of ask orders (sorted by price ascending)
          items:
            $ref: '#/components/schemas/OrderSummary'
        min_order_size:
          type: string
          description: Minimum order size
          example: '1'
        tick_size:
          type: string
          description: Minimum price increment (tick size)
          example: '0.01'
        neg_risk:
          type: boolean
          description: Whether negative risk is enabled for this market
          example: false
        last_trade_price:
          type: string
          description: Last trade price
          example: '0.45'
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
    OrderSummary:
      type: object
      required:
        - price
        - size
      properties:
        price:
          type: string
          description: Order price
          example: '0.45'
        size:
          type: string
          description: Order size
          example: '100'

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get order books (request body)

> Retrieves order book summaries for multiple token IDs using a request body.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml post /books
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /books:
    post:
      tags:
        - Market Data
      summary: Get order books (request body)
      description: >
        Retrieves order book summaries for multiple token IDs using a request
        body.
      operationId: getBooksPost
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: array
              items:
                $ref: '#/components/schemas/BookRequest'
            example:
              - token_id: 0xabc123def456...
              - token_id: 0xdef456abc123...
      responses:
        '200':
          description: Successfully retrieved order books
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/OrderBookSummary'
              example:
                - market: '0x1234567890123456789012345678901234567890'
                  asset_id: 0xabc123def456...
                  timestamp: '1234567890'
                  hash: a1b2c3d4e5f6...
                  bids:
                    - price: '0.45'
                      size: '100'
                  asks:
                    - price: '0.46'
                      size: '150'
                  min_order_size: '1'
                  tick_size: '0.01'
                  neg_risk: false
                  last_trade_price: '0.45'
        '400':
          description: Bad request - Invalid payload
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid payload
      security: []
components:
  schemas:
    BookRequest:
      type: object
      required:
        - token_id
      properties:
        token_id:
          type: string
          description: Token ID (asset ID)
          example: 0xabc123def456...
        side:
          type: string
          description: Order side (optional, not used for midpoint calculation)
          enum:
            - BUY
            - SELL
          example: BUY
    OrderBookSummary:
      type: object
      required:
        - market
        - asset_id
        - timestamp
        - hash
        - bids
        - asks
        - min_order_size
        - tick_size
        - neg_risk
        - last_trade_price
      properties:
        market:
          type: string
          description: Market condition ID
          example: '0x1234567890123456789012345678901234567890'
        asset_id:
          type: string
          description: Token ID (asset ID)
          example: 0xabc123def456...
        timestamp:
          type: string
          description: Timestamp of the order book snapshot
          example: '1234567890'
        hash:
          type: string
          description: Hash of the order book summary
          example: a1b2c3d4e5f6...
        bids:
          type: array
          description: List of bid orders (sorted by price descending)
          items:
            $ref: '#/components/schemas/OrderSummary'
        asks:
          type: array
          description: List of ask orders (sorted by price ascending)
          items:
            $ref: '#/components/schemas/OrderSummary'
        min_order_size:
          type: string
          description: Minimum order size
          example: '1'
        tick_size:
          type: string
          description: Minimum price increment (tick size)
          example: '0.01'
        neg_risk:
          type: boolean
          description: Whether negative risk is enabled for this market
          example: false
        last_trade_price:
          type: string
          description: Last trade price
          example: '0.45'
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
    OrderSummary:
      type: object
      required:
        - price
        - size
      properties:
        price:
          type: string
          description: Order price
          example: '0.45'
        size:
          type: string
          description: Order size
          example: '100'

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get market price

> Retrieves the best market price for a specific token ID and side (bid or ask).
Returns the best bid price for BUY side or best ask price for SELL side.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /price
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /price:
    get:
      tags:
        - Market Data
      summary: Get market price
      description: >
        Retrieves the best market price for a specific token ID and side (bid or
        ask).

        Returns the best bid price for BUY side or best ask price for SELL side.
      operationId: getPrice
      parameters:
        - name: token_id
          in: query
          description: Token ID (asset ID)
          required: true
          schema:
            type: string
          example: 0xabc123def456...
        - name: side
          in: query
          description: Order side
          required: true
          schema:
            type: string
            enum:
              - BUY
              - SELL
          example: BUY
      responses:
        '200':
          description: Successfully retrieved market price
          content:
            application/json:
              schema:
                type: object
                required:
                  - price
                properties:
                  price:
                    type: number
                    format: double
                    description: Market price as a decimal number
                    example: 0.45
              example:
                price: 0.45
        '400':
          description: Bad request - Invalid token id or side
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                invalid_token_id:
                  summary: Invalid token id
                  value:
                    error: Invalid token id
                invalid_side:
                  summary: Invalid side
                  value:
                    error: Invalid side
        '404':
          description: Not found - No orderbook exists for the requested token id
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: No orderbook exists for the requested token id
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security: []
components:
  schemas:
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get market prices (query parameters)

> Retrieves market prices for multiple token IDs and sides using query parameters.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /prices
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /prices:
    get:
      tags:
        - Market Data
      summary: Get market prices (query parameters)
      description: >
        Retrieves market prices for multiple token IDs and sides using query
        parameters.
      operationId: getPricesGet
      parameters:
        - name: token_ids
          in: query
          description: Comma-separated list of token IDs
          required: true
          schema:
            type: string
          example: 0xabc123...,0xdef456...
        - name: sides
          in: query
          description: >-
            Comma-separated list of sides (BUY or SELL) corresponding to token
            IDs
          required: true
          schema:
            type: string
          example: BUY,SELL
      responses:
        '200':
          description: Successfully retrieved market prices
          content:
            application/json:
              schema:
                type: object
                additionalProperties:
                  type: object
                  additionalProperties:
                    type: number
                    format: double
                description: Map of token ID to map of side to price
              example:
                0xabc123def456...:
                  BUY: 0.45
                0xdef456abc123...:
                  SELL: 0.52
        '400':
          description: Bad request - Invalid payload or side
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                invalid_payload:
                  summary: Invalid payload
                  value:
                    error: Invalid payload
                invalid_side:
                  summary: Invalid side
                  value:
                    error: Invalid side
        '404':
          description: Not found - No orderbook exists for the requested token id
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: No orderbook exists for the requested token id
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security: []
components:
  schemas:
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get market prices (request body)

> Retrieves market prices for multiple token IDs and sides using a request body.
Each request must include both token_id and side.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml post /prices
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /prices:
    post:
      tags:
        - Market Data
      summary: Get market prices (request body)
      description: >
        Retrieves market prices for multiple token IDs and sides using a request
        body.

        Each request must include both token_id and side.
      operationId: getPricesPost
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: array
              items:
                $ref: '#/components/schemas/BookRequest'
            example:
              - token_id: 0xabc123def456...
                side: BUY
              - token_id: 0xdef456abc123...
                side: SELL
      responses:
        '200':
          description: Successfully retrieved market prices
          content:
            application/json:
              schema:
                type: object
                additionalProperties:
                  type: object
                  additionalProperties:
                    type: number
                    format: double
                description: Map of token ID to map of side to price
              example:
                0xabc123def456...:
                  BUY: 0.45
                0xdef456abc123...:
                  SELL: 0.52
        '400':
          description: Bad request - Invalid payload or side
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                invalid_payload:
                  summary: Invalid payload
                  value:
                    error: Invalid payload
                invalid_side:
                  summary: Invalid side
                  value:
                    error: Invalid side
        '404':
          description: Not found - No orderbook exists for the requested token id
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: No orderbook exists for the requested token id
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security: []
components:
  schemas:
    BookRequest:
      type: object
      required:
        - token_id
      properties:
        token_id:
          type: string
          description: Token ID (asset ID)
          example: 0xabc123def456...
        side:
          type: string
          description: Order side (optional, not used for midpoint calculation)
          enum:
            - BUY
            - SELL
          example: BUY
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get midpoint price

> Retrieves the midpoint price for a specific token ID.
The midpoint is calculated as the average of the best bid and best ask prices.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /midpoint
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /midpoint:
    get:
      tags:
        - Data
      summary: Get midpoint price
      description: >
        Retrieves the midpoint price for a specific token ID.

        The midpoint is calculated as the average of the best bid and best ask
        prices.
      operationId: getMidpoint
      parameters:
        - name: token_id
          in: query
          description: Token ID (asset ID)
          required: true
          schema:
            type: string
          example: 0xabc123def456...
      responses:
        '200':
          description: Successfully retrieved midpoint price
          content:
            application/json:
              schema:
                type: object
                required:
                  - mid_price
                properties:
                  mid_price:
                    type: string
                    description: Midpoint price as a string
                    example: '0.45'
        '400':
          description: Bad request - Invalid token id
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid token id
        '404':
          description: Not found - No orderbook exists for the requested token id
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: No orderbook exists for the requested token id
      security: []
components:
  schemas:
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get midpoint prices (query parameters)

> Retrieves midpoint prices for multiple token IDs using query parameters.
The midpoint is calculated as the average of the best bid and best ask prices.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /midpoints
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /midpoints:
    get:
      tags:
        - Market Data
      summary: Get midpoint prices (query parameters)
      description: >
        Retrieves midpoint prices for multiple token IDs using query parameters.

        The midpoint is calculated as the average of the best bid and best ask
        prices.
      operationId: getMidpointsGet
      parameters:
        - name: token_ids
          in: query
          description: Comma-separated list of token IDs
          required: true
          schema:
            type: string
          example: 0xabc123...,0xdef456...
      responses:
        '200':
          description: Successfully retrieved midpoint prices
          content:
            application/json:
              schema:
                type: object
                additionalProperties:
                  type: string
                description: Map of token ID to midpoint price
              example:
                0xabc123def456...: '0.45'
                0xdef456abc123...: '0.52'
        '400':
          description: Bad request - Invalid payload
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid payload
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: error getting the mid price
      security: []
components:
  schemas:
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get midpoint prices (request body)

> Retrieves midpoint prices for multiple token IDs using a request body.
The midpoint is calculated as the average of the best bid and best ask prices.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml post /midpoints
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /midpoints:
    post:
      tags:
        - Market Data
      summary: Get midpoint prices (request body)
      description: >
        Retrieves midpoint prices for multiple token IDs using a request body.

        The midpoint is calculated as the average of the best bid and best ask
        prices.
      operationId: getMidpointsPost
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: array
              items:
                $ref: '#/components/schemas/BookRequest'
            example:
              - token_id: 0xabc123def456...
              - token_id: 0xdef456abc123...
      responses:
        '200':
          description: Successfully retrieved midpoint prices
          content:
            application/json:
              schema:
                type: object
                additionalProperties:
                  type: string
                description: Map of token ID to midpoint price
              example:
                0xabc123def456...: '0.45'
                0xdef456abc123...: '0.52'
        '400':
          description: Bad request - Invalid payload
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid payload
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: error getting the mid price
      security: []
components:
  schemas:
    BookRequest:
      type: object
      required:
        - token_id
      properties:
        token_id:
          type: string
          description: Token ID (asset ID)
          example: 0xabc123def456...
        side:
          type: string
          description: Order side (optional, not used for midpoint calculation)
          enum:
            - BUY
            - SELL
          example: BUY
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get spread

> Retrieves the spread for a specific token ID.
The spread is the difference between the best ask and best bid prices.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /spread
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /spread:
    get:
      tags:
        - Market Data
      summary: Get spread
      description: |
        Retrieves the spread for a specific token ID.
        The spread is the difference between the best ask and best bid prices.
      operationId: getSpread
      parameters:
        - name: token_id
          in: query
          description: Token ID (asset ID)
          required: true
          schema:
            type: string
          example: 0xabc123def456...
      responses:
        '200':
          description: Successfully retrieved spread
          content:
            application/json:
              schema:
                type: object
                required:
                  - spread
                properties:
                  spread:
                    type: string
                    description: Spread as a string
                    example: '0.02'
        '400':
          description: Bad request - Invalid token id
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid token id
        '404':
          description: Not found - No orderbook exists for the requested token id
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: No orderbook exists for the requested token id
      security: []
components:
  schemas:
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get spreads

> Retrieves spreads for multiple token IDs.
The spread is the difference between the best ask and best bid prices.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml post /spreads
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /spreads:
    post:
      tags:
        - Market Data
      summary: Get spreads
      description: |
        Retrieves spreads for multiple token IDs.
        The spread is the difference between the best ask and best bid prices.
      operationId: getSpreads
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: array
              items:
                $ref: '#/components/schemas/BookRequest'
            example:
              - token_id: 0xabc123def456...
              - token_id: 0xdef456abc123...
      responses:
        '200':
          description: Successfully retrieved spreads
          content:
            application/json:
              schema:
                type: object
                additionalProperties:
                  type: string
                description: Map of token ID to spread
              example:
                0xabc123def456...: '0.02'
                0xdef456abc123...: '0.015'
        '400':
          description: Bad request - Invalid payload
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid payload
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: error getting the spread
      security: []
components:
  schemas:
    BookRequest:
      type: object
      required:
        - token_id
      properties:
        token_id:
          type: string
          description: Token ID (asset ID)
          example: 0xabc123def456...
        side:
          type: string
          description: Order side (optional, not used for midpoint calculation)
          enum:
            - BUY
            - SELL
          example: BUY
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get last trade price

> Retrieves the last trade price and side for a specific token ID.
Returns default values of "0.5" for price and empty string for side if no trades found.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /last-trade-price
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /last-trade-price:
    get:
      tags:
        - Market Data
      summary: Get last trade price
      description: >
        Retrieves the last trade price and side for a specific token ID.

        Returns default values of "0.5" for price and empty string for side if
        no trades found.
      operationId: getLastTradePrice
      parameters:
        - name: token_id
          in: query
          description: Token ID (asset ID)
          required: true
          schema:
            type: string
          example: 0xabc123def456...
      responses:
        '200':
          description: Successfully retrieved last trade price
          content:
            application/json:
              schema:
                type: object
                required:
                  - price
                  - side
                properties:
                  price:
                    type: string
                    description: Last trade price
                    example: '0.45'
                  side:
                    type: string
                    description: Last trade side (BUY or SELL)
                    enum:
                      - BUY
                      - SELL
                      - ''
                    example: BUY
        '400':
          description: Bad request - Invalid token id
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid token id
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security: []
components:
  schemas:
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get last trade prices (query parameters)

> Retrieves last trade prices for multiple token IDs using query parameters.
Maximum 500 token IDs can be requested per call.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /last-trades-prices
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /last-trades-prices:
    get:
      tags:
        - Market Data
      summary: Get last trade prices (query parameters)
      description: >
        Retrieves last trade prices for multiple token IDs using query
        parameters.

        Maximum 500 token IDs can be requested per call.
      operationId: getLastTradesPricesGet
      parameters:
        - name: token_ids
          in: query
          description: Comma-separated list of token IDs (max 500)
          required: true
          schema:
            type: string
          example: 0xabc123...,0xdef456...
      responses:
        '200':
          description: Successfully retrieved last trade prices
          content:
            application/json:
              schema:
                type: array
                items:
                  type: object
                  required:
                    - token_id
                    - price
                    - side
                  properties:
                    token_id:
                      type: string
                      description: Token ID (asset ID)
                      example: 0xabc123def456...
                    price:
                      type: string
                      description: Last trade price
                      example: '0.45'
                    side:
                      type: string
                      description: Last trade side (BUY or SELL)
                      enum:
                        - BUY
                        - SELL
                      example: BUY
              example:
                - token_id: 0xabc123def456...
                  price: '0.45'
                  side: BUY
                - token_id: 0xdef456abc123...
                  price: '0.52'
                  side: SELL
        '400':
          description: Bad request - Invalid payload or exceeds limit
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                invalid_payload:
                  summary: Invalid payload
                  value:
                    error: Invalid payload
                exceeds_limit:
                  summary: Payload exceeds limit
                  value:
                    error: Payload exceeds the limit
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security: []
components:
  schemas:
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get last trade prices (request body)

> Retrieves last trade prices for multiple token IDs using a request body.
Maximum 500 token IDs can be requested per call.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml post /last-trades-prices
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /last-trades-prices:
    post:
      tags:
        - Market Data
      summary: Get last trade prices (request body)
      description: |
        Retrieves last trade prices for multiple token IDs using a request body.
        Maximum 500 token IDs can be requested per call.
      operationId: getLastTradesPricesPost
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: array
              items:
                $ref: '#/components/schemas/BookRequest'
            example:
              - token_id: 0xabc123def456...
              - token_id: 0xdef456abc123...
      responses:
        '200':
          description: Successfully retrieved last trade prices
          content:
            application/json:
              schema:
                type: array
                items:
                  type: object
                  required:
                    - token_id
                    - price
                    - side
                  properties:
                    token_id:
                      type: string
                      description: Token ID (asset ID)
                      example: 0xabc123def456...
                    price:
                      type: string
                      description: Last trade price
                      example: '0.45'
                    side:
                      type: string
                      description: Last trade side (BUY or SELL)
                      enum:
                        - BUY
                        - SELL
                      example: BUY
              example:
                - token_id: 0xabc123def456...
                  price: '0.45'
                  side: BUY
                - token_id: 0xdef456abc123...
                  price: '0.52'
                  side: SELL
        '400':
          description: Bad request - Invalid payload or exceeds limit
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                invalid_payload:
                  summary: Invalid payload
                  value:
                    error: Invalid payload
                exceeds_limit:
                  summary: Payload exceeds limit
                  value:
                    error: Payload exceeds the limit
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security: []
components:
  schemas:
    BookRequest:
      type: object
      required:
        - token_id
      properties:
        token_id:
          type: string
          description: Token ID (asset ID)
          example: 0xabc123def456...
        side:
          type: string
          description: Order side (optional, not used for midpoint calculation)
          enum:
            - BUY
            - SELL
          example: BUY
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get prices history

> Retrieve historical price data for a market.



## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /prices-history
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /prices-history:
    get:
      tags:
        - Markets
      summary: Get prices history
      description: Retrieve historical price data for a market.
      operationId: getPricesHistory
      parameters:
        - name: market
          in: query
          required: true
          description: The market (asset id) to query.
          schema:
            type: string
        - name: startTs
          in: query
          required: false
          description: Filter by items after this unix timestamp.
          schema:
            type: number
            format: double
        - name: endTs
          in: query
          required: false
          description: Filter by items before this unix timestamp.
          schema:
            type: number
            format: double
        - name: interval
          in: query
          required: false
          description: Time interval for data aggregation.
          schema:
            type: string
            enum:
              - max
              - all
              - 1m
              - 1w
              - 1d
              - 6h
              - 1h
        - name: fidelity
          in: query
          required: false
          description: Accuracy of the data expressed in minutes. Default is 1 minute.
          schema:
            type: integer
      responses:
        '200':
          description: Successful response with price history
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/PricesHistoryResponse'
        '400':
          description: Bad Request - Missing or invalid query parameters
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
      security: []
components:
  schemas:
    PricesHistoryResponse:
      type: object
      properties:
        history:
          type: array
          items:
            $ref: '#/components/schemas/MarketPrice'
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
    MarketPrice:
      type: object
      properties:
        t:
          type: integer
          format: uint32
        p:
          type: number
          format: float

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get batch prices history

> Retrieve historical price data for multiple markets in a single request.



## OpenAPI

````yaml /api-spec/clob-openapi.yaml post /batch-prices-history
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /batch-prices-history:
    post:
      tags:
        - Markets
      summary: Get batch prices history
      description: Retrieve historical price data for multiple markets in a single request.
      operationId: getBatchPricesHistory
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/BatchPricesHistoryRequest'
      responses:
        '200':
          description: Successful response with price history for each market
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/BatchPricesHistoryResponse'
        '400':
          description: Bad Request - Missing or invalid parameters
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
      security: []
components:
  schemas:
    BatchPricesHistoryRequest:
      type: object
      required:
        - markets
      properties:
        markets:
          type: array
          items:
            type: string
          description: List of market asset ids to query. Maximum 20.
          maxItems: 20
        start_ts:
          type: number
          format: double
          description: Filter by items after this unix timestamp (seconds).
        end_ts:
          type: number
          format: double
          description: Filter by items before this unix timestamp (seconds).
        interval:
          type: string
          enum:
            - max
            - all
            - 1m
            - 1w
            - 1d
            - 6h
            - 1h
          description: Time interval for data aggregation.
        fidelity:
          type: integer
          description: Accuracy of the data expressed in minutes. Default is 1 minute.
    BatchPricesHistoryResponse:
      type: object
      properties:
        history:
          type: object
          additionalProperties:
            type: array
            items:
              $ref: '#/components/schemas/MarketPrice'
          description: Map of market asset id to array of price data points.
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
    MarketPrice:
      type: object
      properties:
        t:
          type: integer
          format: uint32
        p:
          type: number
          format: float

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get fee rate

> Retrieves the base fee rate for a specific token ID.
The fee rate can be provided either as a query parameter or as a path parameter.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /fee-rate
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /fee-rate:
    get:
      tags:
        - Market Data
      summary: Get fee rate
      description: >
        Retrieves the base fee rate for a specific token ID.

        The fee rate can be provided either as a query parameter or as a path
        parameter.
      operationId: getFeeRate
      parameters:
        - name: token_id
          in: query
          description: Token ID (asset ID)
          required: false
          schema:
            type: string
          example: 0xabc123def456...
      responses:
        '200':
          description: Successfully retrieved fee rate
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/FeeRate'
              example:
                base_fee: 30
        '400':
          description: Bad request - Invalid token id
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid token id
        '404':
          description: Not found - Fee rate not found for market
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: fee rate not found for market
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security: []
components:
  schemas:
    FeeRate:
      type: object
      required:
        - base_fee
      properties:
        base_fee:
          type: integer
          format: int64
          description: Base fee in basis points
          example: 30
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get fee rate by path parameter

> Retrieves the base fee rate for a specific token ID using the token ID as a path parameter.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /fee-rate/{token_id}
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /fee-rate/{token_id}:
    get:
      tags:
        - Market Data
      summary: Get fee rate by path parameter
      description: >
        Retrieves the base fee rate for a specific token ID using the token ID
        as a path parameter.
      operationId: getFeeRateByPath
      parameters:
        - name: token_id
          in: path
          description: Token ID (asset ID)
          required: true
          schema:
            type: string
          example: 0xabc123def456...
      responses:
        '200':
          description: Successfully retrieved fee rate
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/FeeRate'
              example:
                base_fee: 30
        '400':
          description: Bad request - Invalid token id
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid token id
        '404':
          description: Not found - Fee rate not found for market
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: fee rate not found for market
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security: []
components:
  schemas:
    FeeRate:
      type: object
      required:
        - base_fee
      properties:
        base_fee:
          type: integer
          format: int64
          description: Base fee in basis points
          example: 30
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get tick size

> Retrieves the minimum tick size (price increment) for a specific token ID.
The tick size can be provided either as a query parameter or as a path parameter.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /tick-size
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /tick-size:
    get:
      tags:
        - Market Data
      summary: Get tick size
      description: >
        Retrieves the minimum tick size (price increment) for a specific token
        ID.

        The tick size can be provided either as a query parameter or as a path
        parameter.
      operationId: getTickSize
      parameters:
        - name: token_id
          in: query
          description: Token ID (asset ID)
          required: false
          schema:
            type: string
          example: 0xabc123def456...
      responses:
        '200':
          description: Successfully retrieved tick size
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/TickSize'
              example:
                minimum_tick_size: 0.01
        '400':
          description: Bad request - Invalid token id
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid token id
        '404':
          description: Not found - Market not found
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: market not found
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security: []
components:
  schemas:
    TickSize:
      type: object
      required:
        - minimum_tick_size
      properties:
        minimum_tick_size:
          type: number
          format: double
          description: Minimum tick size (price increment)
          example: 0.01
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get tick size by path parameter

> Retrieves the minimum tick size (price increment) for a specific token ID using the token ID as a path parameter.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /tick-size/{token_id}
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /tick-size/{token_id}:
    get:
      tags:
        - Market Data
      summary: Get tick size by path parameter
      description: >
        Retrieves the minimum tick size (price increment) for a specific token
        ID using the token ID as a path parameter.
      operationId: getTickSizeByPath
      parameters:
        - name: token_id
          in: path
          description: Token ID (asset ID)
          required: true
          schema:
            type: string
          example: 0xabc123def456...
      responses:
        '200':
          description: Successfully retrieved tick size
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/TickSize'
              example:
                minimum_tick_size: 0.01
        '400':
          description: Bad request - Invalid token id
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid token id
        '404':
          description: Not found - Market not found
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: market not found
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security: []
components:
  schemas:
    TickSize:
      type: object
      required:
        - minimum_tick_size
      properties:
        minimum_tick_size:
          type: number
          format: double
          description: Minimum tick size (price increment)
          example: 0.01
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get CLOB market info

> Returns all CLOB-level parameters for a market in a single call —
tokens, tick size, base fees, rewards, RFQ status, and fee details.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /clob-markets/{condition_id}
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /clob-markets/{condition_id}:
    get:
      tags:
        - Markets
      summary: Get CLOB market info
      description: |
        Returns all CLOB-level parameters for a market in a single call —
        tokens, tick size, base fees, rewards, RFQ status, and fee details.
      operationId: getClobMarketInfo
      parameters:
        - name: condition_id
          in: path
          required: true
          description: The condition ID of the market
          schema:
            type: string
          example: '0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af'
      responses:
        '200':
          description: Successfully retrieved CLOB market info
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ClobMarketDetails'
        '400':
          description: Bad request - Invalid condition ID
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    ClobMarketDetails:
      type: object
      description: >-
        CLOB-level parameters for a market — tokens, tick size, base fees,
        rewards, RFQ status, and fee details.
      properties:
        gst:
          type: string
          format: date-time
          nullable: true
          description: >-
            Game start time (used for sports markets), ISO 8601 timestamp or
            null
        r:
          $ref: '#/components/schemas/ClobRewards'
        t:
          type: array
          description: Tokens for this market
          items:
            $ref: '#/components/schemas/ClobToken'
        mos:
          type: number
          format: float
          description: Minimum order size
          example: 5
        mts:
          type: number
          format: float
          description: Minimum tick size (price increment)
          example: 0.01
        mbf:
          type: integer
          format: int64
          description: Maker base fee in basis points
          example: 0
        tbf:
          type: integer
          format: int64
          description: Taker base fee in basis points
          example: 0
        rfqe:
          type: boolean
          description: Whether RFQ (Request for Quote) is enabled for this market
        itode:
          type: boolean
          description: Whether taker order delay is enabled
        ibce:
          type: boolean
          description: Whether Blockaid check is enabled
        fd:
          $ref: '#/components/schemas/FeeDetails'
        oas:
          type: integer
          description: Minimum order age in seconds
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
    ClobRewards:
      type: object
      description: Rewards configuration for a market.
      additionalProperties: true
    ClobToken:
      type: object
      description: A token in a CLOB market with its ID and outcome label.
      properties:
        t:
          type: string
          description: The token ID
          example: >-
            71321045679252212594626385532706912750332728571942532289631379312455583992563
        o:
          type: string
          description: Outcome label for the token (e.g. "Yes", "No")
          example: 'Yes'
    FeeDetails:
      type: object
      description: Fee curve parameters for a market.
      properties:
        r:
          type: number
          format: float
          nullable: true
          description: Fee rate
          example: 0.02
        e:
          type: number
          format: float
          nullable: true
          description: Fee curve exponent
          example: 2
        to:
          type: boolean
          nullable: true
          description: Whether fees apply to takers only
          example: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get server time

> Returns the current Unix timestamp of the server.
This can be used to synchronize client time with server time.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /time
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /time:
    get:
      tags:
        - Data
      summary: Get server time
      description: |
        Returns the current Unix timestamp of the server.
        This can be used to synchronize client time with server time.
      operationId: getTime
      responses:
        '200':
          description: Successfully retrieved server time
          content:
            application/json:
              schema:
                type: integer
                format: int64
                description: Unix timestamp (seconds since epoch)
              example: 1234567890
        '400':
          description: Bad request
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
      security: []
components:
  schemas:
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Post a new order

> Creates a new order in the order book




## OpenAPI

````yaml /api-spec/clob-openapi.yaml post /order
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /order:
    post:
      tags:
        - Trade
      summary: Post a new order
      description: |
        Creates a new order in the order book
      operationId: postOrder
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/SendOrder'
            examples:
              example:
                summary: Send order example
                value:
                  order:
                    maker: '0x1234567890123456789012345678901234567890'
                    signer: '0x1234567890123456789012345678901234567890'
                    tokenId: 0xabc123def456...
                    makerAmount: '100000000'
                    takerAmount: '200000000'
                    side: BUY
                    expiration: '1735689600'
                    timestamp: '1735689600000'
                    metadata: ''
                    builder: >-
                      0x0000000000000000000000000000000000000000000000000000000000000000
                    signature: 0x1234abcd...
                    salt: 1234567890
                    signatureType: 0
                  owner: f4f247b7-4ac7-ff29-a152-04fda0a8755a
                  orderType: GTC
                  deferExec: false
                  postOnly: false
      responses:
        '200':
          description: Order successfully processed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/SendOrderResponse'
              examples:
                live_order:
                  summary: Order placed on book
                  value:
                    success: true
                    orderID: '0xabcdef1234567890abcdef1234567890abcdef12'
                    status: live
                    makingAmount: '100000000'
                    takingAmount: '200000000'
                    errorMsg: ''
                matched_order:
                  summary: Order immediately matched
                  value:
                    success: true
                    orderID: '0xabcdef1234567890abcdef1234567890abcdef12'
                    status: matched
                    makingAmount: '100000000'
                    takingAmount: '200000000'
                    transactionsHashes:
                      - '0x1234567890abcdef1234567890abcdef12345678'
                    tradeIDs:
                      - trade-123
                    errorMsg: ''
                delayed_order:
                  summary: Order delayed
                  value:
                    success: true
                    orderID: '0xabcdef1234567890abcdef1234567890abcdef12'
                    status: delayed
                    makingAmount: '100000000'
                    takingAmount: '200000000'
                    errorMsg: ''
        '400':
          description: Bad request - Invalid order payload or validation error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                invalid_payload:
                  summary: Invalid order payload
                  value:
                    error: Invalid order payload
                owner_mismatch:
                  summary: Owner mismatch
                  value:
                    error: the order owner has to be the owner of the API KEY
                signer_mismatch:
                  summary: Signer mismatch
                  value:
                    error: >-
                      the order signer address has to be the address of the API
                      KEY
                banned_address:
                  summary: Banned address
                  value:
                    error: '''0x1234...'' address banned'
                closed_only_mode:
                  summary: Closed only mode violation
                  value:
                    error: '''0x1234...'' address in closed only mode'
                invalid_order:
                  summary: Invalid order details
                  value:
                    error: >-
                      order 0xabc... is invalid. Price (100) breaks minimum tick
                      size rule: 0.1
        '401':
          description: Unauthorized - Invalid API key or authentication failed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid API key
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: could not insert order
        '503':
          description: Service unavailable - Trading disabled or cancel-only mode
          headers:
            Retry-After:
              description: Seconds to wait before retrying when provided by post-only mode.
              schema:
                type: integer
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                trading_disabled:
                  summary: Trading disabled
                  value:
                    error: >-
                      Trading is currently disabled. Check polymarket.com for
                      updates
                cancel_only:
                  summary: Cancel-only mode
                  value:
                    error: >-
                      Trading is currently cancel-only. New orders are not
                      accepted, but cancels are allowed.
                post_only_mode:
                  summary: Post-only mode
                  value:
                    error: >-
                      post-only mode: only post-only orders and cancels are
                      allowed
                    code: post_only_mode
                    retry_after_seconds: 79
      security:
        - polyApiKey: []
          polyAddress: []
          polySignature: []
          polyPassphrase: []
          polyTimestamp: []
components:
  schemas:
    SendOrder:
      type: object
      required:
        - order
        - owner
      properties:
        order:
          $ref: '#/components/schemas/Order'
        owner:
          type: string
          description: UUID of the API key owner
          example: f4f247b7-4ac7-ff29-a152-04fda0a8755a
        orderType:
          type: string
          description: Time in force
          enum:
            - GTC
            - FOK
            - GTD
            - FAK
          default: GTC
        deferExec:
          type: boolean
          description: Whether to defer execution
          default: false
        postOnly:
          type: boolean
          description: >-
            Whether the order must rest on the book and not match immediately.
            Only supported for GTC and GTD orders.
          default: false
    SendOrderResponse:
      type: object
      required:
        - success
        - orderID
        - status
      properties:
        success:
          type: boolean
          description: Whether the order was successfully processed
          example: true
        orderID:
          type: string
          description: Unique identifier for the order (order hash)
          example: '0xabcdef1234567890abcdef1234567890abcdef12'
        status:
          type: string
          description: Status of the order after processing
          enum:
            - live
            - matched
            - delayed
        makingAmount:
          type: string
          description: Amount the maker is providing in fixed-math with 6 decimals
          example: '100000000'
        takingAmount:
          type: string
          description: Amount the taker is providing in fixed-math with 6 decimals
          example: '200000000'
        transactionsHashes:
          type: array
          description: Array of transaction hashes (present when status is 'matched')
          items:
            type: string
          example:
            - '0x1234567890abcdef1234567890abcdef12345678'
        tradeIDs:
          type: array
          description: Array of trade IDs (present when status is 'matched')
          items:
            type: string
        errorMsg:
          type: string
          description: Error message (empty on success)
          example: ''
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
    Order:
      type: object
      description: >
        Order payload submitted to the CLOB API. In CLOB V2, `expiration`
        remains in

        the POST /order wire body for GTD/order-expiry handling, but it is not
        part

        of the EIP-712 signed order struct.
      required:
        - maker
        - signer
        - tokenId
        - makerAmount
        - takerAmount
        - side
        - expiration
        - timestamp
        - builder
        - signature
        - salt
        - signatureType
      properties:
        maker:
          type: string
          description: >-
            Ethereum address of the maker (In the default case, this is your
            proxy address)
          example: '0x1234567890123456789012345678901234567890'
        signer:
          type: string
          description: Ethereum address of the signer
          example: '0x1234567890123456789012345678901234567890'
        tokenId:
          type: string
          description: Token ID (asset ID) for the order
          example: 0xabc123def456...
        makerAmount:
          type: string
          description: Amount the maker is providing in fixed-math with 6 decimals
          example: '100000000'
        takerAmount:
          type: string
          description: Amount the taker is providing in fixed-math with 6 decimals
          example: '200000000'
        side:
          type: string
          description: Order side
          enum:
            - BUY
            - SELL
          example: BUY
        expiration:
          type: string
          description: >-
            Unix timestamp when the order expires. Present in the API wire body;
            not part of the CLOB V2 EIP-712 signed order struct.
          example: '1735689600'
        timestamp:
          type: string
          description: >-
            Unix timestamp in milliseconds when the order was created (used for
            order uniqueness)
          example: '1735689600000'
        metadata:
          type: string
          description: Reserved for future use
          example: ''
        builder:
          type: string
          description: >-
            Builder code (bytes32) for integrator attribution. `0x` + 64 hex
            chars or empty.
          example: '0x0000000000000000000000000000000000000000000000000000000000000000'
        signature:
          type: string
          description: Cryptographic signature of the order
          example: 0x1234abcd...
        salt:
          type: integer
          description: Random salt for order uniqueness
          example: 1234567890
        signatureType:
          type: integer
          description: Type of signature (0 = EOA, 1 = POLY_PROXY, 2 = POLY_GNOSIS_SAFE)
          enum:
            - 0
            - 1
            - 2
  securitySchemes:
    polyApiKey:
      type: apiKey
      in: header
      name: POLY_API_KEY
      description: Your API key
    polyAddress:
      type: apiKey
      in: header
      name: POLY_ADDRESS
      description: Ethereum address associated with the API key
    polySignature:
      type: apiKey
      in: header
      name: POLY_SIGNATURE
      description: HMAC signature of the request
    polyPassphrase:
      type: apiKey
      in: header
      name: POLY_PASSPHRASE
      description: API key passphrase
    polyTimestamp:
      type: apiKey
      in: header
      name: POLY_TIMESTAMP
      description: Unix timestamp of the request

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Cancel single order

> Cancels a single order by its ID. Works even in cancel-only mode.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml delete /order
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /order:
    delete:
      tags:
        - Trade
      summary: Cancel single order
      description: |
        Cancels a single order by its ID. Works even in cancel-only mode.
      operationId: cancelOrder
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CancelOrderPayload'
            example:
              orderID: '0xabcdef1234567890abcdef1234567890abcdef12'
      responses:
        '200':
          description: Order cancellation result
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CancelOrdersResponse'
              examples:
                canceled:
                  summary: Order successfully canceled
                  value:
                    canceled:
                      - '0xabcdef1234567890abcdef1234567890abcdef12'
                    not_canceled: {}
                not_canceled:
                  summary: Order could not be canceled
                  value:
                    canceled: []
                    not_canceled:
                      '0xabcdef1234567890abcdef1234567890abcdef12': Order not found or already canceled
        '400':
          description: Bad request - Invalid order ID or payload
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                invalid_order_id:
                  summary: Invalid order ID
                  value:
                    error: Invalid orderID
                invalid_payload:
                  summary: Invalid payload
                  value:
                    error: Invalid order payload
        '401':
          description: Unauthorized - Invalid API key or authentication failed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid API key
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
        '503':
          description: >-
            Service unavailable - Trading disabled (cancels still work in
            cancel-only mode)
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: >-
                  Trading is currently disabled. Check polymarket.com for
                  updates
      security:
        - polyApiKey: []
          polyAddress: []
          polySignature: []
          polyPassphrase: []
          polyTimestamp: []
components:
  schemas:
    CancelOrderPayload:
      type: object
      required:
        - orderID
      properties:
        orderID:
          type: string
          description: Order ID (order hash) to cancel
          example: '0xabcdef1234567890abcdef1234567890abcdef12'
    CancelOrdersResponse:
      type: object
      required:
        - canceled
        - not_canceled
      properties:
        canceled:
          type: array
          description: Array of order IDs that were successfully canceled
          items:
            type: string
          example:
            - '0xabcdef1234567890abcdef1234567890abcdef12'
        not_canceled:
          type: object
          description: Map of order IDs that could not be canceled with error messages
          additionalProperties:
            type: string
          example:
            '0xabcdef1234567890abcdef1234567890abcdef12': Order not found or already canceled
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
  securitySchemes:
    polyApiKey:
      type: apiKey
      in: header
      name: POLY_API_KEY
      description: Your API key
    polyAddress:
      type: apiKey
      in: header
      name: POLY_ADDRESS
      description: Ethereum address associated with the API key
    polySignature:
      type: apiKey
      in: header
      name: POLY_SIGNATURE
      description: HMAC signature of the request
    polyPassphrase:
      type: apiKey
      in: header
      name: POLY_PASSPHRASE
      description: API key passphrase
    polyTimestamp:
      type: apiKey
      in: header
      name: POLY_TIMESTAMP
      description: Unix timestamp of the request

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get single order by ID

> Retrieves a specific order by its ID (order hash) for the authenticated user.
Builder-authenticated clients can also use this endpoint to retrieve orders attributed to their builder account.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /data/order/{orderID}
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /data/order/{orderID}:
    get:
      tags:
        - Trade
      summary: Get single order by ID
      description: >
        Retrieves a specific order by its ID (order hash) for the authenticated
        user.

        Builder-authenticated clients can also use this endpoint to retrieve
        orders attributed to their builder account.
      operationId: getOrder
      parameters:
        - name: orderID
          in: path
          description: Order ID (order hash)
          required: true
          schema:
            type: string
          example: '0x0000000000000000000000000000000000000000000000000000000000000001'
      responses:
        '200':
          description: Successfully retrieved order
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/OpenOrder'
              example:
                id: '0xabcdef1234567890abcdef1234567890abcdef12'
                status: ORDER_STATUS_LIVE
                owner: f4f247b7-4ac7-ff29-a152-04fda0a8755a
                maker_address: '0x1234567890123456789012345678901234567890'
                market: >-
                  0x0000000000000000000000000000000000000000000000000000000000000001
                asset_id: 0xabc123def456...
                side: BUY
                original_size: '100000000'
                size_matched: '0'
                price: '0.5'
                outcome: 'YES'
                expiration: '1735689600'
                order_type: GTC
                associate_trades: []
                created_at: 1700000000
        '400':
          description: Bad request - Invalid order ID
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid orderID
        '401':
          description: Unauthorized - Invalid API key or authentication failed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid API key
        '404':
          description: Order not found
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Order not found
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security:
        - polyApiKey: []
          polyAddress: []
          polySignature: []
          polyPassphrase: []
          polyTimestamp: []
components:
  schemas:
    OpenOrder:
      type: object
      required:
        - id
        - status
        - owner
        - maker_address
        - market
        - asset_id
        - side
        - original_size
        - size_matched
        - price
        - expiration
        - order_type
        - created_at
        - outcome
      properties:
        id:
          type: string
          description: Order ID (order hash)
          example: '0xabcdef1234567890abcdef1234567890abcdef12'
        status:
          type: string
          description: Order status
          enum:
            - ORDER_STATUS_LIVE
            - ORDER_STATUS_INVALID
            - ORDER_STATUS_CANCELED_MARKET_RESOLVED
            - ORDER_STATUS_CANCELED
            - ORDER_STATUS_MATCHED
        owner:
          type: string
          description: UUID of the order owner
          example: f4f247b7-4ac7-ff29-a152-04fda0a8755a
        maker_address:
          type: string
          description: Ethereum address of the maker
          example: '0x1234567890123456789012345678901234567890'
        market:
          type: string
          description: Market (condition ID)
          example: '0x0000000000000000000000000000000000000000000000000000000000000001'
        asset_id:
          type: string
          description: Asset ID (token ID)
          example: 0xabc123def456...
        side:
          type: string
          description: Order side
          enum:
            - BUY
            - SELL
          example: BUY
        original_size:
          type: string
          description: Original order size in fixed-math with 6 decimals
          example: '100000000'
        size_matched:
          type: string
          description: Size that has been matched in fixed-math with 6 decimals
          example: '0'
        price:
          type: string
          description: Order price
          example: '0.5'
        outcome:
          type: string
          description: Market outcome (YES/NO)
          example: 'YES'
        expiration:
          type: string
          description: Unix timestamp when the order expires
          example: '1735689600'
        order_type:
          type: string
          description: Order type
          enum:
            - GTC
            - FOK
            - GTD
            - FAK
          example: GTC
        associate_trades:
          type: array
          description: Array of associated trade IDs
          items:
            type: string
          example:
            - trade-123
        created_at:
          type: integer
          description: Unix timestamp when the order was created
          example: 1700000000
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
  securitySchemes:
    polyApiKey:
      type: apiKey
      in: header
      name: POLY_API_KEY
      description: Your API key
    polyAddress:
      type: apiKey
      in: header
      name: POLY_ADDRESS
      description: Ethereum address associated with the API key
    polySignature:
      type: apiKey
      in: header
      name: POLY_SIGNATURE
      description: HMAC signature of the request
    polyPassphrase:
      type: apiKey
      in: header
      name: POLY_PASSPHRASE
      description: API key passphrase
    polyTimestamp:
      type: apiKey
      in: header
      name: POLY_TIMESTAMP
      description: Unix timestamp of the request

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Post multiple orders

> Creates multiple new orders in the order book. Orders are processed in parallel.
Maximum 15 orders per request.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml post /orders
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /orders:
    post:
      tags:
        - Trade
      summary: Post multiple orders
      description: >
        Creates multiple new orders in the order book. Orders are processed in
        parallel.

        Maximum 15 orders per request.
      operationId: postOrders
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: array
              items:
                $ref: '#/components/schemas/SendOrder'
              maxItems: 15
            examples:
              example:
                summary: Send multiple orders example
                value:
                  - order:
                      maker: '0x1234567890123456789012345678901234567890'
                      signer: '0x1234567890123456789012345678901234567890'
                      tokenId: 0xabc123def456...
                      makerAmount: '100000000'
                      takerAmount: '200000000'
                      side: BUY
                      expiration: '1735689600'
                      timestamp: '1735689600000'
                      metadata: ''
                      builder: >-
                        0x0000000000000000000000000000000000000000000000000000000000000000
                      signature: 0x1234abcd...
                      salt: 1234567890
                      signatureType: 0
                    owner: f4f247b7-4ac7-ff29-a152-04fda0a8755a
                    orderType: GTC
                    deferExec: false
                    postOnly: false
                  - order:
                      maker: '0x1234567890123456789012345678901234567890'
                      signer: '0x1234567890123456789012345678901234567890'
                      tokenId: 0xdef456abc789...
                      makerAmount: '200000000'
                      takerAmount: '100000000'
                      side: SELL
                      expiration: '1735689600'
                      timestamp: '1735689600000'
                      metadata: ''
                      builder: >-
                        0x0000000000000000000000000000000000000000000000000000000000000000
                      signature: 0x5678efgh...
                      salt: 1234567891
                      signatureType: 0
                    owner: f4f247b7-4ac7-ff29-a152-04fda0a8755a
                    orderType: GTC
                    deferExec: false
                    postOnly: false
      responses:
        '200':
          description: >-
            Orders successfully processed. Returns an array of order responses,
            one for each order in the request.
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/SendOrderResponse'
              examples:
                mixed_results:
                  summary: Mixed order results
                  value:
                    - success: true
                      orderID: '0xabcdef1234567890abcdef1234567890abcdef12'
                      status: live
                      makingAmount: '100000000'
                      takingAmount: '200000000'
                      errorMsg: ''
                    - success: true
                      orderID: '0xfedcba0987654321fedcba0987654321fedcba09'
                      status: matched
                      makingAmount: '200000000'
                      takingAmount: '100000000'
                      transactionsHashes:
                        - '0x1234567890abcdef1234567890abcdef12345678'
                      tradeIDs:
                        - trade-123
                      errorMsg: ''
                    - success: false
                      orderID: ''
                      status: delayed
                      errorMsg: 'Rate limit exceeded for tokenId: 0xdef456abc789...'
                post_only_mode:
                  summary: Post-only mode results
                  value:
                    - errorMsg: >-
                        post-only mode: only post-only orders and cancels are
                        allowed
                      orderID: ''
                      takingAmount: ''
                      makingAmount: ''
                      status: ''
                      success: true
                    - errorMsg: >-
                        post-only mode: only post-only orders and cancels are
                        allowed
                      orderID: ''
                      takingAmount: ''
                      makingAmount: ''
                      status: ''
                      success: true
        '400':
          description: Bad request - Invalid order payload or validation error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                invalid_payload:
                  summary: Invalid order payload
                  value:
                    error: Invalid order payload
                empty_payload:
                  summary: Empty orders array
                  value:
                    error: Invalid order payload
                too_many_orders:
                  summary: Too many orders
                  value:
                    error: 'Too many orders in payload: 20, max allowed: 15'
                owner_mismatch:
                  summary: Owner mismatch
                  value:
                    error: the order owner has to be the owner of the API KEY
                signer_mismatch:
                  summary: Signer mismatch
                  value:
                    error: >-
                      the order signer address has to be the address of the API
                      KEY
                banned_address:
                  summary: Banned address
                  value:
                    error: '''0x1234...'' address banned'
                closed_only_mode:
                  summary: Closed only mode violation
                  value:
                    error: '''0x1234...'' address in closed only mode'
        '401':
          description: Unauthorized - Invalid API key or authentication failed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid API key
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: could not insert order
        '503':
          description: Service unavailable - Trading disabled or cancel-only mode
          headers:
            Retry-After:
              description: Seconds to wait before retrying when provided by post-only mode.
              schema:
                type: integer
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                trading_disabled:
                  summary: Trading disabled
                  value:
                    error: >-
                      Trading is currently disabled. Check polymarket.com for
                      updates
                cancel_only:
                  summary: Cancel-only mode
                  value:
                    error: >-
                      Trading is currently cancel-only. New orders are not
                      accepted, but cancels are allowed.
                post_only_mode:
                  summary: Post-only mode
                  value:
                    error: >-
                      post-only mode: only post-only orders and cancels are
                      allowed
                    code: post_only_mode
                    retry_after_seconds: 79
      security:
        - polyApiKey: []
          polyAddress: []
          polySignature: []
          polyPassphrase: []
          polyTimestamp: []
components:
  schemas:
    SendOrder:
      type: object
      required:
        - order
        - owner
      properties:
        order:
          $ref: '#/components/schemas/Order'
        owner:
          type: string
          description: UUID of the API key owner
          example: f4f247b7-4ac7-ff29-a152-04fda0a8755a
        orderType:
          type: string
          description: Time in force
          enum:
            - GTC
            - FOK
            - GTD
            - FAK
          default: GTC
        deferExec:
          type: boolean
          description: Whether to defer execution
          default: false
        postOnly:
          type: boolean
          description: >-
            Whether the order must rest on the book and not match immediately.
            Only supported for GTC and GTD orders.
          default: false
    SendOrderResponse:
      type: object
      required:
        - success
        - orderID
        - status
      properties:
        success:
          type: boolean
          description: Whether the order was successfully processed
          example: true
        orderID:
          type: string
          description: Unique identifier for the order (order hash)
          example: '0xabcdef1234567890abcdef1234567890abcdef12'
        status:
          type: string
          description: Status of the order after processing
          enum:
            - live
            - matched
            - delayed
        makingAmount:
          type: string
          description: Amount the maker is providing in fixed-math with 6 decimals
          example: '100000000'
        takingAmount:
          type: string
          description: Amount the taker is providing in fixed-math with 6 decimals
          example: '200000000'
        transactionsHashes:
          type: array
          description: Array of transaction hashes (present when status is 'matched')
          items:
            type: string
          example:
            - '0x1234567890abcdef1234567890abcdef12345678'
        tradeIDs:
          type: array
          description: Array of trade IDs (present when status is 'matched')
          items:
            type: string
        errorMsg:
          type: string
          description: Error message (empty on success)
          example: ''
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
    Order:
      type: object
      description: >
        Order payload submitted to the CLOB API. In CLOB V2, `expiration`
        remains in

        the POST /order wire body for GTD/order-expiry handling, but it is not
        part

        of the EIP-712 signed order struct.
      required:
        - maker
        - signer
        - tokenId
        - makerAmount
        - takerAmount
        - side
        - expiration
        - timestamp
        - builder
        - signature
        - salt
        - signatureType
      properties:
        maker:
          type: string
          description: >-
            Ethereum address of the maker (In the default case, this is your
            proxy address)
          example: '0x1234567890123456789012345678901234567890'
        signer:
          type: string
          description: Ethereum address of the signer
          example: '0x1234567890123456789012345678901234567890'
        tokenId:
          type: string
          description: Token ID (asset ID) for the order
          example: 0xabc123def456...
        makerAmount:
          type: string
          description: Amount the maker is providing in fixed-math with 6 decimals
          example: '100000000'
        takerAmount:
          type: string
          description: Amount the taker is providing in fixed-math with 6 decimals
          example: '200000000'
        side:
          type: string
          description: Order side
          enum:
            - BUY
            - SELL
          example: BUY
        expiration:
          type: string
          description: >-
            Unix timestamp when the order expires. Present in the API wire body;
            not part of the CLOB V2 EIP-712 signed order struct.
          example: '1735689600'
        timestamp:
          type: string
          description: >-
            Unix timestamp in milliseconds when the order was created (used for
            order uniqueness)
          example: '1735689600000'
        metadata:
          type: string
          description: Reserved for future use
          example: ''
        builder:
          type: string
          description: >-
            Builder code (bytes32) for integrator attribution. `0x` + 64 hex
            chars or empty.
          example: '0x0000000000000000000000000000000000000000000000000000000000000000'
        signature:
          type: string
          description: Cryptographic signature of the order
          example: 0x1234abcd...
        salt:
          type: integer
          description: Random salt for order uniqueness
          example: 1234567890
        signatureType:
          type: integer
          description: Type of signature (0 = EOA, 1 = POLY_PROXY, 2 = POLY_GNOSIS_SAFE)
          enum:
            - 0
            - 1
            - 2
  securitySchemes:
    polyApiKey:
      type: apiKey
      in: header
      name: POLY_API_KEY
      description: Your API key
    polyAddress:
      type: apiKey
      in: header
      name: POLY_ADDRESS
      description: Ethereum address associated with the API key
    polySignature:
      type: apiKey
      in: header
      name: POLY_SIGNATURE
      description: HMAC signature of the request
    polyPassphrase:
      type: apiKey
      in: header
      name: POLY_PASSPHRASE
      description: API key passphrase
    polyTimestamp:
      type: apiKey
      in: header
      name: POLY_TIMESTAMP
      description: Unix timestamp of the request

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get user orders

> Retrieves open orders for the authenticated user. Returns paginated results.
Builder-authenticated clients can also use this endpoint to retrieve orders attributed to their builder account.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /data/orders
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /data/orders:
    get:
      tags:
        - Trade
      summary: Get user orders
      description: >
        Retrieves open orders for the authenticated user. Returns paginated
        results.

        Builder-authenticated clients can also use this endpoint to retrieve
        orders attributed to their builder account.
      operationId: getOrders
      parameters:
        - name: id
          in: query
          description: Order ID (hash) to filter by specific order
          required: false
          schema:
            type: string
          example: '0xabcdef1234567890abcdef1234567890abcdef12'
        - name: market
          in: query
          description: Market (condition ID) to filter orders
          required: false
          schema:
            type: string
          example: '0x0000000000000000000000000000000000000000000000000000000000000001'
        - name: asset_id
          in: query
          description: Asset ID (token ID) to filter orders
          required: false
          schema:
            type: string
          example: 0xabc123def456...
        - name: next_cursor
          in: query
          description: Cursor for pagination (base64 encoded offset)
          required: false
          schema:
            type: string
          example: MA==
      responses:
        '200':
          description: Successfully retrieved orders
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/OrdersResponse'
              examples:
                example:
                  summary: User orders response
                  value:
                    limit: 100
                    next_cursor: MTAw
                    count: 2
                    data:
                      - id: '0xabcdef1234567890abcdef1234567890abcdef12'
                        status: ORDER_STATUS_LIVE
                        owner: f4f247b7-4ac7-ff29-a152-04fda0a8755a
                        maker_address: '0x1234567890123456789012345678901234567890'
                        market: >-
                          0x0000000000000000000000000000000000000000000000000000000000000001
                        asset_id: 0xabc123def456...
                        side: BUY
                        original_size: '100000000'
                        size_matched: '0'
                        price: '0.5'
                        outcome: 'YES'
                        expiration: '1735689600'
                        order_type: GTC
                        associate_trades: []
                        created_at: 1700000000
                      - id: '0xfedcba0987654321fedcba0987654321fedcba09'
                        status: ORDER_STATUS_LIVE
                        owner: f4f247b7-4ac7-ff29-a152-04fda0a8755a
                        maker_address: '0x1234567890123456789012345678901234567890'
                        market: >-
                          0x0000000000000000000000000000000000000000000000000000000000000002
                        asset_id: 0xdef456abc789...
                        side: SELL
                        original_size: '200000000'
                        size_matched: '50000000'
                        price: '0.75'
                        outcome: 'NO'
                        expiration: '1735689600'
                        order_type: GTC
                        associate_trades:
                          - trade-123
                        created_at: 1700000001
        '400':
          description: Bad request - Invalid parameters
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: invalid order params payload
        '401':
          description: Unauthorized - Invalid API key or authentication failed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid API key
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security:
        - polyApiKey: []
          polyAddress: []
          polySignature: []
          polyPassphrase: []
          polyTimestamp: []
components:
  schemas:
    OrdersResponse:
      type: object
      required:
        - limit
        - next_cursor
        - count
        - data
      properties:
        limit:
          type: integer
          description: Maximum number of results per page
          example: 100
        next_cursor:
          type: string
          description: >-
            Cursor for pagination (base64 encoded offset). Empty if no more
            results.
          example: MTAw
        count:
          type: integer
          description: Number of orders in this response
          example: 2
        data:
          type: array
          description: Array of open orders
          items:
            $ref: '#/components/schemas/OpenOrder'
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
    OpenOrder:
      type: object
      required:
        - id
        - status
        - owner
        - maker_address
        - market
        - asset_id
        - side
        - original_size
        - size_matched
        - price
        - expiration
        - order_type
        - created_at
        - outcome
      properties:
        id:
          type: string
          description: Order ID (order hash)
          example: '0xabcdef1234567890abcdef1234567890abcdef12'
        status:
          type: string
          description: Order status
          enum:
            - ORDER_STATUS_LIVE
            - ORDER_STATUS_INVALID
            - ORDER_STATUS_CANCELED_MARKET_RESOLVED
            - ORDER_STATUS_CANCELED
            - ORDER_STATUS_MATCHED
        owner:
          type: string
          description: UUID of the order owner
          example: f4f247b7-4ac7-ff29-a152-04fda0a8755a
        maker_address:
          type: string
          description: Ethereum address of the maker
          example: '0x1234567890123456789012345678901234567890'
        market:
          type: string
          description: Market (condition ID)
          example: '0x0000000000000000000000000000000000000000000000000000000000000001'
        asset_id:
          type: string
          description: Asset ID (token ID)
          example: 0xabc123def456...
        side:
          type: string
          description: Order side
          enum:
            - BUY
            - SELL
          example: BUY
        original_size:
          type: string
          description: Original order size in fixed-math with 6 decimals
          example: '100000000'
        size_matched:
          type: string
          description: Size that has been matched in fixed-math with 6 decimals
          example: '0'
        price:
          type: string
          description: Order price
          example: '0.5'
        outcome:
          type: string
          description: Market outcome (YES/NO)
          example: 'YES'
        expiration:
          type: string
          description: Unix timestamp when the order expires
          example: '1735689600'
        order_type:
          type: string
          description: Order type
          enum:
            - GTC
            - FOK
            - GTD
            - FAK
          example: GTC
        associate_trades:
          type: array
          description: Array of associated trade IDs
          items:
            type: string
          example:
            - trade-123
        created_at:
          type: integer
          description: Unix timestamp when the order was created
          example: 1700000000
  securitySchemes:
    polyApiKey:
      type: apiKey
      in: header
      name: POLY_API_KEY
      description: Your API key
    polyAddress:
      type: apiKey
      in: header
      name: POLY_ADDRESS
      description: Ethereum address associated with the API key
    polySignature:
      type: apiKey
      in: header
      name: POLY_SIGNATURE
      description: HMAC signature of the request
    polyPassphrase:
      type: apiKey
      in: header
      name: POLY_PASSPHRASE
      description: API key passphrase
    polyTimestamp:
      type: apiKey
      in: header
      name: POLY_TIMESTAMP
      description: Unix timestamp of the request

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Cancel multiple orders

> Cancels multiple orders by their IDs. Maximum 3000 orders per request.
Duplicate order IDs in the request are automatically ignored.
Works even in cancel-only mode.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml delete /orders
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /orders:
    delete:
      tags:
        - Trade
      summary: Cancel multiple orders
      description: |
        Cancels multiple orders by their IDs. Maximum 3000 orders per request.
        Duplicate order IDs in the request are automatically ignored.
        Works even in cancel-only mode.
      operationId: cancelOrders
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: array
              items:
                type: string
              maxItems: 3000
            example:
              - '0xabcdef1234567890abcdef1234567890abcdef12'
              - '0xfedcba0987654321fedcba0987654321fedcba09'
              - '0x1234567890abcdef1234567890abcdef12345678'
      responses:
        '200':
          description: Cancellation results for all orders
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CancelOrdersResponse'
              examples:
                all_canceled:
                  summary: All orders canceled
                  value:
                    canceled:
                      - '0xabcdef1234567890abcdef1234567890abcdef12'
                      - '0xfedcba0987654321fedcba0987654321fedcba09'
                      - '0x1234567890abcdef1234567890abcdef12345678'
                    not_canceled: {}
                mixed:
                  summary: Some orders canceled, some not
                  value:
                    canceled:
                      - '0xabcdef1234567890abcdef1234567890abcdef12'
                      - '0xfedcba0987654321fedcba0987654321fedcba09'
                    not_canceled:
                      '0x1234567890abcdef1234567890abcdef12345678': Order already matched
                partial:
                  summary: Partial cancellation
                  value:
                    canceled:
                      - '0xabcdef1234567890abcdef1234567890abcdef12'
                    not_canceled:
                      '0xfedcba0987654321fedcba0987654321fedcba09': Order not found
                      '0x1234567890abcdef1234567890abcdef12345678': Order already canceled
        '400':
          description: Bad request - Invalid order IDs or payload
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                invalid_order_id:
                  summary: Invalid order ID
                  value:
                    error: Invalid orderID
                invalid_payload:
                  summary: Invalid payload
                  value:
                    error: Invalid order payload
                too_many_orders:
                  summary: Too many orders
                  value:
                    error: 'Too many orders in payload, max allowed: 3000'
        '401':
          description: Unauthorized - Invalid API key or authentication failed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid API key
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
        '503':
          description: >-
            Service unavailable - Trading disabled (cancels still work in
            cancel-only mode)
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: >-
                  Trading is currently disabled. Check polymarket.com for
                  updates
      security:
        - polyApiKey: []
          polyAddress: []
          polySignature: []
          polyPassphrase: []
          polyTimestamp: []
components:
  schemas:
    CancelOrdersResponse:
      type: object
      required:
        - canceled
        - not_canceled
      properties:
        canceled:
          type: array
          description: Array of order IDs that were successfully canceled
          items:
            type: string
          example:
            - '0xabcdef1234567890abcdef1234567890abcdef12'
        not_canceled:
          type: object
          description: Map of order IDs that could not be canceled with error messages
          additionalProperties:
            type: string
          example:
            '0xabcdef1234567890abcdef1234567890abcdef12': Order not found or already canceled
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
  securitySchemes:
    polyApiKey:
      type: apiKey
      in: header
      name: POLY_API_KEY
      description: Your API key
    polyAddress:
      type: apiKey
      in: header
      name: POLY_ADDRESS
      description: Ethereum address associated with the API key
    polySignature:
      type: apiKey
      in: header
      name: POLY_SIGNATURE
      description: HMAC signature of the request
    polyPassphrase:
      type: apiKey
      in: header
      name: POLY_PASSPHRASE
      description: API key passphrase
    polyTimestamp:
      type: apiKey
      in: header
      name: POLY_TIMESTAMP
      description: Unix timestamp of the request

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Cancel all orders

> Cancels all open orders for the authenticated user. Works even in cancel-only mode.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml delete /cancel-all
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /cancel-all:
    delete:
      tags:
        - Trade
      summary: Cancel all orders
      description: >
        Cancels all open orders for the authenticated user. Works even in
        cancel-only mode.
      operationId: cancelAllOrders
      responses:
        '200':
          description: Cancellation results for all orders
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CancelOrdersResponse'
              examples:
                canceled:
                  summary: All orders canceled
                  value:
                    canceled:
                      - '0xabcdef1234567890abcdef1234567890abcdef12'
                      - '0xfedcba0987654321fedcba0987654321fedcba09'
                    not_canceled: {}
                mixed:
                  summary: Some orders canceled, some not
                  value:
                    canceled:
                      - '0xabcdef1234567890abcdef1234567890abcdef12'
                    not_canceled:
                      '0xfedcba0987654321fedcba0987654321fedcba09': Order already matched
                no_orders:
                  summary: No orders to cancel
                  value:
                    canceled: []
                    not_canceled: {}
        '401':
          description: Unauthorized - Invalid API key or authentication failed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid API key
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
        '503':
          description: >-
            Service unavailable - Trading disabled (cancels still work in
            cancel-only mode)
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: >-
                  Trading is currently disabled. Check polymarket.com for
                  updates
      security:
        - polyApiKey: []
          polyAddress: []
          polySignature: []
          polyPassphrase: []
          polyTimestamp: []
components:
  schemas:
    CancelOrdersResponse:
      type: object
      required:
        - canceled
        - not_canceled
      properties:
        canceled:
          type: array
          description: Array of order IDs that were successfully canceled
          items:
            type: string
          example:
            - '0xabcdef1234567890abcdef1234567890abcdef12'
        not_canceled:
          type: object
          description: Map of order IDs that could not be canceled with error messages
          additionalProperties:
            type: string
          example:
            '0xabcdef1234567890abcdef1234567890abcdef12': Order not found or already canceled
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
  securitySchemes:
    polyApiKey:
      type: apiKey
      in: header
      name: POLY_API_KEY
      description: Your API key
    polyAddress:
      type: apiKey
      in: header
      name: POLY_ADDRESS
      description: Ethereum address associated with the API key
    polySignature:
      type: apiKey
      in: header
      name: POLY_SIGNATURE
      description: HMAC signature of the request
    polyPassphrase:
      type: apiKey
      in: header
      name: POLY_PASSPHRASE
      description: API key passphrase
    polyTimestamp:
      type: apiKey
      in: header
      name: POLY_TIMESTAMP
      description: Unix timestamp of the request

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Cancel orders for a market

> Cancels all open orders for the authenticated user in a specific market (condition) and asset.
Works even in cancel-only mode.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml delete /cancel-market-orders
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /cancel-market-orders:
    delete:
      tags:
        - Trade
      summary: Cancel orders for a market
      description: >
        Cancels all open orders for the authenticated user in a specific market
        (condition) and asset.

        Works even in cancel-only mode.
      operationId: cancelMarketOrders
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/OrderMarketCancelParams'
            example:
              market: >-
                0x0000000000000000000000000000000000000000000000000000000000000001
              asset_id: 0xabc123def456...
      responses:
        '200':
          description: Cancellation results for market orders
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CancelOrdersResponse'
              examples:
                canceled:
                  summary: All market orders canceled
                  value:
                    canceled:
                      - '0xabcdef1234567890abcdef1234567890abcdef12'
                      - '0xfedcba0987654321fedcba0987654321fedcba09'
                    not_canceled: {}
                mixed:
                  summary: Some orders canceled, some not
                  value:
                    canceled:
                      - '0xabcdef1234567890abcdef1234567890abcdef12'
                    not_canceled:
                      '0xfedcba0987654321fedcba0987654321fedcba09': Order already matched
                no_orders:
                  summary: No orders found for this market
                  value:
                    canceled: []
                    not_canceled: {}
        '400':
          description: Bad request - Invalid payload
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid order payload
        '401':
          description: Unauthorized - Invalid API key or authentication failed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid API key
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
        '503':
          description: >-
            Service unavailable - Trading disabled (cancels still work in
            cancel-only mode)
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: >-
                  Trading is currently disabled. Check polymarket.com for
                  updates
      security:
        - polyApiKey: []
          polyAddress: []
          polySignature: []
          polyPassphrase: []
          polyTimestamp: []
components:
  schemas:
    OrderMarketCancelParams:
      type: object
      required:
        - market
        - asset_id
      properties:
        market:
          type: string
          description: Market (condition ID)
          example: '0x0000000000000000000000000000000000000000000000000000000000000001'
        asset_id:
          type: string
          description: Asset ID (token ID)
          example: 0xabc123def456...
    CancelOrdersResponse:
      type: object
      required:
        - canceled
        - not_canceled
      properties:
        canceled:
          type: array
          description: Array of order IDs that were successfully canceled
          items:
            type: string
          example:
            - '0xabcdef1234567890abcdef1234567890abcdef12'
        not_canceled:
          type: object
          description: Map of order IDs that could not be canceled with error messages
          additionalProperties:
            type: string
          example:
            '0xabcdef1234567890abcdef1234567890abcdef12': Order not found or already canceled
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
  securitySchemes:
    polyApiKey:
      type: apiKey
      in: header
      name: POLY_API_KEY
      description: Your API key
    polyAddress:
      type: apiKey
      in: header
      name: POLY_ADDRESS
      description: Ethereum address associated with the API key
    polySignature:
      type: apiKey
      in: header
      name: POLY_SIGNATURE
      description: HMAC signature of the request
    polyPassphrase:
      type: apiKey
      in: header
      name: POLY_PASSPHRASE
      description: API key passphrase
    polyTimestamp:
      type: apiKey
      in: header
      name: POLY_TIMESTAMP
      description: Unix timestamp of the request

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get order scoring status

> Checks if a specific order is currently scoring for rewards.

An order is considered "scoring" if it meets all the criteria for earning maker rewards:
- The order is live on a rewards-eligible market
- The order meets the minimum size requirements
- The order is within the valid spread range
- The order has been live for the required duration




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /order-scoring
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /order-scoring:
    get:
      tags:
        - Trade
      summary: Get order scoring status
      description: >
        Checks if a specific order is currently scoring for rewards.


        An order is considered "scoring" if it meets all the criteria for
        earning maker rewards:

        - The order is live on a rewards-eligible market

        - The order meets the minimum size requirements

        - The order is within the valid spread range

        - The order has been live for the required duration
      operationId: getOrderScoring
      parameters:
        - name: order_id
          in: query
          description: The order ID (order hash) to check scoring status for
          required: true
          schema:
            type: string
          example: '0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890'
      responses:
        '200':
          description: Successfully retrieved order scoring status
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/OrderScoringResponse'
              examples:
                scoring:
                  summary: Order is scoring
                  value:
                    scoring: true
                not_scoring:
                  summary: Order is not scoring
                  value:
                    scoring: false
        '400':
          description: Bad request - Invalid order ID
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid order_id
        '401':
          description: Unauthorized - Invalid API key or order doesn't belong to user
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid API key
        '404':
          description: Market not found for the order
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: market not found
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
        '503':
          description: Service unavailable - Trading disabled
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: >-
                  Trading is currently disabled. Check polymarket.com for
                  updates
      security:
        - polyApiKey: []
          polyAddress: []
          polySignature: []
          polyPassphrase: []
          polyTimestamp: []
components:
  schemas:
    OrderScoringResponse:
      type: object
      description: Response indicating whether an order is currently scoring for rewards
      required:
        - scoring
      properties:
        scoring:
          type: boolean
          description: Whether the order is currently scoring for maker rewards
          example: true
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
  securitySchemes:
    polyApiKey:
      type: apiKey
      in: header
      name: POLY_API_KEY
      description: Your API key
    polyAddress:
      type: apiKey
      in: header
      name: POLY_ADDRESS
      description: Ethereum address associated with the API key
    polySignature:
      type: apiKey
      in: header
      name: POLY_SIGNATURE
      description: HMAC signature of the request
    polyPassphrase:
      type: apiKey
      in: header
      name: POLY_PASSPHRASE
      description: API key passphrase
    polyTimestamp:
      type: apiKey
      in: header
      name: POLY_TIMESTAMP
      description: Unix timestamp of the request

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Send heartbeat

> Sends a heartbeat signal to maintain active session status.
If heartbeats are not sent regularly, all open orders for the user will be automatically canceled.
This is useful for automated trading systems that need to ensure orders are canceled
if the system becomes unresponsive.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml post /heartbeats
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /heartbeats:
    post:
      tags:
        - Trade
      summary: Send heartbeat
      description: >
        Sends a heartbeat signal to maintain active session status.

        If heartbeats are not sent regularly, all open orders for the user will
        be automatically canceled.

        This is useful for automated trading systems that need to ensure orders
        are canceled

        if the system becomes unresponsive.
      operationId: sendHeartbeat
      responses:
        '200':
          description: Heartbeat acknowledged
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/HeartbeatResponse'
              example:
                status: ok
        '401':
          description: Unauthorized - Invalid API key or authentication failed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid API key
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security:
        - polyApiKey: []
          polyAddress: []
          polySignature: []
          polyPassphrase: []
          polyTimestamp: []
components:
  schemas:
    HeartbeatResponse:
      type: object
      description: Response for heartbeat request
      required:
        - status
      properties:
        status:
          type: string
          description: Status of the heartbeat acknowledgment
          example: ok
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
  securitySchemes:
    polyApiKey:
      type: apiKey
      in: header
      name: POLY_API_KEY
      description: Your API key
    polyAddress:
      type: apiKey
      in: header
      name: POLY_ADDRESS
      description: Ethereum address associated with the API key
    polySignature:
      type: apiKey
      in: header
      name: POLY_SIGNATURE
      description: HMAC signature of the request
    polyPassphrase:
      type: apiKey
      in: header
      name: POLY_PASSPHRASE
      description: API key passphrase
    polyTimestamp:
      type: apiKey
      in: header
      name: POLY_TIMESTAMP
      description: Unix timestamp of the request

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get trades

> Retrieves trades for the authenticated user. Returns paginated results.
Requires readonly or level 2 API key authentication.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /data/trades
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /data/trades:
    get:
      tags:
        - Trade
      summary: Get trades
      description: |
        Retrieves trades for the authenticated user. Returns paginated results.
        Requires readonly or level 2 API key authentication.
      operationId: getTrades
      parameters:
        - name: id
          in: query
          description: Trade ID to filter by specific trade
          required: false
          schema:
            type: string
          example: trade-123
        - name: maker_address
          in: query
          description: Maker address to filter trades
          required: true
          schema:
            type: string
            pattern: ^0x[a-fA-F0-9]{40}$
          example: '0x1234567890123456789012345678901234567890'
        - name: market
          in: query
          description: Market (condition ID) to filter trades
          required: false
          schema:
            type: string
            pattern: ^0x[a-fA-F0-9]{64}$
          example: '0x0000000000000000000000000000000000000000000000000000000000000001'
        - name: asset_id
          in: query
          description: Asset ID (token ID) to filter trades
          required: false
          schema:
            type: string
          example: >-
            15871154585880608648532107628464183779895785213830018178010423617714102767076
        - name: before
          in: query
          description: Filter trades before this Unix timestamp
          required: false
          schema:
            type: string
            pattern: ^\d+$
          example: '1700000000'
        - name: after
          in: query
          description: Filter trades after this Unix timestamp
          required: false
          schema:
            type: string
            pattern: ^\d+$
          example: '1600000000'
        - name: next_cursor
          in: query
          description: Cursor for pagination (base64 encoded offset)
          required: false
          schema:
            type: string
          example: MA==
      responses:
        '200':
          description: Successfully retrieved trades
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/TradesResponse'
              examples:
                example:
                  summary: User trades response
                  value:
                    limit: 100
                    next_cursor: MTAw
                    count: 2
                    data:
                      - id: trade-123
                        taker_order_id: '0xabcdef1234567890abcdef1234567890abcdef12'
                        market: >-
                          0x0000000000000000000000000000000000000000000000000000000000000001
                        asset_id: >-
                          15871154585880608648532107628464183779895785213830018178010423617714102767076
                        side: BUY
                        size: '100000000'
                        fee_rate_bps: '30'
                        price: '0.5'
                        status: TRADE_STATUS_CONFIRMED
                        match_time: '1700000000'
                        last_update: '1700000000'
                        outcome: 'YES'
                        bucket_index: 0
                        owner: f4f247b7-4ac7-ff29-a152-04fda0a8755a
                        maker_address: '0x1234567890123456789012345678901234567890'
                        transaction_hash: >-
                          0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef
                        trader_side: TAKER
                        maker_orders: []
        '400':
          description: Bad request - Invalid parameters
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid trade params payload
        '401':
          description: Unauthorized - Invalid API key or authentication failed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid API key
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security:
        - polyApiKey: []
          polyAddress: []
          polySignature: []
          polyPassphrase: []
          polyTimestamp: []
components:
  schemas:
    TradesResponse:
      type: object
      description: Paginated trades response
      required:
        - limit
        - next_cursor
        - count
        - data
      properties:
        limit:
          type: integer
          description: Maximum number of items per page
          example: 100
        next_cursor:
          type: string
          description: >-
            Cursor for next page (base64 encoded offset). "LTE=" indicates no
            more pages
          example: MTAw
        count:
          type: integer
          description: Number of items in current response
          example: 2
        data:
          type: array
          description: Array of trades
          items:
            $ref: '#/components/schemas/Trade'
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
    Trade:
      type: object
      description: Trade information
      required:
        - id
        - taker_order_id
        - market
        - asset_id
        - side
        - size
        - price
        - status
        - match_time
        - last_update
        - outcome
        - bucket_index
        - owner
        - maker_address
        - trader_side
      properties:
        id:
          type: string
          description: Trade ID
          example: trade-123
        taker_order_id:
          type: string
          description: Taker order ID (hash)
          example: '0xabcdef1234567890abcdef1234567890abcdef12'
        market:
          type: string
          description: Market (condition ID)
          pattern: ^0x[a-fA-F0-9]{64}$
          example: '0x0000000000000000000000000000000000000000000000000000000000000001'
        asset_id:
          type: string
          description: Asset ID (token ID)
          example: >-
            15871154585880608648532107628464183779895785213830018178010423617714102767076
        side:
          type: string
          description: Trade side
          enum:
            - BUY
            - SELL
          example: BUY
        size:
          type: string
          description: Trade size
          example: '100000000'
        fee_rate_bps:
          type: string
          description: Fee rate in basis points
          example: '30'
        price:
          type: string
          description: Trade price
          example: '0.5'
        status:
          type: string
          description: Trade status
          enum:
            - TRADE_STATUS_CONFIRMED
            - TRADE_STATUS_FAILED
            - TRADE_STATUS_RETRYING
            - TRADE_STATUS_MATCHED
            - TRADE_STATUS_MINED
          example: TRADE_STATUS_CONFIRMED
        match_time:
          type: string
          description: Match time (Unix timestamp)
          example: '1700000000'
        match_time_nano:
          type: string
          description: Match time in nanoseconds
          example: '1700000000000000000'
        last_update:
          type: string
          description: Last update time (Unix timestamp)
          example: '1700000000'
        outcome:
          type: string
          description: Market outcome
          example: 'YES'
        bucket_index:
          type: integer
          description: Bucket index
          example: 0
        owner:
          type: string
          description: Owner UUID
          example: f4f247b7-4ac7-ff29-a152-04fda0a8755a
        maker_address:
          type: string
          description: Maker address
          pattern: ^0x[a-fA-F0-9]{40}$
          example: '0x1234567890123456789012345678901234567890'
        transaction_hash:
          type: string
          description: Transaction hash
          pattern: ^0x[a-fA-F0-9]{64}$
          example: '0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef'
        err_msg:
          type:
            - string
            - 'null'
          description: Error message (if any)
          example: null
        maker_orders:
          type: array
          description: Array of maker orders associated with this trade
          items:
            type: object
            properties:
              order_id:
                type: string
                description: Order ID (hash)
              owner:
                type: string
                description: Owner UUID
              maker_address:
                type: string
                description: Maker address
              matched_amount:
                type: string
                description: Matched amount
              price:
                type: string
                description: Price
              fee_rate_bps:
                type: string
                description: Fee rate in basis points
              asset_id:
                type: string
                description: Asset ID
              outcome:
                type: string
                description: Outcome
              side:
                type: string
                enum:
                  - BUY
                  - SELL
          example: []
        trader_side:
          type: string
          description: Trader side (TAKER or MAKER)
          enum:
            - TAKER
            - MAKER
          example: TAKER
  securitySchemes:
    polyApiKey:
      type: apiKey
      in: header
      name: POLY_API_KEY
      description: Your API key
    polyAddress:
      type: apiKey
      in: header
      name: POLY_ADDRESS
      description: Ethereum address associated with the API key
    polySignature:
      type: apiKey
      in: header
      name: POLY_SIGNATURE
      description: HMAC signature of the request
    polyPassphrase:
      type: apiKey
      in: header
      name: POLY_PASSPHRASE
      description: API key passphrase
    polyTimestamp:
      type: apiKey
      in: header
      name: POLY_TIMESTAMP
      description: Unix timestamp of the request

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get builder trades

> Retrieves trades attributed to a builder code.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /builder/trades
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /builder/trades:
    get:
      tags:
        - Trade
      summary: Get builder trades
      description: |
        Retrieves trades attributed to a builder code.
      operationId: getBuilderTrades
      parameters:
        - name: builder_code
          in: query
          description: Builder code to fetch attributed trades for
          required: true
          schema:
            type: string
            pattern: ^0x[a-fA-F0-9]{64}$
          example: '0x0000000000000000000000000000000000000000000000000000000000000001'
        - name: id
          in: query
          description: Trade ID to filter by specific trade
          required: false
          schema:
            type: string
          example: trade-123
        - name: market
          in: query
          description: Market (condition ID) to filter trades
          required: false
          schema:
            type: string
            pattern: ^0x[a-fA-F0-9]{64}$
          example: '0x0000000000000000000000000000000000000000000000000000000000000001'
        - name: asset_id
          in: query
          description: Asset ID (token ID) to filter trades
          required: false
          schema:
            type: string
          example: >-
            15871154585880608648532107628464183779895785213830018178010423617714102767076
        - name: before
          in: query
          description: Filter trades before this Unix timestamp
          required: false
          schema:
            type: string
            pattern: ^\d+$
          example: '1700000000'
        - name: after
          in: query
          description: Filter trades after this Unix timestamp
          required: false
          schema:
            type: string
            pattern: ^\d+$
          example: '1600000000'
        - name: next_cursor
          in: query
          description: Cursor for pagination (base64 encoded offset)
          required: false
          schema:
            type: string
          example: MA==
      responses:
        '200':
          description: Successfully retrieved builder trades
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/BuilderTradesResponse'
              examples:
                example:
                  summary: Builder trades response
                  value:
                    limit: 300
                    next_cursor: MzAw
                    count: 2
                    data:
                      - id: trade-123
                        tradeType: TAKER
                        takerOrderHash: '0xabcdef1234567890abcdef1234567890abcdef12'
                        builder: >-
                          0x0000000000000000000000000000000000000000000000000000000000000001
                        market: >-
                          0x0000000000000000000000000000000000000000000000000000000000000001
                        assetId: >-
                          15871154585880608648532107628464183779895785213830018178010423617714102767076
                        side: BUY
                        size: '100000000'
                        sizeUsdc: '50000000'
                        price: '0.5'
                        status: TRADE_STATUS_CONFIRMED
                        outcome: 'YES'
                        outcomeIndex: 0
                        owner: f4f247b7-4ac7-ff29-a152-04fda0a8755a
                        maker: '0x1234567890123456789012345678901234567890'
                        transactionHash: >-
                          0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef
                        matchTime: '1700000000'
                        bucketIndex: 0
                        fee: '300000'
                        feeUsdc: '150000'
                        createdAt: '2024-01-01T00:00:00Z'
                        updatedAt: '2024-01-01T00:00:00Z'
        '400':
          description: Bad request - Invalid parameters
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: invalid builder trade params
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: could not fetch builder trades
components:
  schemas:
    BuilderTradesResponse:
      type: object
      description: Paginated builder trades response
      required:
        - limit
        - next_cursor
        - count
        - data
      properties:
        limit:
          type: integer
          description: Maximum number of items per page
          example: 300
        next_cursor:
          type: string
          description: >-
            Cursor for next page (base64 encoded offset). "LTE=" indicates no
            more pages
          example: MzAw
        count:
          type: integer
          description: Number of items in current response
          example: 2
        data:
          type: array
          description: Array of builder trades
          items:
            $ref: '#/components/schemas/BuilderTrade'
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
    BuilderTrade:
      type: object
      description: Builder trade information
      required:
        - id
        - tradeType
        - takerOrderHash
        - builder
        - market
        - assetId
        - side
        - size
        - sizeUsdc
        - price
        - status
        - outcome
        - outcomeIndex
        - owner
        - maker
        - transactionHash
        - matchTime
        - bucketIndex
        - fee
        - feeUsdc
      properties:
        id:
          type: string
          description: Trade ID
          example: trade-123
        tradeType:
          type: string
          description: Trade type
          example: TAKER
        takerOrderHash:
          type: string
          description: Taker order hash
          example: '0xabcdef1234567890abcdef1234567890abcdef12'
        builder:
          type: string
          description: Builder code attributed to the trade
          pattern: ^0x[a-fA-F0-9]{64}$
          example: '0x0000000000000000000000000000000000000000000000000000000000000001'
        market:
          type: string
          description: Market (condition ID)
          pattern: ^0x[a-fA-F0-9]{64}$
          example: '0x0000000000000000000000000000000000000000000000000000000000000001'
        assetId:
          type: string
          description: Asset ID (token ID)
          example: >-
            15871154585880608648532107628464183779895785213830018178010423617714102767076
        side:
          type: string
          description: Trade side
          enum:
            - BUY
            - SELL
          example: BUY
        size:
          type: string
          description: Trade size
          example: '100000000'
        sizeUsdc:
          type: string
          description: Trade size in USDC
          example: '50000000'
        price:
          type: string
          description: Trade price
          example: '0.5'
        status:
          type: string
          description: Trade status
          example: TRADE_STATUS_CONFIRMED
        outcome:
          type: string
          description: Market outcome
          example: 'YES'
        outcomeIndex:
          type: integer
          description: Outcome index
          example: 0
        owner:
          type: string
          description: Owner UUID
          example: f4f247b7-4ac7-ff29-a152-04fda0a8755a
        maker:
          type: string
          description: Maker address
          pattern: ^0x[a-fA-F0-9]{40}$
          example: '0x1234567890123456789012345678901234567890'
        transactionHash:
          type: string
          description: Transaction hash
          pattern: ^0x[a-fA-F0-9]{64}$
          example: '0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef'
        matchTime:
          type: string
          description: Match time (Unix timestamp)
          example: '1700000000'
        bucketIndex:
          type: integer
          description: Bucket index
          example: 0
        fee:
          type: string
          description: Fee amount
          example: '300000'
        feeUsdc:
          type: string
          description: Fee amount in USDC
          example: '150000'
        err_msg:
          type:
            - string
            - 'null'
          description: Error message (if any)
          example: null
        createdAt:
          type: string
          format: date-time
          description: Creation timestamp
          example: '2024-01-01T00:00:00Z'
        updatedAt:
          type: string
          format: date-time
          description: Last update timestamp
          example: '2024-01-01T00:00:00Z'

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get simplified markets



## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /simplified-markets
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /simplified-markets:
    get:
      tags:
        - Markets
      summary: Get simplified markets
      operationId: getSimplifiedMarkets
      parameters:
        - name: next_cursor
          in: query
          required: false
          schema:
            type: string
      responses:
        '200':
          description: Successful response
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/PaginatedSimplifiedMarkets'
        '400':
          description: Invalid request
        '500':
          description: Internal server error
      security: []
components:
  schemas:
    PaginatedSimplifiedMarkets:
      type: object
      properties:
        limit:
          type: integer
        next_cursor:
          type: string
        count:
          type: integer
        data:
          type: array
          items:
            $ref: '#/components/schemas/SimplifiedMarket'
    SimplifiedMarket:
      type: object
      properties:
        condition_id:
          type: string
        rewards:
          $ref: '#/components/schemas/Rewards'
        tokens:
          type: array
          items:
            $ref: '#/components/schemas/Token'
        active:
          type: boolean
        closed:
          type: boolean
        archived:
          type: boolean
        accepting_orders:
          type: boolean
    Rewards:
      type: object
      properties:
        rates:
          type: array
          items:
            type: object
            properties:
              asset_address:
                type: string
              rewards_daily_rate:
                type: number
                format: double
        min_size:
          type: number
          format: double
        max_spread:
          type: number
          format: double
    Token:
      type: object
      properties:
        token_id:
          type: string
        outcome:
          type: string
        price:
          type: number
          format: double
        winner:
          type: boolean

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get sampling markets



## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /sampling-markets
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /sampling-markets:
    get:
      tags:
        - Markets
      summary: Get sampling markets
      operationId: getSamplingMarkets
      parameters:
        - name: next_cursor
          in: query
          required: false
          schema:
            type: string
      responses:
        '200':
          description: Successful response
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/PaginatedMarkets'
        '400':
          description: Invalid request
        '500':
          description: Internal server error
      security: []
components:
  schemas:
    PaginatedMarkets:
      type: object
      properties:
        limit:
          type: integer
        next_cursor:
          type: string
        count:
          type: integer
        data:
          type: array
          items:
            $ref: '#/components/schemas/Market'
    Market:
      type: object
      properties:
        enable_order_book:
          type: boolean
        active:
          type: boolean
        closed:
          type: boolean
        archived:
          type: boolean
        accepting_orders:
          type: boolean
        accepting_order_timestamp:
          type: string
          format: date-time
        minimum_order_size:
          type: number
          format: double
        minimum_tick_size:
          type: number
          format: double
        condition_id:
          type: string
        question_id:
          type: string
        question:
          type: string
        description:
          type: string
        market_slug:
          type: string
        end_date_iso:
          type: string
          format: date-time
        game_start_time:
          type: string
          format: date-time
        seconds_delay:
          type: integer
        fpmm:
          type: string
        maker_base_fee:
          type: integer
          format: int64
        taker_base_fee:
          type: integer
          format: int64
        notifications_enabled:
          type: boolean
        neg_risk:
          type: boolean
        neg_risk_market_id:
          type: string
        neg_risk_request_id:
          type: string
        icon:
          type: string
        image:
          type: string
        rewards:
          $ref: '#/components/schemas/Rewards'
        is_50_50_outcome:
          type: boolean
        tokens:
          type: array
          items:
            $ref: '#/components/schemas/Token'
        tags:
          type: array
          items:
            type: string
    Rewards:
      type: object
      properties:
        rates:
          type: array
          items:
            type: object
            properties:
              asset_address:
                type: string
              rewards_daily_rate:
                type: number
                format: double
        min_size:
          type: number
          format: double
        max_spread:
          type: number
          format: double
    Token:
      type: object
      properties:
        token_id:
          type: string
        outcome:
          type: string
        price:
          type: number
          format: double
        winner:
          type: boolean

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get sampling simplified markets



## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /sampling-simplified-markets
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /sampling-simplified-markets:
    get:
      tags:
        - Markets
      summary: Get sampling simplified markets
      operationId: getSamplingSimplifiedMarkets
      parameters:
        - name: next_cursor
          in: query
          required: false
          schema:
            type: string
      responses:
        '200':
          description: Successful response
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/PaginatedSimplifiedMarkets'
        '400':
          description: Invalid request
        '500':
          description: Internal server error
      security: []
components:
  schemas:
    PaginatedSimplifiedMarkets:
      type: object
      properties:
        limit:
          type: integer
        next_cursor:
          type: string
        count:
          type: integer
        data:
          type: array
          items:
            $ref: '#/components/schemas/SimplifiedMarket'
    SimplifiedMarket:
      type: object
      properties:
        condition_id:
          type: string
        rewards:
          $ref: '#/components/schemas/Rewards'
        tokens:
          type: array
          items:
            $ref: '#/components/schemas/Token'
        active:
          type: boolean
        closed:
          type: boolean
        archived:
          type: boolean
        accepting_orders:
          type: boolean
    Rewards:
      type: object
      properties:
        rates:
          type: array
          items:
            type: object
            properties:
              asset_address:
                type: string
              rewards_daily_rate:
                type: number
                format: double
        min_size:
          type: number
          format: double
        max_spread:
          type: number
          format: double
    Token:
      type: object
      properties:
        token_id:
          type: string
        outcome:
          type: string
        price:
          type: number
          format: double
        winner:
          type: boolean

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get current rebated fees for a maker

> Returns the current rebated fees for a maker address on a given date.

Each entry includes the condition ID, asset address, and the USDC amount rebated.

This endpoint does not require authentication.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /rebates/current
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /rebates/current:
    get:
      tags:
        - Rebates
      summary: Get current rebated fees for a maker
      description: >
        Returns the current rebated fees for a maker address on a given date.


        Each entry includes the condition ID, asset address, and the USDC amount
        rebated.


        This endpoint does not require authentication.
      operationId: getCurrentRebatedFees
      parameters:
        - name: date
          in: query
          description: Date in YYYY-MM-DD format
          required: true
          schema:
            type: string
            format: date
          example: '2026-02-27'
        - name: maker_address
          in: query
          description: Ethereum address of the maker
          required: true
          schema:
            type: string
          example: '0xFeA4cB3dD4ca7CefD3368653B7D6FF9BcDFca604'
      responses:
        '200':
          description: Successfully retrieved rebated fees
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/RebatedFees'
              example:
                - date: '2026-02-27'
                  condition_id: >-
                    0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af
                  asset_address: '0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB'
                  maker_address: '0xFeA4cB3dD4ca7CefD3368653B7D6FF9BcDFca604'
                  rebated_fees_usdc: '0.237519'
        '400':
          description: Bad request - Invalid parameters
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                invalid_date:
                  summary: Invalid date
                  value:
                    error: Invalid date
                invalid_maker_address:
                  summary: Invalid maker address
                  value:
                    error: Invalid maker_address
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
components:
  schemas:
    RebatedFees:
      type: object
      description: Rebated fees for a maker on a specific market and date
      required:
        - date
        - condition_id
        - asset_address
        - maker_address
        - rebated_fees_usdc
      properties:
        date:
          type: string
          description: Date of the rebate (YYYY-MM-DD)
          example: '2026-02-27'
        condition_id:
          type: string
          description: Condition ID of the market
          example: '0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af'
        asset_address:
          type: string
          description: Asset address (e.g. USDC contract)
          example: '0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB'
        maker_address:
          type: string
          description: Maker's Ethereum address
          example: '0xFeA4cB3dD4ca7CefD3368653B7D6FF9BcDFca604'
        rebated_fees_usdc:
          type: string
          description: Rebated fee amount in USDC
          example: '0.237519'
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get current active rewards configurations

> Returns all current active rewards configurations grouped by market.

When `sponsored=true`, returns sponsored reward configurations instead.

Results are paginated (500 items per page). Use next_cursor to fetch subsequent pages.
A next_cursor value of "LTE=" indicates the last page.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /rewards/markets/current
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /rewards/markets/current:
    get:
      tags:
        - Rewards
      summary: Get current active rewards configurations
      description: >
        Returns all current active rewards configurations grouped by market.


        When `sponsored=true`, returns sponsored reward configurations instead.


        Results are paginated (500 items per page). Use next_cursor to fetch
        subsequent pages.

        A next_cursor value of "LTE=" indicates the last page.
      operationId: getCurrentRewards
      parameters:
        - name: sponsored
          in: query
          description: >-
            If true, returns sponsored reward configurations instead of standard
            ones
          required: false
          schema:
            type: boolean
            default: false
        - name: next_cursor
          in: query
          description: Pagination cursor from previous response
          required: false
          schema:
            type: string
      responses:
        '200':
          description: Successfully retrieved current rewards configurations
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/PaginatedCurrentReward'
              example:
                limit: 500
                count: 1
                next_cursor: LTE=
                data:
                  - condition_id: >-
                      0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af
                    rewards_max_spread: 99
                    rewards_min_size: 10
                    rewards_config:
                      - id: 0
                        asset_address: '0x9c4E1703476E875070EE25b56A58B008CFb8FA78'
                        start_date: '2024-03-01'
                        end_date: '2500-12-31'
                        rate_per_day: 2
                        total_rewards: 92
                      - id: 0
                        asset_address: '0x69308FB512518e39F9b16112fA8d994F4e2Bf8bB'
                        start_date: '2024-03-01'
                        end_date: '2500-12-31'
                        rate_per_day: 1
                        total_rewards: 46
                    sponsored_daily_rate: 0.5
                    sponsors_count: 2
                    native_daily_rate: 2.5
                    total_daily_rate: 3
        '400':
          description: Bad request - Invalid next_cursor
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid next_cursor
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
components:
  schemas:
    PaginatedCurrentReward:
      type: object
      description: Paginated list of current reward configurations
      properties:
        limit:
          type: integer
          description: Maximum number of items per page
        count:
          type: integer
          description: Number of items in the current response
        next_cursor:
          type: string
          description: Cursor for the next page. "LTE=" indicates the last page.
        data:
          type: array
          items:
            $ref: '#/components/schemas/CurrentReward'
      required:
        - limit
        - count
        - next_cursor
        - data
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
    CurrentReward:
      type: object
      description: Current active reward configuration for a market
      properties:
        condition_id:
          type: string
          description: Condition ID of the market
        rewards_max_spread:
          type: number
          description: Maximum spread for rewards eligibility
        rewards_min_size:
          type: number
          description: Minimum order size for rewards eligibility
        rewards_config:
          type: array
          items:
            $ref: '#/components/schemas/CurrentRewardConfig'
        sponsored_daily_rate:
          type: number
          format: double
          description: Sponsored daily rate (omitted when zero)
        sponsors_count:
          type: integer
          description: Number of sponsors (omitted when zero)
        native_daily_rate:
          type: number
          format: double
          description: Computed native daily rate excluding sponsors (omitted when zero)
        total_daily_rate:
          type: number
          format: double
          description: Computed total daily rate including sponsors (omitted when zero)
      required:
        - condition_id
    CurrentRewardConfig:
      type: object
      description: Reward configuration entry for a current rewards market
      properties:
        id:
          type: integer
          description: Rewards config ID (always 0 on /rewards/markets/current)
        asset_address:
          type: string
          description: Address of the reward asset
        start_date:
          type: string
          format: date
          description: Start date of the rewards period
        end_date:
          type: string
          format: date
          description: End date of the rewards period
        rate_per_day:
          type: number
          format: double
          description: Daily reward rate
        total_rewards:
          type: number
          format: double
          description: Total rewards amount
      required:
        - asset_address
        - start_date
        - rate_per_day

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get raw rewards for a specific market

> Returns an array of present and future rewards configured on a market.

When `sponsored=true`, sponsored daily rates are folded into each config's
`rate_per_day` .

Results are paginated (100 items per page). Use next_cursor to fetch subsequent pages.
A next_cursor value of "LTE=" indicates the last page.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /rewards/markets/{condition_id}
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /rewards/markets/{condition_id}:
    get:
      tags:
        - Rewards
      summary: Get raw rewards for a specific market
      description: >
        Returns an array of present and future rewards configured on a market.


        When `sponsored=true`, sponsored daily rates are folded into each
        config's

        `rate_per_day` .


        Results are paginated (100 items per page). Use next_cursor to fetch
        subsequent pages.

        A next_cursor value of "LTE=" indicates the last page.
      operationId: getRawRewardsForMarket
      parameters:
        - name: condition_id
          in: path
          required: true
          description: The condition ID of the market
          schema:
            type: string
          example: '0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af'
        - name: sponsored
          in: query
          description: If true, folds sponsored daily rates into each config's rate_per_day
          required: false
          schema:
            type: boolean
            default: false
        - name: next_cursor
          in: query
          description: Pagination cursor from previous response
          required: false
          schema:
            type: string
      responses:
        '200':
          description: Successfully retrieved rewards for market
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/PaginatedMarketReward'
              example:
                limit: 100
                count: 1
                next_cursor: LTE=
                data:
                  - condition_id: >-
                      0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af
                    question: Will Trump win the 2024 Iowa Caucus?
                    market_slug: will-trump-win-the-2024-iowa-caucus
                    event_slug: will-trump-win-the-2024-iowa-caucus
                    image: >-
                      https://polymarket-upload.s3.us-east-2.amazonaws.com/trump1+copy.png
                    rewards_max_spread: 99
                    rewards_min_size: 10
                    market_competitiveness: 0.42
                    tokens:
                      - token_id: >-
                          1343197538147866997676250008839231694243646439454152539053893078719042421992
                        outcome: 'YES'
                        price: 0.8
                      - token_id: >-
                          16678291189211314787145083999015737376658799626183230671758641503291735614088
                        outcome: 'NO'
                        price: 0.2
                    rewards_config:
                      - id: 1
                        asset_address: '0x9c4E1703476E875070EE25b56A58B008CFb8FA78'
                        start_date: '2024-03-01'
                        end_date: '2500-12-31'
                        rate_per_day: 0.25
                        total_rewards: 0
                        total_days: 174161
                      - id: 2
                        asset_address: '0x9c4E1703476E875070EE25b56A58B008CFb8FA78'
                        start_date: '2024-03-01'
                        end_date: '2024-05-31'
                        rate_per_day: 1
                        total_rewards: 92
                        total_days: 92
        '400':
          description: Bad request - Invalid market or next_cursor
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                invalid_market:
                  summary: Empty condition ID
                  value:
                    error: Invalid market
                invalid_cursor:
                  summary: Invalid pagination cursor
                  value:
                    error: Invalid next_cursor
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
components:
  schemas:
    PaginatedMarketReward:
      type: object
      description: Paginated list of market reward configurations
      properties:
        limit:
          type: integer
          description: Maximum number of items per page
        count:
          type: integer
          description: Number of items in the current response
        next_cursor:
          type: string
          description: Cursor for the next page. "LTE=" indicates the last page.
        data:
          type: array
          items:
            $ref: '#/components/schemas/MarketReward'
      required:
        - limit
        - count
        - next_cursor
        - data
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
    MarketReward:
      type: object
      description: Market with raw reward configurations
      properties:
        condition_id:
          type: string
          description: Condition ID of the market
        question:
          type: string
          description: Market question
        market_slug:
          type: string
          description: URL slug for the market
        event_slug:
          type: string
          description: URL slug for the event
        image:
          type: string
          description: URL to market image
        rewards_max_spread:
          type: number
          description: Maximum spread for rewards eligibility
        rewards_min_size:
          type: number
          description: Minimum order size for rewards eligibility
        market_competitiveness:
          type: number
          format: double
          description: Competitiveness score of the market
        tokens:
          type: array
          items:
            $ref: '#/components/schemas/RewardsToken'
        rewards_config:
          type: array
          items:
            $ref: '#/components/schemas/RewardsConfig'
      required:
        - condition_id
        - question
        - tokens
    RewardsToken:
      type: object
      description: Token information for rewards markets
      properties:
        token_id:
          type: string
          description: Token ID
        outcome:
          type: string
          description: Outcome name (e.g., "YES", "NO")
        price:
          type: number
          format: double
          description: Current price of the token
      required:
        - token_id
        - outcome
    RewardsConfig:
      type: object
      description: Rewards configuration for a market
      properties:
        id:
          type: integer
          description: Rewards config ID
        asset_address:
          type: string
          description: Address of the reward asset
        start_date:
          type: string
          format: date
          description: Start date of the rewards period
        end_date:
          type: string
          format: date
          description: End date of the rewards period
        rate_per_day:
          type: number
          format: double
          description: Daily reward rate
        total_rewards:
          type: number
          format: double
          description: Total rewards amount
        remaining_reward_amount:
          type: number
          format: double
          description: Remaining reward amount
        total_days:
          type: integer
          description: Total number of days in the rewards period
      required:
        - asset_address
        - start_date
        - rate_per_day

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get earnings for user by date

> Returns an array of user earnings per market for a provided day.

Requires CLOB L2 Auth headers.

Results are paginated (100 items per page). Use next_cursor to fetch subsequent pages.
A next_cursor value of "LTE=" indicates the last page.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /rewards/user
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /rewards/user:
    get:
      tags:
        - Rewards
      summary: Get earnings for user by date
      description: >
        Returns an array of user earnings per market for a provided day.


        Requires CLOB L2 Auth headers.


        Results are paginated (100 items per page). Use next_cursor to fetch
        subsequent pages.

        A next_cursor value of "LTE=" indicates the last page.
      operationId: getEarningsForUserForDay
      parameters:
        - name: date
          in: query
          description: Date in YYYY-MM-DD format
          required: true
          schema:
            type: string
            format: date
          example: '2024-03-26'
        - name: signature_type
          in: query
          description: |
            Signature type for address derivation (required for API KEY auth):
            - 0: EOA
            - 1: POLY_PROXY
            - 2: POLY_GNOSIS_SAFE
          required: false
          schema:
            type: integer
            enum:
              - 0
              - 1
              - 2
        - name: maker_address
          in: query
          description: Maker address to query earnings for
          required: false
          schema:
            type: string
          example: '0xFeA4cB3dD4ca7CefD3368653B7D6FF9BcDFca604'
        - name: sponsored
          in: query
          description: If true, returns sponsored-only earnings
          required: false
          schema:
            type: boolean
            default: false
        - name: next_cursor
          in: query
          description: Pagination cursor from previous response
          required: false
          schema:
            type: string
      responses:
        '200':
          description: Successfully retrieved user earnings
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/PaginatedUserEarnings'
              example:
                limit: 100
                count: 1
                next_cursor: LTE=
                data:
                  - date: '2024-03-26T00:00:00Z'
                    condition_id: >-
                      0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af
                    asset_address: '0x9c4E1703476E875070EE25b56A58B008CFb8FA78'
                    maker_address: '0xFeA4cB3dD4ca7CefD3368653B7D6FF9BcDFca604'
                    earnings: 0.237519
                    asset_rate: 1
        '400':
          description: Bad request - Invalid parameters
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                invalid_date:
                  summary: Invalid date format
                  value:
                    error: 'Invalid date (format: YYYY-MM-DD)'
                invalid_signature_type:
                  summary: Invalid signature type
                  value:
                    error: Invalid signature_type
                invalid_maker_address:
                  summary: Invalid maker address
                  value:
                    error: Invalid maker_address
        '401':
          description: Unauthorized - Invalid API key or authentication failed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid API key
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security:
        - polyApiKey: []
          polyAddress: []
          polySignature: []
          polyPassphrase: []
          polyTimestamp: []
components:
  schemas:
    PaginatedUserEarnings:
      type: object
      description: Paginated list of user earnings
      properties:
        limit:
          type: integer
          description: Maximum number of items per page
        count:
          type: integer
          description: Number of items in the current response
        next_cursor:
          type: string
          description: Cursor for the next page. "LTE=" indicates the last page.
        data:
          type: array
          items:
            $ref: '#/components/schemas/UserEarning'
      required:
        - limit
        - count
        - next_cursor
        - data
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
    UserEarning:
      type: object
      description: User earnings for a specific market on a given day
      properties:
        date:
          type: string
          format: date-time
          description: Date of the earnings
        condition_id:
          type: string
          description: Condition ID of the market
        asset_address:
          type: string
          description: Address of the reward asset
        maker_address:
          type: string
          description: Address of the maker
        earnings:
          type: number
          format: double
          description: Amount of earnings in the asset
        asset_rate:
          type: number
          format: double
          description: Exchange rate of the asset
      required:
        - date
        - condition_id
        - asset_address
        - maker_address
        - earnings
  securitySchemes:
    polyApiKey:
      type: apiKey
      in: header
      name: POLY_API_KEY
      description: Your API key
    polyAddress:
      type: apiKey
      in: header
      name: POLY_ADDRESS
      description: Ethereum address associated with the API key
    polySignature:
      type: apiKey
      in: header
      name: POLY_SIGNATURE
      description: HMAC signature of the request
    polyPassphrase:
      type: apiKey
      in: header
      name: POLY_PASSPHRASE
      description: API key passphrase
    polyTimestamp:
      type: apiKey
      in: header
      name: POLY_TIMESTAMP
      description: Unix timestamp of the request

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get total earnings for user by date

> Returns the summed total rewards earnings for a user on a provided day,
grouped by asset address.

Requires CLOB L2 Auth headers.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /rewards/user/total
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /rewards/user/total:
    get:
      tags:
        - Rewards
      summary: Get total earnings for user by date
      description: |
        Returns the summed total rewards earnings for a user on a provided day,
        grouped by asset address.

        Requires CLOB L2 Auth headers.
      operationId: getTotalEarningsForUserForDay
      parameters:
        - name: date
          in: query
          description: Date in YYYY-MM-DD format
          required: true
          schema:
            type: string
            format: date
          example: '2024-03-26'
        - name: signature_type
          in: query
          description: |
            Signature type for address derivation (required for API KEY auth):
            - 0: EOA
            - 1: POLY_PROXY
            - 2: POLY_GNOSIS_SAFE
          required: false
          schema:
            type: integer
            enum:
              - 0
              - 1
              - 2
        - name: maker_address
          in: query
          description: Maker address to query earnings for
          required: false
          schema:
            type: string
          example: '0xFeA4cB3dD4ca7CefD3368653B7D6FF9BcDFca604'
        - name: sponsored
          in: query
          description: If true, aggregates both native and sponsored earnings
          required: false
          schema:
            type: boolean
            default: false
      responses:
        '200':
          description: Successfully retrieved total user earnings
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/TotalUserEarning'
              example:
                - date: '2024-04-09T00:00:00Z'
                  asset_address: '0x9c4E1703476E875070EE25b56A58B008CFb8FA78'
                  maker_address: '0xD527CCdBEB6478488c848465F9947bDA3C2e6994'
                  earnings: 1.59984
                  asset_rate: 0.999357
                - date: '2024-04-09T00:00:00Z'
                  asset_address: '0x69308FB512518e39F9b16112fA8d994F4e2Bf8bB'
                  maker_address: '0xD527CCdBEB6478488c848465F9947bDA3C2e6994'
                  earnings: 8.187219
                  asset_rate: 3.51
        '400':
          description: Bad request - Invalid parameters
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                invalid_date:
                  summary: Invalid date format
                  value:
                    error: 'Invalid date (format: YYYY-MM-DD)'
                invalid_signature_type:
                  summary: Invalid signature type
                  value:
                    error: Invalid signature_type
                invalid_maker_address:
                  summary: Invalid maker address
                  value:
                    error: Invalid maker_address
        '401':
          description: Unauthorized - Invalid API key or authentication failed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid API key
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security:
        - polyApiKey: []
          polyAddress: []
          polySignature: []
          polyPassphrase: []
          polyTimestamp: []
components:
  schemas:
    TotalUserEarning:
      type: object
      description: Total user earnings for a given day grouped by asset
      properties:
        date:
          type: string
          format: date-time
          description: Date of the earnings
        asset_address:
          type: string
          description: Address of the reward asset
        maker_address:
          type: string
          description: Address of the maker
        earnings:
          type: number
          format: double
          description: Total amount of earnings in the asset
        asset_rate:
          type: number
          format: double
          description: Exchange rate of the asset
      required:
        - date
        - asset_address
        - maker_address
        - earnings
        - asset_rate
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
  securitySchemes:
    polyApiKey:
      type: apiKey
      in: header
      name: POLY_API_KEY
      description: Your API key
    polyAddress:
      type: apiKey
      in: header
      name: POLY_ADDRESS
      description: Ethereum address associated with the API key
    polySignature:
      type: apiKey
      in: header
      name: POLY_SIGNATURE
      description: HMAC signature of the request
    polyPassphrase:
      type: apiKey
      in: header
      name: POLY_PASSPHRASE
      description: API key passphrase
    polyTimestamp:
      type: apiKey
      in: header
      name: POLY_TIMESTAMP
      description: Unix timestamp of the request

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get reward percentages for user

> Returns the real-time percentages of rewards that a user is earning per market.

The response is a map of condition_id to the percentage of total rewards
the user is currently earning in that market.

Requires CLOB L2 Auth headers.




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /rewards/user/percentages
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /rewards/user/percentages:
    get:
      tags:
        - Rewards
      summary: Get reward percentages for user
      description: >
        Returns the real-time percentages of rewards that a user is earning per
        market.


        The response is a map of condition_id to the percentage of total rewards

        the user is currently earning in that market.


        Requires CLOB L2 Auth headers.
      operationId: getRewardPercentagesForUser
      parameters:
        - name: signature_type
          in: query
          description: |
            Signature type for address derivation (required for API KEY auth):
            - 0: EOA
            - 1: POLY_PROXY
            - 2: POLY_GNOSIS_SAFE
          required: false
          schema:
            type: integer
            enum:
              - 0
              - 1
              - 2
        - name: maker_address
          in: query
          description: Maker address to query percentages for
          required: false
          schema:
            type: string
          example: '0xFeA4cB3dD4ca7CefD3368653B7D6FF9BcDFca604'
      responses:
        '200':
          description: Successfully retrieved reward percentages
          content:
            application/json:
              schema:
                type: object
                additionalProperties:
                  type: number
                  format: double
                description: Map of condition_id to reward percentage
              example:
                '0x296ea2f3ad438ce7ead77f40d0159bf3e5d8be146f6f615fa253b00e02243f5c': 20
                '0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af': 20
        '400':
          description: Bad request - Invalid parameters
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                invalid_signature_type:
                  summary: Invalid signature type
                  value:
                    error: Invalid signature_type
                invalid_maker_address:
                  summary: Invalid maker address
                  value:
                    error: Invalid maker_address
        '401':
          description: Unauthorized - Invalid API key or authentication failed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid API key
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security:
        - polyApiKey: []
          polyAddress: []
          polySignature: []
          polyPassphrase: []
          polyTimestamp: []
components:
  schemas:
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
  securitySchemes:
    polyApiKey:
      type: apiKey
      in: header
      name: POLY_API_KEY
      description: Your API key
    polyAddress:
      type: apiKey
      in: header
      name: POLY_ADDRESS
      description: Ethereum address associated with the API key
    polySignature:
      type: apiKey
      in: header
      name: POLY_SIGNATURE
      description: HMAC signature of the request
    polyPassphrase:
      type: apiKey
      in: header
      name: POLY_PASSPHRASE
      description: API key passphrase
    polyTimestamp:
      type: apiKey
      in: header
      name: POLY_TIMESTAMP
      description: Unix timestamp of the request

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get user earnings and markets configuration

> Returns an array of current rewards including user earnings and live percentages
per market for a provided day.

Results are paginated (100 items per page by default, max 500). Use next_cursor to fetch subsequent pages.
A next_cursor value of "LTE=" indicates the last page.

Requires CLOB L2 Auth headers.

Optional features:
- Search by question/description using the `q` parameter
- Filter by tag slugs using `tag_slug` parameter (multiple values are OR'ed)
- Filter by favorite markets using `favorite_markets=true`
- Sort by various fields using `order_by` and `position` parameters




## OpenAPI

````yaml /api-spec/clob-openapi.yaml get /rewards/user/markets
openapi: 3.1.0
info:
  title: Polymarket CLOB API
  description: Polymarket CLOB API Reference
  license:
    name: MIT
    identifier: MIT
  version: 1.0.0
servers:
  - url: https://clob.polymarket.com
    description: Production CLOB API
  - url: https://clob-staging.polymarket.com
    description: Staging CLOB API
security: []
tags:
  - name: Trade
    description: Trade endpoints
  - name: Markets
    description: Market data endpoints
  - name: Account
    description: Account and authentication endpoints
  - name: Notifications
    description: User notification endpoints
  - name: Rewards
    description: Rewards and earnings endpoints
  - name: Rebates
    description: Maker rebate endpoints
paths:
  /rewards/user/markets:
    get:
      tags:
        - Rewards
      summary: Get user earnings and markets configuration
      description: >
        Returns an array of current rewards including user earnings and live
        percentages

        per market for a provided day.


        Results are paginated (100 items per page by default, max 500). Use
        next_cursor to fetch subsequent pages.

        A next_cursor value of "LTE=" indicates the last page.


        Requires CLOB L2 Auth headers.


        Optional features:

        - Search by question/description using the `q` parameter

        - Filter by tag slugs using `tag_slug` parameter (multiple values are
        OR'ed)

        - Filter by favorite markets using `favorite_markets=true`

        - Sort by various fields using `order_by` and `position` parameters
      operationId: getUserEarningsAndMarketsConfig
      parameters:
        - name: date
          in: query
          description: Date in YYYY-MM-DD format. Defaults to current date if not provided.
          required: false
          schema:
            type: string
            format: date
          example: '2024-03-26'
        - name: signature_type
          in: query
          description: |
            Signature type for address derivation (required for API KEY auth):
            - 0: EOA
            - 1: POLY_PROXY
            - 2: POLY_GNOSIS_SAFE
          required: false
          schema:
            type: integer
            enum:
              - 0
              - 1
              - 2
        - name: maker_address
          in: query
          description: Maker address to query data for
          required: false
          schema:
            type: string
          example: '0xFeA4cB3dD4ca7CefD3368653B7D6FF9BcDFca604'
        - name: sponsored
          in: query
          description: If true, returns sponsored reward earnings
          required: false
          schema:
            type: boolean
            default: false
        - name: next_cursor
          in: query
          description: Pagination cursor from previous response
          required: false
          schema:
            type: string
        - name: page_size
          in: query
          description: Number of items per page (max 500, values above are capped)
          required: false
          schema:
            type: integer
            default: 100
            maximum: 500
        - name: q
          in: query
          description: Search query to filter markets by question/description
          required: false
          schema:
            type: string
        - name: tag_slug
          in: query
          description: Filter by tag slug (can be repeated for OR logic)
          required: false
          schema:
            type: string
        - name: favorite_markets
          in: query
          description: If true, only show markets favorited by the user (requires auth)
          required: false
          schema:
            type: boolean
            default: false
        - name: no_competition
          in: query
          description: Filter for markets with no competition
          required: false
          schema:
            type: boolean
            default: false
        - name: only_mergeable
          in: query
          description: Filter for only mergeable markets
          required: false
          schema:
            type: boolean
            default: false
        - name: only_open_orders
          in: query
          description: Filter for markets where user has open orders
          required: false
          schema:
            type: boolean
            default: false
        - name: only_open_positions
          in: query
          description: Filter for markets where user has open positions
          required: false
          schema:
            type: boolean
            default: false
        - name: order_by
          in: query
          description: Field to sort by
          required: false
          schema:
            type: string
            enum:
              - max_spread
              - min_size
              - end_date
              - earning_percentage
              - rate_per_day
              - earnings
              - spread
              - competitiveness
              - question
              - price
              - market
              - volume_24hr
        - name: position
          in: query
          description: Sort direction
          required: false
          schema:
            type: string
            enum:
              - ASC
              - DESC
      responses:
        '200':
          description: Successfully retrieved user earnings and market configurations
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/PaginatedUserRewardsMarkets'
              example:
                limit: 100
                count: 1
                total_count: 42
                next_cursor: LTE=
                data:
                  - condition_id: >-
                      0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af
                    market_id: '248849'
                    event_id: '12345'
                    question: Will Trump win the 2024 Iowa Caucus?
                    market_slug: will-trump-win-the-2024-iowa-caucus
                    event_slug: will-trump-win-the-2024-iowa-caucus
                    image: >-
                      https://polymarket-upload.s3.us-east-2.amazonaws.com/trump1+copy.png
                    rewards_max_spread: 99
                    rewards_min_size: 10
                    volume_24hr: 12345.67
                    spread: 0.12
                    market_competitiveness: 0.42
                    tokens:
                      - token_id: >-
                          1343197538147866997676250008839231694243646439454152539053893078719042421992
                        outcome: 'YES'
                        price: 0.8
                      - token_id: >-
                          16678291189211314787145083999015737376658799626183230671758641503291735614088
                        outcome: 'NO'
                        price: 0.2
                    rewards_config:
                      - id: 0
                        asset_address: '0x9c4E1703476E875070EE25b56A58B008CFb8FA78'
                        start_date: '2024-03-01'
                        end_date: '2500-12-31'
                        rate_per_day: 2
                        total_rewards: 92
                    maker_address: '0xD527CCdBEB6478488c848465F9947bDA3C2e6994'
                    earning_percentage: 30
                    earnings:
                      - asset_address: '0x9c4E1703476E875070EE25b56A58B008CFb8FA78'
                        earnings: 0.585051
                        asset_rate: 1.001
        '400':
          description: Bad request - Invalid parameters
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              examples:
                invalid_date:
                  summary: Invalid date format
                  value:
                    error: 'Invalid date (format: YYYY-MM-DD)'
                invalid_signature_type:
                  summary: Invalid signature type
                  value:
                    error: Invalid signature_type
                favorite_requires_auth:
                  summary: Favorite markets requires auth
                  value:
                    error: favorite_markets query argument requires authentication
        '401':
          description: Unauthorized - Invalid API key or authentication failed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Invalid API key
        '500':
          description: Internal server error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
              example:
                error: Internal server error
      security:
        - polyApiKey: []
          polyAddress: []
          polySignature: []
          polyPassphrase: []
          polyTimestamp: []
components:
  schemas:
    PaginatedUserRewardsMarkets:
      type: object
      description: Paginated list of user rewards markets
      properties:
        limit:
          type: integer
          description: Maximum number of items per page
        count:
          type: integer
          description: Number of items in the current response
        total_count:
          type: integer
          description: Total number of items across all pages
        next_cursor:
          type: string
          description: Cursor for the next page. "LTE=" indicates the last page.
        data:
          type: array
          items:
            $ref: '#/components/schemas/UserRewardsMarket'
      required:
        - limit
        - count
        - next_cursor
        - data
    ErrorResponse:
      type: object
      required:
        - error
      properties:
        error:
          type: string
          description: Error message
        code:
          type: string
          description: Machine-readable error code, when provided
        retry_after_seconds:
          type: integer
          description: Number of seconds to wait before retrying, when provided
    UserRewardsMarket:
      type: object
      description: Market with user rewards earnings and configuration
      properties:
        condition_id:
          type: string
          description: Condition ID of the market
        market_id:
          type: string
          description: Market ID
        event_id:
          type: string
          description: Event ID
        question:
          type: string
          description: Market question
        market_slug:
          type: string
          description: URL slug for the market
        event_slug:
          type: string
          description: URL slug for the event
        image:
          type: string
          description: URL to market image
        rewards_max_spread:
          type: number
          description: Maximum spread for rewards eligibility
        rewards_min_size:
          type: number
          description: Minimum order size for rewards eligibility
        volume_24hr:
          type: number
          format: double
          description: 24-hour trading volume
        spread:
          type: number
          format: double
          description: Current spread
        market_competitiveness:
          type: number
          format: double
          description: Competitiveness score of the market
        tokens:
          type: array
          items:
            $ref: '#/components/schemas/RewardsToken'
        rewards_config:
          type: array
          items:
            $ref: '#/components/schemas/CurrentRewardConfig'
        maker_address:
          type: string
          description: Maker address
        earning_percentage:
          type: number
          format: double
          description: Percentage of total rewards the user is earning
        earnings:
          type: array
          items:
            $ref: '#/components/schemas/AssetEarning'
      required:
        - condition_id
        - market_id
        - question
        - tokens
    RewardsToken:
      type: object
      description: Token information for rewards markets
      properties:
        token_id:
          type: string
          description: Token ID
        outcome:
          type: string
          description: Outcome name (e.g., "YES", "NO")
        price:
          type: number
          format: double
          description: Current price of the token
      required:
        - token_id
        - outcome
    CurrentRewardConfig:
      type: object
      description: Reward configuration entry for a current rewards market
      properties:
        id:
          type: integer
          description: Rewards config ID (always 0 on /rewards/markets/current)
        asset_address:
          type: string
          description: Address of the reward asset
        start_date:
          type: string
          format: date
          description: Start date of the rewards period
        end_date:
          type: string
          format: date
          description: End date of the rewards period
        rate_per_day:
          type: number
          format: double
          description: Daily reward rate
        total_rewards:
          type: number
          format: double
          description: Total rewards amount
      required:
        - asset_address
        - start_date
        - rate_per_day
    AssetEarning:
      type: object
      description: Earnings for a specific asset
      properties:
        asset_address:
          type: string
          description: Address of the reward asset
        earnings:
          type: number
          format: double
          description: Amount of earnings
        asset_rate:
          type: number
          format: double
          description: Exchange rate of the asset
      required:
        - asset_address
        - earnings
  securitySchemes:
    polyApiKey:
      type: apiKey
      in: header
      name: POLY_API_KEY
      description: Your API key
    polyAddress:
      type: apiKey
      in: header
      name: POLY_ADDRESS
      description: Ethereum address associated with the API key
    polySignature:
      type: apiKey
      in: header
      name: POLY_SIGNATURE
      description: HMAC signature of the request
    polyPassphrase:
      type: apiKey
      in: header
      name: POLY_PASSPHRASE
      description: API key passphrase
    polyTimestamp:
      type: apiKey
      in: header
      name: POLY_TIMESTAMP
      description: Unix timestamp of the request

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get public profile by wallet address



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /public-profile
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /public-profile:
    get:
      tags:
        - Profiles
      summary: Get public profile by wallet address
      operationId: getPublicProfile
      parameters:
        - name: address
          in: query
          required: true
          description: The wallet address (proxy wallet or user address)
          schema:
            type: string
            pattern: ^0x[a-fA-F0-9]{40}$
          example: '0x7c3db723f1d4d8cb9c550095203b686cb11e5c6b'
      responses:
        '200':
          description: Public profile information
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/PublicProfileResponse'
        '400':
          description: Invalid address format
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/PublicProfileError'
              example:
                type: validation error
                error: invalid address
        '404':
          description: Profile not found
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/PublicProfileError'
              example:
                type: not found error
                error: profile not found
components:
  schemas:
    PublicProfileResponse:
      type: object
      properties:
        createdAt:
          type: string
          format: date-time
          description: ISO 8601 timestamp of when the profile was created
          nullable: true
        proxyWallet:
          type: string
          description: The proxy wallet address
          nullable: true
        profileImage:
          type: string
          format: uri
          description: URL to the profile image
          nullable: true
        displayUsernamePublic:
          type: boolean
          description: Whether the username is displayed publicly
          nullable: true
        bio:
          type: string
          description: Profile bio
          nullable: true
        pseudonym:
          type: string
          description: Auto-generated pseudonym
          nullable: true
        name:
          type: string
          description: User-chosen display name
          nullable: true
        users:
          type: array
          description: Array of associated user objects
          nullable: true
          items:
            $ref: '#/components/schemas/PublicProfileUser'
        xUsername:
          type: string
          description: X (Twitter) username
          nullable: true
        verifiedBadge:
          type: boolean
          description: Whether the profile has a verified badge
          nullable: true
    PublicProfileError:
      type: object
      description: Error response for public profile endpoint
      properties:
        type:
          type: string
          description: Error type classification
        error:
          type: string
          description: Error message
    PublicProfileUser:
      type: object
      description: User object associated with a public profile
      properties:
        id:
          type: string
          description: User ID
        creator:
          type: boolean
          description: Whether the user is a creator
        mod:
          type: boolean
          description: Whether the user is a moderator

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get current positions for a user



## OpenAPI

````yaml /api-spec/data-openapi.yaml get /positions
openapi: 3.0.3
info:
  title: Polymarket Data API
  version: 1.0.0
  description: >
    HTTP API for Polymarket data. This specification documents all public
    routes.
servers:
  - url: https://data-api.polymarket.com
    description: Relative server (same host)
security: []
tags:
  - name: Data API Status
    description: Data API health check
  - name: Core
  - name: Builders
  - name: Misc
paths:
  /positions:
    get:
      tags:
        - Core
      summary: Get current positions for a user
      parameters:
        - in: query
          name: user
          required: true
          schema:
            $ref: '#/components/schemas/Address'
          description: User address (required)
        - in: query
          name: market
          style: form
          explode: false
          schema:
            type: array
            items:
              $ref: '#/components/schemas/Hash64'
          description: >-
            Comma-separated list of condition IDs. Mutually exclusive with
            eventId.
        - in: query
          name: eventId
          style: form
          explode: false
          schema:
            type: array
            items:
              type: integer
              minimum: 1
          description: Comma-separated list of event IDs. Mutually exclusive with market.
        - in: query
          name: sizeThreshold
          schema:
            type: number
            default: 1
            minimum: 0
        - in: query
          name: redeemable
          schema:
            type: boolean
            default: false
        - in: query
          name: mergeable
          schema:
            type: boolean
            default: false
        - in: query
          name: limit
          schema:
            type: integer
            default: 100
            minimum: 0
            maximum: 500
        - in: query
          name: offset
          schema:
            type: integer
            default: 0
            minimum: 0
            maximum: 10000
        - in: query
          name: sortBy
          schema:
            type: string
            enum:
              - CURRENT
              - INITIAL
              - TOKENS
              - CASHPNL
              - PERCENTPNL
              - TITLE
              - RESOLVING
              - PRICE
              - AVGPRICE
            default: TOKENS
        - in: query
          name: sortDirection
          schema:
            type: string
            enum:
              - ASC
              - DESC
            default: DESC
        - in: query
          name: title
          schema:
            type: string
            maxLength: 100
      responses:
        '200':
          description: Success
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Position'
        '400':
          description: Bad Request
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '401':
          description: Unauthorized
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Server Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    Address:
      type: string
      description: User Profile Address (0x-prefixed, 40 hex chars)
      pattern: ^0x[a-fA-F0-9]{40}$
      example: '0x56687bf447db6ffa42ffe2204a05edaa20f55839'
    Hash64:
      type: string
      description: 0x-prefixed 64-hex string
      pattern: ^0x[a-fA-F0-9]{64}$
      example: '0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917'
    Position:
      type: object
      properties:
        proxyWallet:
          $ref: '#/components/schemas/Address'
        asset:
          type: string
        conditionId:
          $ref: '#/components/schemas/Hash64'
        size:
          type: number
        avgPrice:
          type: number
        initialValue:
          type: number
        currentValue:
          type: number
        cashPnl:
          type: number
        percentPnl:
          type: number
        totalBought:
          type: number
        realizedPnl:
          type: number
        percentRealizedPnl:
          type: number
        curPrice:
          type: number
        redeemable:
          type: boolean
        mergeable:
          type: boolean
        title:
          type: string
        slug:
          type: string
        icon:
          type: string
        eventSlug:
          type: string
        outcome:
          type: string
        outcomeIndex:
          type: integer
        oppositeOutcome:
          type: string
        oppositeAsset:
          type: string
        endDate:
          type: string
        negativeRisk:
          type: boolean
    ErrorResponse:
      type: object
      properties:
        error:
          type: string
      required:
        - error

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get closed positions for a user



## OpenAPI

````yaml /api-spec/data-openapi.yaml get /closed-positions
openapi: 3.0.3
info:
  title: Polymarket Data API
  version: 1.0.0
  description: >
    HTTP API for Polymarket data. This specification documents all public
    routes.
servers:
  - url: https://data-api.polymarket.com
    description: Relative server (same host)
security: []
tags:
  - name: Data API Status
    description: Data API health check
  - name: Core
  - name: Builders
  - name: Misc
paths:
  /closed-positions:
    get:
      tags:
        - Core
      summary: Get closed positions for a user
      parameters:
        - in: query
          name: user
          required: true
          schema:
            $ref: '#/components/schemas/Address'
          description: The address of the user in question
        - in: query
          name: market
          style: form
          explode: false
          schema:
            type: array
            items:
              $ref: '#/components/schemas/Hash64'
          description: >-
            The conditionId of the market in question. Supports multiple csv
            separated values. Cannot be used with the eventId param.
        - in: query
          name: title
          schema:
            type: string
            maxLength: 100
          description: Filter by market title
        - in: query
          name: eventId
          style: form
          explode: false
          schema:
            type: array
            items:
              type: integer
              minimum: 1
          description: >-
            The event id of the event in question. Supports multiple csv
            separated values. Returns positions for all markets for those event
            ids. Cannot be used with the market param.
        - in: query
          name: limit
          schema:
            type: integer
            default: 10
            minimum: 0
            maximum: 50
          description: The max number of positions to return
        - in: query
          name: offset
          schema:
            type: integer
            default: 0
            minimum: 0
            maximum: 100000
          description: The starting index for pagination
        - in: query
          name: sortBy
          schema:
            type: string
            enum:
              - REALIZEDPNL
              - TITLE
              - PRICE
              - AVGPRICE
              - TIMESTAMP
            default: REALIZEDPNL
          description: The sort criteria
        - in: query
          name: sortDirection
          schema:
            type: string
            enum:
              - ASC
              - DESC
            default: DESC
          description: The sort direction
      responses:
        '200':
          description: Success
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/ClosedPosition'
        '400':
          description: Bad Request
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '401':
          description: Unauthorized
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Server Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    Address:
      type: string
      description: User Profile Address (0x-prefixed, 40 hex chars)
      pattern: ^0x[a-fA-F0-9]{40}$
      example: '0x56687bf447db6ffa42ffe2204a05edaa20f55839'
    Hash64:
      type: string
      description: 0x-prefixed 64-hex string
      pattern: ^0x[a-fA-F0-9]{64}$
      example: '0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917'
    ClosedPosition:
      type: object
      properties:
        proxyWallet:
          $ref: '#/components/schemas/Address'
        asset:
          type: string
        conditionId:
          $ref: '#/components/schemas/Hash64'
        avgPrice:
          type: number
        totalBought:
          type: number
        realizedPnl:
          type: number
        curPrice:
          type: number
        timestamp:
          type: integer
          format: int64
        title:
          type: string
        slug:
          type: string
        icon:
          type: string
        eventSlug:
          type: string
        outcome:
          type: string
        outcomeIndex:
          type: integer
        oppositeOutcome:
          type: string
        oppositeAsset:
          type: string
        endDate:
          type: string
    ErrorResponse:
      type: object
      properties:
        error:
          type: string
      required:
        - error

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get user activity



## OpenAPI

````yaml /api-spec/data-openapi.yaml get /activity
openapi: 3.0.3
info:
  title: Polymarket Data API
  version: 1.0.0
  description: >
    HTTP API for Polymarket data. This specification documents all public
    routes.
servers:
  - url: https://data-api.polymarket.com
    description: Relative server (same host)
security: []
tags:
  - name: Data API Status
    description: Data API health check
  - name: Core
  - name: Builders
  - name: Misc
paths:
  /activity:
    get:
      tags:
        - Core
      summary: Get user activity
      parameters:
        - in: query
          name: limit
          schema:
            type: integer
            default: 100
            minimum: 0
            maximum: 500
        - in: query
          name: offset
          schema:
            type: integer
            default: 0
            minimum: 0
            maximum: 10000
        - in: query
          name: user
          required: true
          schema:
            $ref: '#/components/schemas/Address'
        - in: query
          name: market
          style: form
          explode: false
          schema:
            type: array
            items:
              $ref: '#/components/schemas/Hash64'
          description: >-
            Comma-separated list of condition IDs. Mutually exclusive with
            eventId.
        - in: query
          name: eventId
          style: form
          explode: false
          schema:
            type: array
            items:
              type: integer
              minimum: 1
          description: Comma-separated list of event IDs. Mutually exclusive with market.
        - in: query
          name: type
          style: form
          explode: false
          schema:
            type: array
            items:
              type: string
              enum:
                - TRADE
                - SPLIT
                - MERGE
                - REDEEM
                - REWARD
                - CONVERSION
                - MAKER_REBATE
                - REFERRAL_REWARD
        - in: query
          name: start
          schema:
            type: integer
            minimum: 0
        - in: query
          name: end
          schema:
            type: integer
            minimum: 0
        - in: query
          name: sortBy
          schema:
            type: string
            enum:
              - TIMESTAMP
              - TOKENS
              - CASH
            default: TIMESTAMP
        - in: query
          name: sortDirection
          schema:
            type: string
            enum:
              - ASC
              - DESC
            default: DESC
        - in: query
          name: side
          schema:
            type: string
            enum:
              - BUY
              - SELL
      responses:
        '200':
          description: Success
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Activity'
        '400':
          description: Bad Request
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '401':
          description: Unauthorized
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Server Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    Address:
      type: string
      description: User Profile Address (0x-prefixed, 40 hex chars)
      pattern: ^0x[a-fA-F0-9]{40}$
      example: '0x56687bf447db6ffa42ffe2204a05edaa20f55839'
    Hash64:
      type: string
      description: 0x-prefixed 64-hex string
      pattern: ^0x[a-fA-F0-9]{64}$
      example: '0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917'
    Activity:
      type: object
      properties:
        proxyWallet:
          $ref: '#/components/schemas/Address'
        timestamp:
          type: integer
          format: int64
        conditionId:
          $ref: '#/components/schemas/Hash64'
        type:
          type: string
          enum:
            - TRADE
            - SPLIT
            - MERGE
            - REDEEM
            - REWARD
            - CONVERSION
            - MAKER_REBATE
            - REFERRAL_REWARD
        size:
          type: number
        usdcSize:
          type: number
        transactionHash:
          type: string
        price:
          type: number
        asset:
          type: string
        side:
          type: string
          enum:
            - BUY
            - SELL
        outcomeIndex:
          type: integer
        title:
          type: string
        slug:
          type: string
        icon:
          type: string
        eventSlug:
          type: string
        outcome:
          type: string
        name:
          type: string
        pseudonym:
          type: string
        bio:
          type: string
        profileImage:
          type: string
        profileImageOptimized:
          type: string
    ErrorResponse:
      type: object
      properties:
        error:
          type: string
      required:
        - error

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get total value of a user's positions



## OpenAPI

````yaml /api-spec/data-openapi.yaml get /value
openapi: 3.0.3
info:
  title: Polymarket Data API
  version: 1.0.0
  description: >
    HTTP API for Polymarket data. This specification documents all public
    routes.
servers:
  - url: https://data-api.polymarket.com
    description: Relative server (same host)
security: []
tags:
  - name: Data API Status
    description: Data API health check
  - name: Core
  - name: Builders
  - name: Misc
paths:
  /value:
    get:
      tags:
        - Core
      summary: Get total value of a user's positions
      parameters:
        - in: query
          name: user
          required: true
          schema:
            $ref: '#/components/schemas/Address'
        - in: query
          name: market
          style: form
          explode: false
          schema:
            type: array
            items:
              $ref: '#/components/schemas/Hash64'
      responses:
        '200':
          description: Success
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Value'
        '400':
          description: Bad Request
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Server Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    Address:
      type: string
      description: User Profile Address (0x-prefixed, 40 hex chars)
      pattern: ^0x[a-fA-F0-9]{40}$
      example: '0x56687bf447db6ffa42ffe2204a05edaa20f55839'
    Hash64:
      type: string
      description: 0x-prefixed 64-hex string
      pattern: ^0x[a-fA-F0-9]{64}$
      example: '0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917'
    Value:
      type: object
      properties:
        user:
          $ref: '#/components/schemas/Address'
        value:
          type: number
    ErrorResponse:
      type: object
      properties:
        error:
          type: string
      required:
        - error

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get trades for a user or markets



## OpenAPI

````yaml /api-spec/data-openapi.yaml get /trades
openapi: 3.0.3
info:
  title: Polymarket Data API
  version: 1.0.0
  description: >
    HTTP API for Polymarket data. This specification documents all public
    routes.
servers:
  - url: https://data-api.polymarket.com
    description: Relative server (same host)
security: []
tags:
  - name: Data API Status
    description: Data API health check
  - name: Core
  - name: Builders
  - name: Misc
paths:
  /trades:
    get:
      tags:
        - Core
      summary: Get trades for a user or markets
      parameters:
        - in: query
          name: limit
          schema:
            type: integer
            default: 100
            minimum: 0
            maximum: 10000
        - in: query
          name: offset
          schema:
            type: integer
            default: 0
            minimum: 0
            maximum: 10000
        - in: query
          name: takerOnly
          schema:
            type: boolean
            default: true
        - in: query
          name: filterType
          schema:
            type: string
            enum:
              - CASH
              - TOKENS
          description: Must be provided together with filterAmount.
        - in: query
          name: filterAmount
          schema:
            type: number
            minimum: 0
          description: Must be provided together with filterType.
        - in: query
          name: market
          style: form
          explode: false
          schema:
            type: array
            items:
              $ref: '#/components/schemas/Hash64'
          description: >-
            Comma-separated list of condition IDs. Mutually exclusive with
            eventId.
        - in: query
          name: eventId
          style: form
          explode: false
          schema:
            type: array
            items:
              type: integer
              minimum: 1
          description: Comma-separated list of event IDs. Mutually exclusive with market.
        - in: query
          name: user
          schema:
            $ref: '#/components/schemas/Address'
        - in: query
          name: side
          schema:
            type: string
            enum:
              - BUY
              - SELL
      responses:
        '200':
          description: Success
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Trade'
        '400':
          description: Bad Request
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '401':
          description: Unauthorized
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Server Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    Hash64:
      type: string
      description: 0x-prefixed 64-hex string
      pattern: ^0x[a-fA-F0-9]{64}$
      example: '0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917'
    Address:
      type: string
      description: User Profile Address (0x-prefixed, 40 hex chars)
      pattern: ^0x[a-fA-F0-9]{40}$
      example: '0x56687bf447db6ffa42ffe2204a05edaa20f55839'
    Trade:
      type: object
      properties:
        proxyWallet:
          $ref: '#/components/schemas/Address'
        side:
          type: string
          enum:
            - BUY
            - SELL
        asset:
          type: string
        conditionId:
          $ref: '#/components/schemas/Hash64'
        size:
          type: number
        price:
          type: number
        timestamp:
          type: integer
          format: int64
        title:
          type: string
        slug:
          type: string
        icon:
          type: string
        eventSlug:
          type: string
        outcome:
          type: string
        outcomeIndex:
          type: integer
        name:
          type: string
        pseudonym:
          type: string
        bio:
          type: string
        profileImage:
          type: string
        profileImageOptimized:
          type: string
        transactionHash:
          type: string
    ErrorResponse:
      type: object
      properties:
        error:
          type: string
      required:
        - error

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get total markets a user has traded



## OpenAPI

````yaml /api-spec/data-openapi.yaml get /traded
openapi: 3.0.3
info:
  title: Polymarket Data API
  version: 1.0.0
  description: >
    HTTP API for Polymarket data. This specification documents all public
    routes.
servers:
  - url: https://data-api.polymarket.com
    description: Relative server (same host)
security: []
tags:
  - name: Data API Status
    description: Data API health check
  - name: Core
  - name: Builders
  - name: Misc
paths:
  /traded:
    get:
      tags:
        - Misc
      summary: Get total markets a user has traded
      parameters:
        - in: query
          name: user
          required: true
          schema:
            $ref: '#/components/schemas/Address'
      responses:
        '200':
          description: Success
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Traded'
        '400':
          description: Bad Request
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '401':
          description: Unauthorized
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Server Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    Address:
      type: string
      description: User Profile Address (0x-prefixed, 40 hex chars)
      pattern: ^0x[a-fA-F0-9]{40}$
      example: '0x56687bf447db6ffa42ffe2204a05edaa20f55839'
    Traded:
      type: object
      properties:
        user:
          $ref: '#/components/schemas/Address'
        traded:
          type: integer
    ErrorResponse:
      type: object
      properties:
        error:
          type: string
      required:
        - error

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get positions for a market



## OpenAPI

````yaml /api-spec/data-openapi.yaml get /v1/market-positions
openapi: 3.0.3
info:
  title: Polymarket Data API
  version: 1.0.0
  description: >
    HTTP API for Polymarket data. This specification documents all public
    routes.
servers:
  - url: https://data-api.polymarket.com
    description: Relative server (same host)
security: []
tags:
  - name: Data API Status
    description: Data API health check
  - name: Core
  - name: Builders
  - name: Misc
paths:
  /v1/market-positions:
    get:
      tags:
        - Core
      summary: Get positions for a market
      parameters:
        - in: query
          name: market
          required: true
          schema:
            $ref: '#/components/schemas/Hash64'
          description: The condition ID of the market to query positions for
        - in: query
          name: user
          schema:
            $ref: '#/components/schemas/Address'
          description: Filter to a single user by proxy wallet address
        - in: query
          name: status
          schema:
            type: string
            enum:
              - OPEN
              - CLOSED
              - ALL
            default: ALL
          description: |
            Filter positions by status.
            - `OPEN` — Only positions with size > 0.01
            - `CLOSED` — Only positions with size <= 0.01
            - `ALL` — All positions regardless of size
        - in: query
          name: sortBy
          schema:
            type: string
            enum:
              - TOKENS
              - CASH_PNL
              - REALIZED_PNL
              - TOTAL_PNL
            default: TOTAL_PNL
          description: |
            Sort positions by:
            - `TOKENS` — Position size (number of tokens)
            - `CASH_PNL` — Unrealized cash PnL
            - `REALIZED_PNL` — Realized PnL
            - `TOTAL_PNL` — Total PnL (cash_pnl + realized_pnl)
        - in: query
          name: sortDirection
          schema:
            type: string
            enum:
              - ASC
              - DESC
            default: DESC
        - in: query
          name: limit
          schema:
            type: integer
            default: 50
            minimum: 0
            maximum: 500
          description: Max number of positions to return per outcome token
        - in: query
          name: offset
          schema:
            type: integer
            default: 0
            minimum: 0
            maximum: 10000
          description: Pagination offset per outcome token
      responses:
        '200':
          description: Success
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/MetaMarketPositionV1'
        '400':
          description: Bad Request
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '401':
          description: Unauthorized
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Server Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    Hash64:
      type: string
      description: 0x-prefixed 64-hex string
      pattern: ^0x[a-fA-F0-9]{64}$
      example: '0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917'
    Address:
      type: string
      description: User Profile Address (0x-prefixed, 40 hex chars)
      pattern: ^0x[a-fA-F0-9]{40}$
      example: '0x56687bf447db6ffa42ffe2204a05edaa20f55839'
    MetaMarketPositionV1:
      type: object
      properties:
        token:
          type: string
          description: The outcome token asset ID
        positions:
          type: array
          items:
            $ref: '#/components/schemas/MarketPositionV1'
    ErrorResponse:
      type: object
      properties:
        error:
          type: string
      required:
        - error
    MarketPositionV1:
      type: object
      properties:
        proxyWallet:
          $ref: '#/components/schemas/Address'
        name:
          type: string
        profileImage:
          type: string
        verified:
          type: boolean
        asset:
          type: string
        conditionId:
          $ref: '#/components/schemas/Hash64'
        avgPrice:
          type: number
        size:
          type: number
        currPrice:
          type: number
        currentValue:
          type: number
        cashPnl:
          type: number
        totalBought:
          type: number
        realizedPnl:
          type: number
        totalPnl:
          type: number
        outcome:
          type: string
        outcomeIndex:
          type: integer

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Download an accounting snapshot (ZIP of CSVs)



## OpenAPI

````yaml /api-spec/data-openapi.yaml get /v1/accounting/snapshot
openapi: 3.0.3
info:
  title: Polymarket Data API
  version: 1.0.0
  description: >
    HTTP API for Polymarket data. This specification documents all public
    routes.
servers:
  - url: https://data-api.polymarket.com
    description: Relative server (same host)
security: []
tags:
  - name: Data API Status
    description: Data API health check
  - name: Core
  - name: Builders
  - name: Misc
paths:
  /v1/accounting/snapshot:
    get:
      tags:
        - Misc
      summary: Download an accounting snapshot (ZIP of CSVs)
      parameters:
        - in: query
          name: user
          required: true
          schema:
            $ref: '#/components/schemas/Address'
          description: User address (0x-prefixed)
      responses:
        '200':
          description: ZIP file containing `positions.csv` and `equity.csv`.
          content:
            application/zip:
              schema:
                type: string
                format: binary
        '400':
          description: Bad Request
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Server Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    Address:
      type: string
      description: User Profile Address (0x-prefixed, 40 hex chars)
      pattern: ^0x[a-fA-F0-9]{40}$
      example: '0x56687bf447db6ffa42ffe2204a05edaa20f55839'
    ErrorResponse:
      type: object
      properties:
        error:
          type: string
      required:
        - error

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get trader leaderboard rankings



## OpenAPI

````yaml /api-spec/data-openapi.yaml get /v1/leaderboard
openapi: 3.0.3
info:
  title: Polymarket Data API
  version: 1.0.0
  description: >
    HTTP API for Polymarket data. This specification documents all public
    routes.
servers:
  - url: https://data-api.polymarket.com
    description: Relative server (same host)
security: []
tags:
  - name: Data API Status
    description: Data API health check
  - name: Core
  - name: Builders
  - name: Misc
paths:
  /v1/leaderboard:
    get:
      tags:
        - Core
      summary: Get trader leaderboard rankings
      parameters:
        - in: query
          name: category
          schema:
            type: string
            enum:
              - OVERALL
              - POLITICS
              - SPORTS
              - CRYPTO
              - CULTURE
              - MENTIONS
              - WEATHER
              - ECONOMICS
              - TECH
              - FINANCE
            default: OVERALL
          description: Market category for the leaderboard
        - in: query
          name: timePeriod
          schema:
            type: string
            enum:
              - DAY
              - WEEK
              - MONTH
              - ALL
            default: DAY
          description: Time period for leaderboard results
        - in: query
          name: orderBy
          schema:
            type: string
            enum:
              - PNL
              - VOL
            default: PNL
          description: Leaderboard ordering criteria
        - in: query
          name: limit
          schema:
            type: integer
            default: 25
            minimum: 1
            maximum: 50
          description: Max number of leaderboard traders to return
        - in: query
          name: offset
          schema:
            type: integer
            default: 0
            minimum: 0
            maximum: 1000
          description: Starting index for pagination
        - in: query
          name: user
          schema:
            $ref: '#/components/schemas/Address'
          description: Limit leaderboard to a single user by address
        - in: query
          name: userName
          schema:
            type: string
          description: Limit leaderboard to a single username
      responses:
        '200':
          description: Success
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/TraderLeaderboardEntry'
        '400':
          description: Bad Request
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Server Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    Address:
      type: string
      description: User Profile Address (0x-prefixed, 40 hex chars)
      pattern: ^0x[a-fA-F0-9]{40}$
      example: '0x56687bf447db6ffa42ffe2204a05edaa20f55839'
    TraderLeaderboardEntry:
      type: object
      properties:
        rank:
          type: string
          description: The rank position of the trader
        proxyWallet:
          $ref: '#/components/schemas/Address'
        userName:
          type: string
          description: The trader's username
        vol:
          type: number
          description: Trading volume for this trader
        pnl:
          type: number
          description: Profit and loss for this trader
        profileImage:
          type: string
          description: URL to the trader's profile image
        xUsername:
          type: string
          description: The trader's X (Twitter) username
        verifiedBadge:
          type: boolean
          description: Whether the trader has a verified badge
    ErrorResponse:
      type: object
      properties:
        error:
          type: string
      required:
        - error

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get aggregated builder leaderboard



## OpenAPI

````yaml /api-spec/data-openapi.yaml get /v1/builders/leaderboard
openapi: 3.0.3
info:
  title: Polymarket Data API
  version: 1.0.0
  description: >
    HTTP API for Polymarket data. This specification documents all public
    routes.
servers:
  - url: https://data-api.polymarket.com
    description: Relative server (same host)
security: []
tags:
  - name: Data API Status
    description: Data API health check
  - name: Core
  - name: Builders
  - name: Misc
paths:
  /v1/builders/leaderboard:
    get:
      tags:
        - Builders
      summary: Get aggregated builder leaderboard
      parameters:
        - in: query
          name: timePeriod
          schema:
            type: string
            enum:
              - DAY
              - WEEK
              - MONTH
              - ALL
            default: DAY
          description: |
            The time period to aggregate results over.
        - in: query
          name: limit
          schema:
            type: integer
            default: 25
            minimum: 0
            maximum: 50
          description: Maximum number of builders to return
        - in: query
          name: offset
          schema:
            type: integer
            default: 0
            minimum: 0
            maximum: 1000
          description: Starting index for pagination
      responses:
        '200':
          description: Success
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/LeaderboardEntry'
        '400':
          description: Bad Request
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Server Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    LeaderboardEntry:
      type: object
      properties:
        rank:
          type: string
          description: The rank position of the builder
        builder:
          type: string
          description: The builder name or identifier
        builderCode:
          type: string
          description: >-
            The builder's onchain attribution code as attached to orders via
            `builderCode` (see CLOB V2). Empty string for legacy builders
            without a registered code.
        volume:
          type: number
          description: Total trading volume attributed to this builder
        activeUsers:
          type: integer
          description: Number of active users for this builder
        verified:
          type: boolean
          description: Whether the builder is verified
        builderLogo:
          type: string
          description: URL to the builder's logo image
    ErrorResponse:
      type: object
      properties:
        error:
          type: string
      required:
        - error

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get daily builder volume time-series



## OpenAPI

````yaml /api-spec/data-openapi.yaml get /v1/builders/volume
openapi: 3.0.3
info:
  title: Polymarket Data API
  version: 1.0.0
  description: >
    HTTP API for Polymarket data. This specification documents all public
    routes.
servers:
  - url: https://data-api.polymarket.com
    description: Relative server (same host)
security: []
tags:
  - name: Data API Status
    description: Data API health check
  - name: Core
  - name: Builders
  - name: Misc
paths:
  /v1/builders/volume:
    get:
      tags:
        - Builders
      summary: Get daily builder volume time-series
      parameters:
        - in: query
          name: timePeriod
          schema:
            type: string
            enum:
              - DAY
              - WEEK
              - MONTH
              - ALL
            default: DAY
          description: |
            The time period to fetch daily records for.
      responses:
        '200':
          description: Success - Returns array of daily volume records
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/BuilderVolumeEntry'
        '400':
          description: Bad Request
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Server Error
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
components:
  schemas:
    BuilderVolumeEntry:
      type: object
      properties:
        dt:
          type: string
          format: date-time
          description: The timestamp for this volume entry in ISO 8601 format
          example: '2025-11-15T00:00:00Z'
        builder:
          type: string
          description: The builder name or identifier
        builderCode:
          type: string
          description: >-
            The builder's onchain attribution code as attached to orders via
            `builderCode` (see CLOB V2). Empty string for legacy builders
            without a registered code.
        builderLogo:
          type: string
          description: URL to the builder's logo image
        verified:
          type: boolean
          description: Whether the builder is verified
        volume:
          type: number
          description: Trading volume for this builder on this date
        activeUsers:
          type: integer
          description: Number of active users for this builder on this date
        rank:
          type: string
          description: The rank position of the builder on this date
    ErrorResponse:
      type: object
      properties:
        error:
          type: string
      required:
        - error

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Search markets, events, and profiles



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /public-search
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /public-search:
    get:
      tags:
        - Search
      summary: Search markets, events, and profiles
      operationId: publicSearch
      parameters:
        - name: q
          in: query
          required: true
          schema:
            type: string
        - name: cache
          in: query
          schema:
            type: boolean
        - name: events_status
          in: query
          schema:
            type: string
        - name: limit_per_type
          in: query
          schema:
            type: integer
        - name: page
          in: query
          schema:
            type: integer
        - name: events_tag
          in: query
          schema:
            type: array
            items:
              type: string
        - name: keep_closed_markets
          in: query
          schema:
            type: integer
        - name: sort
          in: query
          schema:
            type: string
        - name: ascending
          in: query
          schema:
            type: boolean
        - name: search_tags
          in: query
          schema:
            type: boolean
        - name: search_profiles
          in: query
          schema:
            type: boolean
        - name: recurrence
          in: query
          schema:
            type: string
        - name: exclude_tag_id
          in: query
          schema:
            type: array
            items:
              type: integer
        - name: optimized
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: Search results
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Search'
components:
  schemas:
    Search:
      type: object
      properties:
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
          nullable: true
        tags:
          type: array
          items:
            $ref: '#/components/schemas/SearchTag'
          nullable: true
        profiles:
          type: array
          items:
            $ref: '#/components/schemas/Profile'
          nullable: true
        pagination:
          $ref: '#/components/schemas/Pagination'
    Event:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        creationDate:
          type: string
          format: date-time
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        liquidity:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        openInterest:
          type: number
          nullable: true
        sortBy:
          type: string
          nullable: true
        category:
          type: string
          nullable: true
        subcategory:
          type: string
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        published_at:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: number
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        featuredImage:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        parentEvent:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        negRiskMarketID:
          type: string
          nullable: true
        negRiskFeeBips:
          type: integer
          nullable: true
        commentCount:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        featuredImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        subEvents:
          type: array
          items:
            type: string
          nullable: true
        markets:
          type: array
          items:
            $ref: '#/components/schemas/Market'
        series:
          type: array
          items:
            $ref: '#/components/schemas/Series'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        cyom:
          type: boolean
          nullable: true
        closedTime:
          type: string
          format: date-time
          nullable: true
        showAllOutcomes:
          type: boolean
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        enableNegRisk:
          type: boolean
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        eventDate:
          type: string
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        eventWeek:
          type: integer
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        score:
          type: string
          nullable: true
        elapsed:
          type: string
          nullable: true
        period:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        ended:
          type: boolean
          nullable: true
        finishedTimestamp:
          type: string
          format: date-time
          nullable: true
        gmpChartMode:
          type: string
          nullable: true
        eventCreators:
          type: array
          items:
            $ref: '#/components/schemas/EventCreator'
        tweetCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
        featuredOrder:
          type: integer
          nullable: true
        estimateValue:
          type: boolean
          nullable: true
        cantEstimate:
          type: boolean
          nullable: true
        estimatedValue:
          type: string
          nullable: true
        templates:
          type: array
          items:
            $ref: '#/components/schemas/Template'
        spreadsMainLine:
          type: number
          nullable: true
        totalsMainLine:
          type: number
          nullable: true
        carouselMap:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        gameStatus:
          type: string
          nullable: true
    SearchTag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
        slug:
          type: string
        event_count:
          type: integer
    Profile:
      type: object
      properties:
        id:
          type: string
        name:
          type: string
          nullable: true
        user:
          type: integer
          nullable: true
        referral:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        utmSource:
          type: string
          nullable: true
        utmMedium:
          type: string
          nullable: true
        utmCampaign:
          type: string
          nullable: true
        utmContent:
          type: string
          nullable: true
        utmTerm:
          type: string
          nullable: true
        walletActivated:
          type: boolean
          nullable: true
        pseudonym:
          type: string
          nullable: true
        displayUsernamePublic:
          type: boolean
          nullable: true
        profileImage:
          type: string
          nullable: true
        bio:
          type: string
          nullable: true
        proxyWallet:
          type: string
          nullable: true
        profileImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        isCloseOnly:
          type: boolean
          nullable: true
        isCertReq:
          type: boolean
          nullable: true
        certReqDate:
          type: string
          format: date-time
          nullable: true
    Pagination:
      type: object
      properties:
        hasMore:
          type: boolean
        totalResults:
          type: integer
    ImageOptimization:
      type: object
      properties:
        id:
          type: string
        imageUrlSource:
          type: string
          nullable: true
        imageUrlOptimized:
          type: string
          nullable: true
        imageSizeKbSource:
          type: number
          nullable: true
        imageSizeKbOptimized:
          type: number
          nullable: true
        imageOptimizedComplete:
          type: boolean
          nullable: true
        imageOptimizedLastUpdated:
          type: string
          nullable: true
        relID:
          type: integer
          nullable: true
        field:
          type: string
          nullable: true
        relname:
          type: string
          nullable: true
    Market:
      type: object
      properties:
        id:
          type: string
        question:
          type: string
          nullable: true
        conditionId:
          type: string
        slug:
          type: string
          nullable: true
        twitterCardImage:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        category:
          type: string
          nullable: true
        ammType:
          type: string
          nullable: true
        liquidity:
          type: string
          nullable: true
        sponsorName:
          type: string
          nullable: true
        sponsorImage:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        xAxisValue:
          type: string
          nullable: true
        yAxisValue:
          type: string
          nullable: true
        denominationToken:
          type: string
          nullable: true
        fee:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        lowerBound:
          type: string
          nullable: true
        upperBound:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
        outcomePrices:
          type: string
          nullable: true
        volume:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        marketType:
          type: string
          nullable: true
        formatType:
          type: string
          nullable: true
        lowerBoundDate:
          type: string
          nullable: true
        upperBoundDate:
          type: string
          nullable: true
        closed:
          type: boolean
          nullable: true
        marketMakerAddress:
          type: string
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        closedTime:
          type: string
          nullable: true
        wideFormat:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        mailchimpTag:
          type: string
          nullable: true
        featured:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        resolvedBy:
          type: string
          nullable: true
        restricted:
          type: boolean
          nullable: true
        marketGroup:
          type: integer
          nullable: true
        groupItemTitle:
          type: string
          nullable: true
        groupItemThreshold:
          type: string
          nullable: true
        questionID:
          type: string
          nullable: true
        umaEndDate:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        orderPriceMinTickSize:
          type: number
          nullable: true
        orderMinSize:
          type: number
          nullable: true
        umaResolutionStatus:
          type: string
          nullable: true
        curationOrder:
          type: integer
          nullable: true
        volumeNum:
          type: number
          nullable: true
        liquidityNum:
          type: number
          nullable: true
        endDateIso:
          type: string
          nullable: true
        startDateIso:
          type: string
          nullable: true
        umaEndDateIso:
          type: string
          nullable: true
        hasReviewedDates:
          type: boolean
          nullable: true
        readyForCron:
          type: boolean
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        gameStartTime:
          type: string
          nullable: true
        secondsDelay:
          type: integer
          nullable: true
        clobTokenIds:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        shortOutcomes:
          type: string
          nullable: true
        teamAID:
          type: string
          nullable: true
        teamBID:
          type: string
          nullable: true
        umaBond:
          type: string
          nullable: true
        umaReward:
          type: string
          nullable: true
        fpmmLive:
          type: boolean
          nullable: true
        volume24hrAmm:
          type: number
          nullable: true
        volume1wkAmm:
          type: number
          nullable: true
        volume1moAmm:
          type: number
          nullable: true
        volume1yrAmm:
          type: number
          nullable: true
        volume24hrClob:
          type: number
          nullable: true
        volume1wkClob:
          type: number
          nullable: true
        volume1moClob:
          type: number
          nullable: true
        volume1yrClob:
          type: number
          nullable: true
        volumeAmm:
          type: number
          nullable: true
        volumeClob:
          type: number
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        makerBaseFee:
          type: integer
          nullable: true
        takerBaseFee:
          type: integer
          nullable: true
        customLiveness:
          type: integer
          nullable: true
        acceptingOrders:
          type: boolean
          nullable: true
        notificationsEnabled:
          type: boolean
          nullable: true
        score:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        creator:
          type: string
          nullable: true
        ready:
          type: boolean
          nullable: true
        funded:
          type: boolean
          nullable: true
        pastSlugs:
          type: string
          nullable: true
        readyTimestamp:
          type: string
          format: date-time
          nullable: true
        fundedTimestamp:
          type: string
          format: date-time
          nullable: true
        acceptingOrdersTimestamp:
          type: string
          format: date-time
          nullable: true
        competitive:
          type: number
          nullable: true
        rewardsMinSize:
          type: number
          nullable: true
        rewardsMaxSpread:
          type: number
          nullable: true
        spread:
          type: number
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        oneDayPriceChange:
          type: number
          nullable: true
        oneHourPriceChange:
          type: number
          nullable: true
        oneWeekPriceChange:
          type: number
          nullable: true
        oneMonthPriceChange:
          type: number
          nullable: true
        oneYearPriceChange:
          type: number
          nullable: true
        lastTradePrice:
          type: number
          nullable: true
        bestBid:
          type: number
          nullable: true
        bestAsk:
          type: number
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        clearBookOnStart:
          type: boolean
          nullable: true
        chartColor:
          type: string
          nullable: true
        seriesColor:
          type: string
          nullable: true
        showGmpSeries:
          type: boolean
          nullable: true
        showGmpOutcome:
          type: boolean
          nullable: true
        manualActivation:
          type: boolean
          nullable: true
        negRiskOther:
          type: boolean
          nullable: true
        gameId:
          type: string
          nullable: true
        groupItemRange:
          type: string
          nullable: true
        sportsMarketType:
          type: string
          nullable: true
        line:
          type: number
          nullable: true
        umaResolutionStatuses:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        rfqEnabled:
          type: boolean
          nullable: true
        eventStartTime:
          type: string
          format: date-time
          nullable: true
        feesEnabled:
          type: boolean
          nullable: true
        feeSchedule:
          $ref: '#/components/schemas/FeeSchedule'
    Series:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        seriesType:
          type: string
          nullable: true
        recurrence:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: string
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        liquidity:
          type: number
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        pythTokenID:
          type: string
          nullable: true
        cgAssetName:
          type: string
          nullable: true
        score:
          type: integer
          nullable: true
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        commentCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
    Category:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        parentCategory:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Collection:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        collectionType:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        tags:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        headerImage:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        headerImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true
    EventCreator:
      type: object
      properties:
        id:
          type: string
        creatorName:
          type: string
          nullable: true
        creatorHandle:
          type: string
          nullable: true
        creatorUrl:
          type: string
          nullable: true
        creatorImage:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Chat:
      type: object
      properties:
        id:
          type: string
        channelId:
          type: string
          nullable: true
        channelName:
          type: string
          nullable: true
        channelImage:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        endTime:
          type: string
          format: date-time
          nullable: true
    Template:
      type: object
      properties:
        id:
          type: string
        eventTitle:
          type: string
          nullable: true
        eventSlug:
          type: string
          nullable: true
        eventImage:
          type: string
          nullable: true
        marketTitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        sortBy:
          type: string
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
    FeeSchedule:
      type: object
      properties:
        exponent:
          type: number
          nullable: true
        rate:
          type: number
          nullable: true
        takerOnly:
          type: boolean
          nullable: true
        rebateRate:
          type: number
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# List tags



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /tags
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /tags:
    get:
      tags:
        - Tags
      summary: List tags
      operationId: listTags
      parameters:
        - $ref: '#/components/parameters/limit'
        - $ref: '#/components/parameters/offset'
        - $ref: '#/components/parameters/order'
        - $ref: '#/components/parameters/ascending'
        - name: include_template
          in: query
          schema:
            type: boolean
        - name: is_carousel
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: List of tags
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Tag'
components:
  parameters:
    limit:
      name: limit
      in: query
      schema:
        type: integer
        minimum: 0
    offset:
      name: offset
      in: query
      schema:
        type: integer
        minimum: 0
    order:
      name: order
      in: query
      schema:
        type: string
      description: Comma-separated list of fields to order by
    ascending:
      name: ascending
      in: query
      schema:
        type: boolean
  schemas:
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get tag by id



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /tags/{id}
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /tags/{id}:
    get:
      tags:
        - Tags
      summary: Get tag by id
      operationId: getTag
      parameters:
        - $ref: '#/components/parameters/pathId'
        - name: include_template
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: Tag
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Tag'
        '404':
          description: Not found
components:
  parameters:
    pathId:
      name: id
      in: path
      required: true
      schema:
        type: integer
  schemas:
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get tag by slug



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /tags/slug/{slug}
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /tags/slug/{slug}:
    get:
      tags:
        - Tags
      summary: Get tag by slug
      operationId: getTagBySlug
      parameters:
        - $ref: '#/components/parameters/pathSlug'
        - name: include_template
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: Tag
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Tag'
        '404':
          description: Not found
components:
  parameters:
    pathSlug:
      name: slug
      in: path
      required: true
      schema:
        type: string
  schemas:
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get related tags (relationships) by tag id



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /tags/{id}/related-tags
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /tags/{id}/related-tags:
    get:
      tags:
        - Tags
      summary: Get related tags (relationships) by tag id
      operationId: getRelatedTagsById
      parameters:
        - $ref: '#/components/parameters/pathId'
        - name: omit_empty
          in: query
          schema:
            type: boolean
        - name: status
          in: query
          schema:
            type: string
            enum:
              - active
              - closed
              - all
      responses:
        '200':
          description: Related tag relationships
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/RelatedTag'
components:
  parameters:
    pathId:
      name: id
      in: path
      required: true
      schema:
        type: integer
  schemas:
    RelatedTag:
      type: object
      properties:
        id:
          type: string
        tagID:
          type: integer
          nullable: true
        relatedTagID:
          type: integer
          nullable: true
        rank:
          type: integer
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get related tags (relationships) by tag slug



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /tags/slug/{slug}/related-tags
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /tags/slug/{slug}/related-tags:
    get:
      tags:
        - Tags
      summary: Get related tags (relationships) by tag slug
      operationId: getRelatedTagsBySlug
      parameters:
        - $ref: '#/components/parameters/pathSlug'
        - name: omit_empty
          in: query
          schema:
            type: boolean
        - name: status
          in: query
          schema:
            type: string
            enum:
              - active
              - closed
              - all
      responses:
        '200':
          description: Related tag relationships
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/RelatedTag'
components:
  parameters:
    pathSlug:
      name: slug
      in: path
      required: true
      schema:
        type: string
  schemas:
    RelatedTag:
      type: object
      properties:
        id:
          type: string
        tagID:
          type: integer
          nullable: true
        relatedTagID:
          type: integer
          nullable: true
        rank:
          type: integer
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get tags related to a tag id



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /tags/{id}/related-tags/tags
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /tags/{id}/related-tags/tags:
    get:
      tags:
        - Tags
      summary: Get tags related to a tag id
      operationId: getTagsRelatedToATagById
      parameters:
        - $ref: '#/components/parameters/pathId'
        - name: omit_empty
          in: query
          schema:
            type: boolean
        - name: status
          in: query
          schema:
            type: string
            enum:
              - active
              - closed
              - all
      responses:
        '200':
          description: Related tags
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Tag'
components:
  parameters:
    pathId:
      name: id
      in: path
      required: true
      schema:
        type: integer
  schemas:
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get tags related to a tag slug



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /tags/slug/{slug}/related-tags/tags
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /tags/slug/{slug}/related-tags/tags:
    get:
      tags:
        - Tags
      summary: Get tags related to a tag slug
      operationId: getTagsRelatedToATagBySlug
      parameters:
        - $ref: '#/components/parameters/pathSlug'
        - name: omit_empty
          in: query
          schema:
            type: boolean
        - name: status
          in: query
          schema:
            type: string
            enum:
              - active
              - closed
              - all
      responses:
        '200':
          description: Related tags
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Tag'
components:
  parameters:
    pathSlug:
      name: slug
      in: path
      required: true
      schema:
        type: string
  schemas:
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# List series



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /series
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /series:
    get:
      tags:
        - Series
      summary: List series
      operationId: listSeries
      parameters:
        - $ref: '#/components/parameters/limit'
        - $ref: '#/components/parameters/offset'
        - $ref: '#/components/parameters/order'
        - $ref: '#/components/parameters/ascending'
        - name: slug
          in: query
          schema:
            type: array
            items:
              type: string
        - name: categories_ids
          in: query
          schema:
            type: array
            items:
              type: integer
        - name: categories_labels
          in: query
          schema:
            type: array
            items:
              type: string
        - name: closed
          in: query
          schema:
            type: boolean
        - name: include_chat
          in: query
          schema:
            type: boolean
        - name: recurrence
          in: query
          schema:
            type: string
        - name: exclude_events
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: List of series
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Series'
components:
  parameters:
    limit:
      name: limit
      in: query
      schema:
        type: integer
        minimum: 0
    offset:
      name: offset
      in: query
      schema:
        type: integer
        minimum: 0
    order:
      name: order
      in: query
      schema:
        type: string
      description: Comma-separated list of fields to order by
    ascending:
      name: ascending
      in: query
      schema:
        type: boolean
  schemas:
    Series:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        seriesType:
          type: string
          nullable: true
        recurrence:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: string
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        liquidity:
          type: number
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        pythTokenID:
          type: string
          nullable: true
        cgAssetName:
          type: string
          nullable: true
        score:
          type: integer
          nullable: true
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        commentCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
    Event:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        creationDate:
          type: string
          format: date-time
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        liquidity:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        openInterest:
          type: number
          nullable: true
        sortBy:
          type: string
          nullable: true
        category:
          type: string
          nullable: true
        subcategory:
          type: string
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        published_at:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: number
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        featuredImage:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        parentEvent:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        negRiskMarketID:
          type: string
          nullable: true
        negRiskFeeBips:
          type: integer
          nullable: true
        commentCount:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        featuredImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        subEvents:
          type: array
          items:
            type: string
          nullable: true
        markets:
          type: array
          items:
            $ref: '#/components/schemas/Market'
        series:
          type: array
          items:
            $ref: '#/components/schemas/Series'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        cyom:
          type: boolean
          nullable: true
        closedTime:
          type: string
          format: date-time
          nullable: true
        showAllOutcomes:
          type: boolean
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        enableNegRisk:
          type: boolean
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        eventDate:
          type: string
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        eventWeek:
          type: integer
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        score:
          type: string
          nullable: true
        elapsed:
          type: string
          nullable: true
        period:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        ended:
          type: boolean
          nullable: true
        finishedTimestamp:
          type: string
          format: date-time
          nullable: true
        gmpChartMode:
          type: string
          nullable: true
        eventCreators:
          type: array
          items:
            $ref: '#/components/schemas/EventCreator'
        tweetCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
        featuredOrder:
          type: integer
          nullable: true
        estimateValue:
          type: boolean
          nullable: true
        cantEstimate:
          type: boolean
          nullable: true
        estimatedValue:
          type: string
          nullable: true
        templates:
          type: array
          items:
            $ref: '#/components/schemas/Template'
        spreadsMainLine:
          type: number
          nullable: true
        totalsMainLine:
          type: number
          nullable: true
        carouselMap:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        gameStatus:
          type: string
          nullable: true
    Collection:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        collectionType:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        tags:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        headerImage:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        headerImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
    Category:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        parentCategory:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true
    Chat:
      type: object
      properties:
        id:
          type: string
        channelId:
          type: string
          nullable: true
        channelName:
          type: string
          nullable: true
        channelImage:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        endTime:
          type: string
          format: date-time
          nullable: true
    ImageOptimization:
      type: object
      properties:
        id:
          type: string
        imageUrlSource:
          type: string
          nullable: true
        imageUrlOptimized:
          type: string
          nullable: true
        imageSizeKbSource:
          type: number
          nullable: true
        imageSizeKbOptimized:
          type: number
          nullable: true
        imageOptimizedComplete:
          type: boolean
          nullable: true
        imageOptimizedLastUpdated:
          type: string
          nullable: true
        relID:
          type: integer
          nullable: true
        field:
          type: string
          nullable: true
        relname:
          type: string
          nullable: true
    Market:
      type: object
      properties:
        id:
          type: string
        question:
          type: string
          nullable: true
        conditionId:
          type: string
        slug:
          type: string
          nullable: true
        twitterCardImage:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        category:
          type: string
          nullable: true
        ammType:
          type: string
          nullable: true
        liquidity:
          type: string
          nullable: true
        sponsorName:
          type: string
          nullable: true
        sponsorImage:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        xAxisValue:
          type: string
          nullable: true
        yAxisValue:
          type: string
          nullable: true
        denominationToken:
          type: string
          nullable: true
        fee:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        lowerBound:
          type: string
          nullable: true
        upperBound:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
        outcomePrices:
          type: string
          nullable: true
        volume:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        marketType:
          type: string
          nullable: true
        formatType:
          type: string
          nullable: true
        lowerBoundDate:
          type: string
          nullable: true
        upperBoundDate:
          type: string
          nullable: true
        closed:
          type: boolean
          nullable: true
        marketMakerAddress:
          type: string
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        closedTime:
          type: string
          nullable: true
        wideFormat:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        mailchimpTag:
          type: string
          nullable: true
        featured:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        resolvedBy:
          type: string
          nullable: true
        restricted:
          type: boolean
          nullable: true
        marketGroup:
          type: integer
          nullable: true
        groupItemTitle:
          type: string
          nullable: true
        groupItemThreshold:
          type: string
          nullable: true
        questionID:
          type: string
          nullable: true
        umaEndDate:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        orderPriceMinTickSize:
          type: number
          nullable: true
        orderMinSize:
          type: number
          nullable: true
        umaResolutionStatus:
          type: string
          nullable: true
        curationOrder:
          type: integer
          nullable: true
        volumeNum:
          type: number
          nullable: true
        liquidityNum:
          type: number
          nullable: true
        endDateIso:
          type: string
          nullable: true
        startDateIso:
          type: string
          nullable: true
        umaEndDateIso:
          type: string
          nullable: true
        hasReviewedDates:
          type: boolean
          nullable: true
        readyForCron:
          type: boolean
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        gameStartTime:
          type: string
          nullable: true
        secondsDelay:
          type: integer
          nullable: true
        clobTokenIds:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        shortOutcomes:
          type: string
          nullable: true
        teamAID:
          type: string
          nullable: true
        teamBID:
          type: string
          nullable: true
        umaBond:
          type: string
          nullable: true
        umaReward:
          type: string
          nullable: true
        fpmmLive:
          type: boolean
          nullable: true
        volume24hrAmm:
          type: number
          nullable: true
        volume1wkAmm:
          type: number
          nullable: true
        volume1moAmm:
          type: number
          nullable: true
        volume1yrAmm:
          type: number
          nullable: true
        volume24hrClob:
          type: number
          nullable: true
        volume1wkClob:
          type: number
          nullable: true
        volume1moClob:
          type: number
          nullable: true
        volume1yrClob:
          type: number
          nullable: true
        volumeAmm:
          type: number
          nullable: true
        volumeClob:
          type: number
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        makerBaseFee:
          type: integer
          nullable: true
        takerBaseFee:
          type: integer
          nullable: true
        customLiveness:
          type: integer
          nullable: true
        acceptingOrders:
          type: boolean
          nullable: true
        notificationsEnabled:
          type: boolean
          nullable: true
        score:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        creator:
          type: string
          nullable: true
        ready:
          type: boolean
          nullable: true
        funded:
          type: boolean
          nullable: true
        pastSlugs:
          type: string
          nullable: true
        readyTimestamp:
          type: string
          format: date-time
          nullable: true
        fundedTimestamp:
          type: string
          format: date-time
          nullable: true
        acceptingOrdersTimestamp:
          type: string
          format: date-time
          nullable: true
        competitive:
          type: number
          nullable: true
        rewardsMinSize:
          type: number
          nullable: true
        rewardsMaxSpread:
          type: number
          nullable: true
        spread:
          type: number
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        oneDayPriceChange:
          type: number
          nullable: true
        oneHourPriceChange:
          type: number
          nullable: true
        oneWeekPriceChange:
          type: number
          nullable: true
        oneMonthPriceChange:
          type: number
          nullable: true
        oneYearPriceChange:
          type: number
          nullable: true
        lastTradePrice:
          type: number
          nullable: true
        bestBid:
          type: number
          nullable: true
        bestAsk:
          type: number
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        clearBookOnStart:
          type: boolean
          nullable: true
        chartColor:
          type: string
          nullable: true
        seriesColor:
          type: string
          nullable: true
        showGmpSeries:
          type: boolean
          nullable: true
        showGmpOutcome:
          type: boolean
          nullable: true
        manualActivation:
          type: boolean
          nullable: true
        negRiskOther:
          type: boolean
          nullable: true
        gameId:
          type: string
          nullable: true
        groupItemRange:
          type: string
          nullable: true
        sportsMarketType:
          type: string
          nullable: true
        line:
          type: number
          nullable: true
        umaResolutionStatuses:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        rfqEnabled:
          type: boolean
          nullable: true
        eventStartTime:
          type: string
          format: date-time
          nullable: true
        feesEnabled:
          type: boolean
          nullable: true
        feeSchedule:
          $ref: '#/components/schemas/FeeSchedule'
    EventCreator:
      type: object
      properties:
        id:
          type: string
        creatorName:
          type: string
          nullable: true
        creatorHandle:
          type: string
          nullable: true
        creatorUrl:
          type: string
          nullable: true
        creatorImage:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Template:
      type: object
      properties:
        id:
          type: string
        eventTitle:
          type: string
          nullable: true
        eventSlug:
          type: string
          nullable: true
        eventImage:
          type: string
          nullable: true
        marketTitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        sortBy:
          type: string
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
    FeeSchedule:
      type: object
      properties:
        exponent:
          type: number
          nullable: true
        rate:
          type: number
          nullable: true
        takerOnly:
          type: boolean
          nullable: true
        rebateRate:
          type: number
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get series by id



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /series/{id}
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /series/{id}:
    get:
      tags:
        - Series
      summary: Get series by id
      operationId: getSeries
      parameters:
        - $ref: '#/components/parameters/pathId'
        - name: include_chat
          in: query
          schema:
            type: boolean
      responses:
        '200':
          description: Series
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Series'
        '404':
          description: Not found
components:
  parameters:
    pathId:
      name: id
      in: path
      required: true
      schema:
        type: integer
  schemas:
    Series:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        seriesType:
          type: string
          nullable: true
        recurrence:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: string
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        liquidity:
          type: number
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        pythTokenID:
          type: string
          nullable: true
        cgAssetName:
          type: string
          nullable: true
        score:
          type: integer
          nullable: true
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        commentCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
    Event:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        creationDate:
          type: string
          format: date-time
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        liquidity:
          type: number
          nullable: true
        volume:
          type: number
          nullable: true
        openInterest:
          type: number
          nullable: true
        sortBy:
          type: string
          nullable: true
        category:
          type: string
          nullable: true
        subcategory:
          type: string
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        published_at:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        competitive:
          type: number
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        featuredImage:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        parentEvent:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        negRiskMarketID:
          type: string
          nullable: true
        negRiskFeeBips:
          type: integer
          nullable: true
        commentCount:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        featuredImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        subEvents:
          type: array
          items:
            type: string
          nullable: true
        markets:
          type: array
          items:
            $ref: '#/components/schemas/Market'
        series:
          type: array
          items:
            $ref: '#/components/schemas/Series'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        collections:
          type: array
          items:
            $ref: '#/components/schemas/Collection'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        cyom:
          type: boolean
          nullable: true
        closedTime:
          type: string
          format: date-time
          nullable: true
        showAllOutcomes:
          type: boolean
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        enableNegRisk:
          type: boolean
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        eventDate:
          type: string
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        eventWeek:
          type: integer
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        score:
          type: string
          nullable: true
        elapsed:
          type: string
          nullable: true
        period:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        ended:
          type: boolean
          nullable: true
        finishedTimestamp:
          type: string
          format: date-time
          nullable: true
        gmpChartMode:
          type: string
          nullable: true
        eventCreators:
          type: array
          items:
            $ref: '#/components/schemas/EventCreator'
        tweetCount:
          type: integer
          nullable: true
        chats:
          type: array
          items:
            $ref: '#/components/schemas/Chat'
        featuredOrder:
          type: integer
          nullable: true
        estimateValue:
          type: boolean
          nullable: true
        cantEstimate:
          type: boolean
          nullable: true
        estimatedValue:
          type: string
          nullable: true
        templates:
          type: array
          items:
            $ref: '#/components/schemas/Template'
        spreadsMainLine:
          type: number
          nullable: true
        totalsMainLine:
          type: number
          nullable: true
        carouselMap:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        gameStatus:
          type: string
          nullable: true
    Collection:
      type: object
      properties:
        id:
          type: string
        ticker:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        title:
          type: string
          nullable: true
        subtitle:
          type: string
          nullable: true
        collectionType:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        tags:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        headerImage:
          type: string
          nullable: true
        layout:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        closed:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        featured:
          type: boolean
          nullable: true
        restricted:
          type: boolean
          nullable: true
        isTemplate:
          type: boolean
          nullable: true
        templateVariables:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        headerImageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
    Category:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        parentCategory:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: string
          nullable: true
        updatedBy:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Tag:
      type: object
      properties:
        id:
          type: string
        label:
          type: string
          nullable: true
        slug:
          type: string
          nullable: true
        forceShow:
          type: boolean
          nullable: true
        publishedAt:
          type: string
          nullable: true
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        forceHide:
          type: boolean
          nullable: true
        isCarousel:
          type: boolean
          nullable: true
    Chat:
      type: object
      properties:
        id:
          type: string
        channelId:
          type: string
          nullable: true
        channelName:
          type: string
          nullable: true
        channelImage:
          type: string
          nullable: true
        live:
          type: boolean
          nullable: true
        startTime:
          type: string
          format: date-time
          nullable: true
        endTime:
          type: string
          format: date-time
          nullable: true
    ImageOptimization:
      type: object
      properties:
        id:
          type: string
        imageUrlSource:
          type: string
          nullable: true
        imageUrlOptimized:
          type: string
          nullable: true
        imageSizeKbSource:
          type: number
          nullable: true
        imageSizeKbOptimized:
          type: number
          nullable: true
        imageOptimizedComplete:
          type: boolean
          nullable: true
        imageOptimizedLastUpdated:
          type: string
          nullable: true
        relID:
          type: integer
          nullable: true
        field:
          type: string
          nullable: true
        relname:
          type: string
          nullable: true
    Market:
      type: object
      properties:
        id:
          type: string
        question:
          type: string
          nullable: true
        conditionId:
          type: string
        slug:
          type: string
          nullable: true
        twitterCardImage:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        endDate:
          type: string
          format: date-time
          nullable: true
        category:
          type: string
          nullable: true
        ammType:
          type: string
          nullable: true
        liquidity:
          type: string
          nullable: true
        sponsorName:
          type: string
          nullable: true
        sponsorImage:
          type: string
          nullable: true
        startDate:
          type: string
          format: date-time
          nullable: true
        xAxisValue:
          type: string
          nullable: true
        yAxisValue:
          type: string
          nullable: true
        denominationToken:
          type: string
          nullable: true
        fee:
          type: string
          nullable: true
        image:
          type: string
          nullable: true
        icon:
          type: string
          nullable: true
        lowerBound:
          type: string
          nullable: true
        upperBound:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
        outcomePrices:
          type: string
          nullable: true
        volume:
          type: string
          nullable: true
        active:
          type: boolean
          nullable: true
        marketType:
          type: string
          nullable: true
        formatType:
          type: string
          nullable: true
        lowerBoundDate:
          type: string
          nullable: true
        upperBoundDate:
          type: string
          nullable: true
        closed:
          type: boolean
          nullable: true
        marketMakerAddress:
          type: string
        createdBy:
          type: integer
          nullable: true
        updatedBy:
          type: integer
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
        closedTime:
          type: string
          nullable: true
        wideFormat:
          type: boolean
          nullable: true
        new:
          type: boolean
          nullable: true
        mailchimpTag:
          type: string
          nullable: true
        featured:
          type: boolean
          nullable: true
        archived:
          type: boolean
          nullable: true
        resolvedBy:
          type: string
          nullable: true
        restricted:
          type: boolean
          nullable: true
        marketGroup:
          type: integer
          nullable: true
        groupItemTitle:
          type: string
          nullable: true
        groupItemThreshold:
          type: string
          nullable: true
        questionID:
          type: string
          nullable: true
        umaEndDate:
          type: string
          nullable: true
        enableOrderBook:
          type: boolean
          nullable: true
        orderPriceMinTickSize:
          type: number
          nullable: true
        orderMinSize:
          type: number
          nullable: true
        umaResolutionStatus:
          type: string
          nullable: true
        curationOrder:
          type: integer
          nullable: true
        volumeNum:
          type: number
          nullable: true
        liquidityNum:
          type: number
          nullable: true
        endDateIso:
          type: string
          nullable: true
        startDateIso:
          type: string
          nullable: true
        umaEndDateIso:
          type: string
          nullable: true
        hasReviewedDates:
          type: boolean
          nullable: true
        readyForCron:
          type: boolean
          nullable: true
        commentsEnabled:
          type: boolean
          nullable: true
        volume24hr:
          type: number
          nullable: true
        volume1wk:
          type: number
          nullable: true
        volume1mo:
          type: number
          nullable: true
        volume1yr:
          type: number
          nullable: true
        gameStartTime:
          type: string
          nullable: true
        secondsDelay:
          type: integer
          nullable: true
        clobTokenIds:
          type: string
          nullable: true
        disqusThread:
          type: string
          nullable: true
        shortOutcomes:
          type: string
          nullable: true
        teamAID:
          type: string
          nullable: true
        teamBID:
          type: string
          nullable: true
        umaBond:
          type: string
          nullable: true
        umaReward:
          type: string
          nullable: true
        fpmmLive:
          type: boolean
          nullable: true
        volume24hrAmm:
          type: number
          nullable: true
        volume1wkAmm:
          type: number
          nullable: true
        volume1moAmm:
          type: number
          nullable: true
        volume1yrAmm:
          type: number
          nullable: true
        volume24hrClob:
          type: number
          nullable: true
        volume1wkClob:
          type: number
          nullable: true
        volume1moClob:
          type: number
          nullable: true
        volume1yrClob:
          type: number
          nullable: true
        volumeAmm:
          type: number
          nullable: true
        volumeClob:
          type: number
          nullable: true
        liquidityAmm:
          type: number
          nullable: true
        liquidityClob:
          type: number
          nullable: true
        makerBaseFee:
          type: integer
          nullable: true
        takerBaseFee:
          type: integer
          nullable: true
        customLiveness:
          type: integer
          nullable: true
        acceptingOrders:
          type: boolean
          nullable: true
        notificationsEnabled:
          type: boolean
          nullable: true
        score:
          type: integer
          nullable: true
        imageOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        iconOptimized:
          $ref: '#/components/schemas/ImageOptimization'
        events:
          type: array
          items:
            $ref: '#/components/schemas/Event'
        categories:
          type: array
          items:
            $ref: '#/components/schemas/Category'
        tags:
          type: array
          items:
            $ref: '#/components/schemas/Tag'
        creator:
          type: string
          nullable: true
        ready:
          type: boolean
          nullable: true
        funded:
          type: boolean
          nullable: true
        pastSlugs:
          type: string
          nullable: true
        readyTimestamp:
          type: string
          format: date-time
          nullable: true
        fundedTimestamp:
          type: string
          format: date-time
          nullable: true
        acceptingOrdersTimestamp:
          type: string
          format: date-time
          nullable: true
        competitive:
          type: number
          nullable: true
        rewardsMinSize:
          type: number
          nullable: true
        rewardsMaxSpread:
          type: number
          nullable: true
        spread:
          type: number
          nullable: true
        automaticallyResolved:
          type: boolean
          nullable: true
        oneDayPriceChange:
          type: number
          nullable: true
        oneHourPriceChange:
          type: number
          nullable: true
        oneWeekPriceChange:
          type: number
          nullable: true
        oneMonthPriceChange:
          type: number
          nullable: true
        oneYearPriceChange:
          type: number
          nullable: true
        lastTradePrice:
          type: number
          nullable: true
        bestBid:
          type: number
          nullable: true
        bestAsk:
          type: number
          nullable: true
        automaticallyActive:
          type: boolean
          nullable: true
        clearBookOnStart:
          type: boolean
          nullable: true
        chartColor:
          type: string
          nullable: true
        seriesColor:
          type: string
          nullable: true
        showGmpSeries:
          type: boolean
          nullable: true
        showGmpOutcome:
          type: boolean
          nullable: true
        manualActivation:
          type: boolean
          nullable: true
        negRiskOther:
          type: boolean
          nullable: true
        gameId:
          type: string
          nullable: true
        groupItemRange:
          type: string
          nullable: true
        sportsMarketType:
          type: string
          nullable: true
        line:
          type: number
          nullable: true
        umaResolutionStatuses:
          type: string
          nullable: true
        pendingDeployment:
          type: boolean
          nullable: true
        deploying:
          type: boolean
          nullable: true
        deployingTimestamp:
          type: string
          format: date-time
          nullable: true
        scheduledDeploymentTimestamp:
          type: string
          format: date-time
          nullable: true
        rfqEnabled:
          type: boolean
          nullable: true
        eventStartTime:
          type: string
          format: date-time
          nullable: true
        feesEnabled:
          type: boolean
          nullable: true
        feeSchedule:
          $ref: '#/components/schemas/FeeSchedule'
    EventCreator:
      type: object
      properties:
        id:
          type: string
        creatorName:
          type: string
          nullable: true
        creatorHandle:
          type: string
          nullable: true
        creatorUrl:
          type: string
          nullable: true
        creatorImage:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true
    Template:
      type: object
      properties:
        id:
          type: string
        eventTitle:
          type: string
          nullable: true
        eventSlug:
          type: string
          nullable: true
        eventImage:
          type: string
          nullable: true
        marketTitle:
          type: string
          nullable: true
        description:
          type: string
          nullable: true
        resolutionSource:
          type: string
          nullable: true
        negRisk:
          type: boolean
          nullable: true
        sortBy:
          type: string
          nullable: true
        showMarketImages:
          type: boolean
          nullable: true
        seriesSlug:
          type: string
          nullable: true
        outcomes:
          type: string
          nullable: true
    FeeSchedule:
      type: object
      properties:
        exponent:
          type: number
          nullable: true
        rate:
          type: number
          nullable: true
        takerOnly:
          type: boolean
          nullable: true
        rebateRate:
          type: number
          nullable: true

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get sports metadata information



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /sports
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /sports:
    get:
      tags:
        - Sports
      summary: Get sports metadata information
      operationId: getSportsMetadata
      responses:
        '200':
          description: >-
            List of sports metadata objects containing sport configuration
            details, visual assets, and related identifiers
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/SportsMetadata'
components:
  schemas:
    SportsMetadata:
      type: object
      properties:
        sport:
          type: string
          description: The sport identifier or abbreviation
        image:
          type: string
          format: uri
          description: URL to the sport's logo or image asset
        resolution:
          type: string
          format: uri
          description: >-
            URL to the official resolution source for the sport (e.g., league
            website)
        ordering:
          type: string
          description: Preferred ordering for sport display, typically "home" or "away"
        tags:
          type: string
          description: >-
            Comma-separated list of tag IDs associated with the sport for
            categorization and filtering
        series:
          type: string
          description: >-
            Series identifier linking the sport to a specific tournament or
            season series

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get valid sports market types



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /sports/market-types
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /sports/market-types:
    get:
      tags:
        - Sports
      summary: Get valid sports market types
      operationId: getSportsMarketTypes
      responses:
        '200':
          description: List of valid sports market types
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/SportsMarketTypesResponse'
components:
  schemas:
    SportsMarketTypesResponse:
      type: object
      properties:
        marketTypes:
          type: array
          description: List of all valid sports market types
          items:
            type: string

````> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# List teams



## OpenAPI

````yaml /api-spec/gamma-openapi.yaml get /teams
openapi: 3.0.3
info:
  title: Markets API
  version: 1.0.0
  description: REST API specification for public endpoints used by the Markets service.
servers:
  - url: https://gamma-api.polymarket.com
    description: Polymarket Gamma API Production Server
security: []
tags:
  - name: Gamma Status
    description: Gamma API status and health check
  - name: Sports
    description: Sports-related endpoints including teams and game data
  - name: Tags
    description: Tag management and related tag operations
  - name: Events
    description: Event management and event-related operations
  - name: Markets
    description: Market data and market-related operations
  - name: Comments
    description: Comment system and user interactions
  - name: Series
    description: Series management and related operations
  - name: Profiles
    description: User profile management
  - name: Search
    description: Search functionality across different entity types
paths:
  /teams:
    get:
      tags:
        - Sports
      summary: List teams
      operationId: listTeams
      parameters:
        - $ref: '#/components/parameters/limit'
        - $ref: '#/components/parameters/offset'
        - $ref: '#/components/parameters/order'
        - $ref: '#/components/parameters/ascending'
        - name: league
          in: query
          schema:
            type: array
            items:
              type: string
        - name: name
          in: query
          schema:
            type: array
            items:
              type: string
        - name: abbreviation
          in: query
          schema:
            type: array
            items:
              type: string
      responses:
        '200':
          description: List of teams
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Team'
components:
  parameters:
    limit:
      name: limit
      in: query
      schema:
        type: integer
        minimum: 0
    offset:
      name: offset
      in: query
      schema:
        type: integer
        minimum: 0
    order:
      name: order
      in: query
      schema:
        type: string
      description: Comma-separated list of fields to order by
    ascending:
      name: ascending
      in: query
      schema:
        type: boolean
  schemas:
    Team:
      type: object
      properties:
        id:
          type: integer
        name:
          type: string
          nullable: true
        league:
          type: string
          nullable: true
        record:
          type: string
          nullable: true
        logo:
          type: string
          nullable: true
        abbreviation:
          type: string
          nullable: true
        alias:
          type: string
          nullable: true
        createdAt:
          type: string
          format: date-time
          nullable: true
        updatedAt:
          type: string
          format: date-time
          nullable: true

````