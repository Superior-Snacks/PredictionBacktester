> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Orderbook

> Reading the orderbook, prices, spreads, and midpoints

The orderbook is a public endpoint — no authentication required. You can read prices and liquidity using the SDK or REST API directly.

<CodeGroup>
  ```typescript TypeScript theme={null}
  import { ClobClient } from "@polymarket/clob-client";

  const client = new ClobClient("https://clob.polymarket.com", 137);
  ```

  ```python Python theme={null}
  from py_clob_client.client import ClobClient

  client = ClobClient("https://clob.polymarket.com", chain_id=137)
  ```

  ```rust Rust theme={null}
  use polymarket_client_sdk::clob::Client;

  let client = Client::default(); // https://clob.polymarket.com
  ```

  ```bash REST theme={null}
  # Base URL for all orderbook endpoints
  https://clob.polymarket.com
  ```
</CodeGroup>

***

## Get the Orderbook

Fetch the full orderbook for a token, including all resting bid and ask levels:

<CodeGroup>
  ```typescript TypeScript theme={null}
  const book = await client.getOrderBook("TOKEN_ID");

  console.log("Best bid:", book.bids[0]);
  console.log("Best ask:", book.asks[0]);
  console.log("Tick size:", book.tick_size);
  ```

  ```python Python theme={null}
  book = client.get_order_book("TOKEN_ID")

  print("Best bid:", book["bids"][0])
  print("Best ask:", book["asks"][0])
  print("Tick size:", book["tick_size"])
  ```

  ```rust Rust theme={null}
  use polymarket_client_sdk::clob::types::request::OrderBookSummaryRequest;

  let token_id = "TOKEN_ID".parse()?;
  let request = OrderBookSummaryRequest::builder().token_id(token_id).build();
  let book = client.order_book(&request).await?;

  println!("Best bid: {:?}", book.bids[0]);
  println!("Best ask: {:?}", book.asks[0]);
  println!("Tick size: {:?}", book.tick_size);
  ```

  ```bash REST theme={null}
  curl "https://clob.polymarket.com/book?token_id=TOKEN_ID"
  ```
</CodeGroup>

### Response

```json  theme={null}
{
  "market": "0xbd31dc8a...",
  "asset_id": "52114319501245...",
  "timestamp": "2023-10-21T08:00:00Z",
  "bids": [
    { "price": "0.48", "size": "1000" },
    { "price": "0.47", "size": "2500" }
  ],
  "asks": [
    { "price": "0.52", "size": "800" },
    { "price": "0.53", "size": "1500" }
  ],
  "min_order_size": "5",
  "tick_size": "0.01",
  "neg_risk": false,
  "hash": "0xabc123..."
}
```

| Field            | Description                                         |
| ---------------- | --------------------------------------------------- |
| `market`         | Condition ID of the market                          |
| `asset_id`       | Token ID                                            |
| `bids`           | Buy orders sorted by price (highest first)          |
| `asks`           | Sell orders sorted by price (lowest first)          |
| `tick_size`      | Minimum price increment for this market             |
| `min_order_size` | Minimum order size for this market                  |
| `neg_risk`       | Whether this is a multi-outcome (neg risk) market   |
| `hash`           | Hash of the orderbook state — use to detect changes |

***

## Prices

Get the best available price for buying or selling a token:

<CodeGroup>
  ```typescript TypeScript theme={null}
  const buyPrice = await client.getPrice("TOKEN_ID", "BUY");
  console.log("Best ask:", buyPrice.price); // Price you'd pay to buy

  const sellPrice = await client.getPrice("TOKEN_ID", "SELL");
  console.log("Best bid:", sellPrice.price); // Price you'd receive to sell
  ```

  ```python Python theme={null}
  buy_price = client.get_price("TOKEN_ID", "BUY")
  print("Best ask:", buy_price["price"])

  sell_price = client.get_price("TOKEN_ID", "SELL")
  print("Best bid:", sell_price["price"])
  ```

  ```rust Rust theme={null}
  use polymarket_client_sdk::clob::types::{Side, request::PriceRequest};

  let token_id = "TOKEN_ID".parse()?;

  let buy_req = PriceRequest::builder().token_id(token_id).side(Side::Buy).build();
  let buy_price = client.price(&buy_req).await?;
  println!("Best ask: {}", buy_price.price);

  let sell_req = PriceRequest::builder().token_id(token_id).side(Side::Sell).build();
  let sell_price = client.price(&sell_req).await?;
  println!("Best bid: {}", sell_price.price);
  ```

  ```bash REST theme={null}
  # Best price for buying (lowest ask)
  curl "https://clob.polymarket.com/price?token_id=TOKEN_ID&side=BUY"

  # Best price for selling (highest bid)
  curl "https://clob.polymarket.com/price?token_id=TOKEN_ID&side=SELL"
  ```
</CodeGroup>

***

## Midpoints

The midpoint is the average of the best bid and best ask. This is the price displayed on Polymarket as the market's implied probability.

<CodeGroup>
  ```typescript TypeScript theme={null}
  const midpoint = await client.getMidpoint("TOKEN_ID");
  console.log("Midpoint:", midpoint.mid); // e.g., "0.50"
  ```

  ```python Python theme={null}
  midpoint = client.get_midpoint("TOKEN_ID")
  print("Midpoint:", midpoint["mid"])
  ```

  ```rust Rust theme={null}
  use polymarket_client_sdk::clob::types::request::MidpointRequest;

  let token_id = "TOKEN_ID".parse()?;
  let request = MidpointRequest::builder().token_id(token_id).build();
  let midpoint = client.midpoint(&request).await?;
  println!("Midpoint: {}", midpoint.mid);
  ```

  ```bash REST theme={null}
  curl "https://clob.polymarket.com/midpoint?token_id=TOKEN_ID"
  ```
</CodeGroup>

<Note>
  If the bid-ask spread is wider than \$0.10, Polymarket displays the last traded
  price instead of the midpoint.
</Note>

***

## Spreads

The spread is the difference between the best ask and the best bid. Tighter spreads indicate more liquid markets.

<CodeGroup>
  ```typescript TypeScript theme={null}
  const spread = await client.getSpread("TOKEN_ID");
  console.log("Spread:", spread.spread); // e.g., "0.04"
  ```

  ```python Python theme={null}
  spread = client.get_spread("TOKEN_ID")
  print("Spread:", spread["spread"])
  ```

  ```rust Rust theme={null}
  use polymarket_client_sdk::clob::types::request::SpreadRequest;

  let token_id = "TOKEN_ID".parse()?;
  let request = SpreadRequest::builder().token_id(token_id).build();
  let spread = client.spread(&request).await?;
  println!("Spread: {}", spread.spread);
  ```

  ```bash REST theme={null}
  # Spreads use POST for batch requests
  curl -X POST "https://clob.polymarket.com/spreads" \
    -H "Content-Type: application/json" \
    -d '[{"token_id": "TOKEN_ID"}]'
  ```
</CodeGroup>

***

## Price History

Fetch historical price data for a token over various time intervals:

<CodeGroup>
  ```typescript TypeScript theme={null}
  import { PriceHistoryInterval } from "@polymarket/clob-client";

  const history = await client.getPricesHistory({
    market: "TOKEN_ID", // Note: this param is named "market" but takes a token ID
    interval: PriceHistoryInterval.ONE_DAY,
    fidelity: 60, // Data points every 60 minutes
  });

  // Each entry: { t: timestamp, p: price }
  history.forEach((point) => {
    console.log(`${new Date(point.t * 1000).toISOString()}: ${point.p}`);
  });
  ```

  ```python Python theme={null}
  history = client.get_prices_history(
      market="TOKEN_ID",  # Note: this param is named "market" but takes a token ID
      interval="1d",
      fidelity=60,  # Data points every 60 minutes
  )

  for point in history:
      print(f"{point['t']}: {point['p']}")
  ```

  ```rust Rust theme={null}
  use polymarket_client_sdk::clob::types::{Interval, TimeRange, request::PriceHistoryRequest};

  let token_id = "TOKEN_ID".parse()?;
  let request = PriceHistoryRequest::builder()
      .market(token_id) // Note: this param is named "market" but takes a token ID
      .time_range(TimeRange::Interval { interval: Interval::OneDay })
      .fidelity(60) // Data points every 60 minutes
      .build();
  let history = client.price_history(&request).await?;

  for point in &history.history {
      println!("{}: {}", point.t, point.p);
  }
  ```

  ```bash REST theme={null}
  # By interval (relative to now)
  curl "https://clob.polymarket.com/prices-history?market=TOKEN_ID&interval=1d&fidelity=60"

  # By timestamp range
  curl "https://clob.polymarket.com/prices-history?market=TOKEN_ID&startTs=1697875200&endTs=1697961600"
  ```
</CodeGroup>

| Interval | Description        |
| -------- | ------------------ |
| `1h`     | Last hour          |
| `6h`     | Last 6 hours       |
| `1d`     | Last day           |
| `1w`     | Last week          |
| `1m`     | Last month         |
| `max`    | All available data |

<Note>
  `interval` is relative to the current time. Use `startTs` / `endTs` for
  absolute time ranges. They are mutually exclusive — don't combine them.
</Note>

***

## Estimate Fill Price

Calculate the effective price you'd pay for a market order of a given size, accounting for orderbook depth:

<CodeGroup>
  ```typescript TypeScript theme={null}
  import { Side, OrderType } from "@polymarket/clob-client";

  // What price would I pay to buy $500 worth?
  const price = await client.calculateMarketPrice(
    "TOKEN_ID",
    Side.BUY,
    500, // dollar amount
    OrderType.FOK,
  );

  console.log("Estimated fill price:", price);
  ```

  ```python Python theme={null}
  from py_clob_client.clob_types import OrderType

  price = client.calculate_market_price(
      token_id="TOKEN_ID",
      side="BUY",
      amount=500,
      order_type=OrderType.FOK,
  )

  print("Estimated fill price:", price)
  ```

  ```rust Rust theme={null}
  // The Rust SDK handles market price calculation automatically
  // inside the market_order() builder when no price is specified.
  // It walks the orderbook to determine the fill price for you.
  let order = client
      .market_order()
      .token_id("TOKEN_ID".parse()?)
      .amount(Amount::usdc(dec!(500))?)
      .side(Side::Buy)
      .order_type(OrderType::FOK)
      .build()
      .await?; // Price auto-calculated from orderbook depth
  ```
</CodeGroup>

This walks the orderbook to estimate slippage. Useful for sizing market orders before submitting them.

***

## Batch Requests

All orderbook queries have batch variants for fetching data across multiple tokens in a single request (up to 500 tokens):

| Single                | Batch                   | REST              |
| --------------------- | ----------------------- | ----------------- |
| `getOrderBook()`      | `getOrderBooks()`       | `POST /books`     |
| `getPrice()`          | `getPrices()`           | `POST /prices`    |
| `getMidpoint()`       | `getMidpoints()`        | `POST /midpoints` |
| `getSpread()`         | `getSpreads()`          | `POST /spreads`   |
| `getLastTradePrice()` | `getLastTradesPrices()` | —                 |

<Note>
  `BookParams` for batch orderbook requests accepts a `token_id` and an optional
  `side` parameter to filter by bid or ask side.
</Note>

<CodeGroup>
  ```typescript TypeScript theme={null}
  import { Side } from "@polymarket/clob-client";

  // Fetch prices for multiple tokens
  const prices = await client.getPrices([
    { token_id: "TOKEN_A", side: Side.BUY },
    { token_id: "TOKEN_B", side: Side.BUY },
  ]);
  // Returns: { "TOKEN_A": { "BUY": "0.52" }, "TOKEN_B": { "BUY": "0.74" } }
  ```

  ```python Python theme={null}
  prices = client.get_prices([
      {"token_id": "TOKEN_A", "side": "BUY"},
      {"token_id": "TOKEN_B", "side": "BUY"},
  ])
  ```

  ```rust Rust theme={null}
  use polymarket_client_sdk::clob::types::{Side, request::PriceRequest};

  let token_a = "TOKEN_A".parse()?;
  let token_b = "TOKEN_B".parse()?;
  let requests = vec![
      PriceRequest::builder().token_id(token_a).side(Side::Buy).build(),
      PriceRequest::builder().token_id(token_b).side(Side::Buy).build(),
  ];
  let prices = client.prices(&requests).await?;
  ```

  ```bash REST theme={null}
  curl -X POST "https://clob.polymarket.com/prices" \
    -H "Content-Type: application/json" \
    -d '[
      {"token_id": "TOKEN_A", "side": "BUY"},
      {"token_id": "TOKEN_B", "side": "BUY"}
    ]'
  ```
</CodeGroup>

***

## Last Trade Price

Get the price and side of the most recent trade for a token:

<CodeGroup>
  ```typescript TypeScript theme={null}
  const lastTrade = await client.getLastTradePrice("TOKEN_ID");
  console.log(lastTrade.price, lastTrade.side);
  // e.g., "0.52", "BUY"
  ```

  ```python Python theme={null}
  last_trade = client.get_last_trade_price("TOKEN_ID")
  print(last_trade["price"], last_trade["side"])
  ```

  ```rust Rust theme={null}
  use polymarket_client_sdk::clob::types::request::LastTradePriceRequest;

  let token_id = "TOKEN_ID".parse()?;
  let request = LastTradePriceRequest::builder().token_id(token_id).build();
  let last_trade = client.last_trade_price(&request).await?;
  println!("{} {:?}", last_trade.price, last_trade.side);
  ```
</CodeGroup>

***

## Real-Time Updates

For live orderbook data, use the WebSocket API instead of polling. The `market` channel streams orderbook changes, price updates, and trade events in real time.

### Connecting

```typescript  theme={null}
const ws = new WebSocket(
  "wss://ws-subscriptions-clob.polymarket.com/ws/market",
);

ws.onopen = () => {
  ws.send(
    JSON.stringify({
      type: "market",
      assets_ids: ["TOKEN_ID"],
      custom_feature_enabled: true, // enables best_bid_ask, new_market, market_resolved events
    }),
  );
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  switch (data.event_type) {
    case "book": // full orderbook snapshot
    case "price_change": // individual price level update
    case "last_trade_price": // new trade executed
    case "tick_size_change": // market tick size changed
    case "best_bid_ask": // top-of-book update (requires custom_feature_enabled)
    case "new_market": // new market created (requires custom_feature_enabled)
    case "market_resolved": // market resolved (requires custom_feature_enabled)
  }
};
```

### Dynamic Subscribe and Unsubscribe

After connecting, you can change your subscriptions without reconnecting:

```typescript  theme={null}
// Subscribe to additional tokens
ws.send(
  JSON.stringify({
    assets_ids: ["NEW_TOKEN_ID"],
    operation: "subscribe",
  }),
);

// Unsubscribe from tokens
ws.send(
  JSON.stringify({
    assets_ids: ["OLD_TOKEN_ID"],
    operation: "unsubscribe",
  }),
);
```

### Event Types

| Event              | Trigger                                      | Key Fields                                                             |
| ------------------ | -------------------------------------------- | ---------------------------------------------------------------------- |
| `book`             | On subscribe + when a trade affects the book | `bids[]`, `asks[]`, `hash`, `timestamp`                                |
| `price_change`     | New order placed or order cancelled          | `price_changes[]` with `price`, `size`, `side`, `best_bid`, `best_ask` |
| `last_trade_price` | Trade executed                               | `price`, `side`, `size`, `fee_rate_bps`                                |
| `tick_size_change` | Price hits >0.96 or \< 0.04                  | `old_tick_size`, `new_tick_size`                                       |
| `best_bid_ask`     | Top-of-book changes                          | `best_bid`, `best_ask`, `spread`                                       |
| `new_market`       | Market created                               | `question`, `assets_ids`, `outcomes`                                   |
| `market_resolved`  | Market resolved                              | `winning_asset_id`, `winning_outcome`                                  |

<Note>
  `best_bid_ask`, `new_market`, and `market_resolved` require
  `custom_feature_enabled: true` in your subscription message.
</Note>

<Warning>
  The `tick_size_change` event is critical for trading bots. If the tick size
  changes and you continue using the old tick size, your orders will be
  rejected.
</Warning>

***

## Next Steps

<CardGroup cols={2}>
  <Card title="Place Orders" icon="plus" href="/trading/orders/create">
    Create and submit orders using the orderbook data
  </Card>

  <Card title="Fetching Markets" icon="magnifying-glass" href="/market-data/fetching-markets">
    Find token IDs for markets you want to trade
  </Card>
</CardGroup>


Built with [Mintlify](https://mintlify.com).

> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# L2 Methods

> These methods require user API credentials (L2 headers). Use these for placing trades and managing your positions.

## Client Initialization

L2 methods require the client to initialize with a signer, signature type, API credentials, and funder address.

<Tabs>
  <Tab title="TypeScript">
    ```typescript  theme={null}
    import { ClobClient } from "@polymarket/clob-client";
    import { Wallet } from "ethers";

    const signer = new Wallet(process.env.PRIVATE_KEY);

    const apiCreds = {
      apiKey: process.env.API_KEY,
      secret: process.env.SECRET,
      passphrase: process.env.PASSPHRASE,
    };

    const client = new ClobClient(
      "https://clob.polymarket.com",
      137,
      signer,
      apiCreds,
      2, // GNOSIS_SAFE
      process.env.FUNDER_ADDRESS
    );

    // Ready to send authenticated requests
    const order = await client.postOrder(signedOrder);
    ```
  </Tab>

  <Tab title="Python">
    ```python  theme={null}
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    import os

    api_creds = ApiCreds(
        api_key=os.getenv("API_KEY"),
        api_secret=os.getenv("SECRET"),
        api_passphrase=os.getenv("PASSPHRASE")
    )

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=os.getenv("PRIVATE_KEY"),
        creds=api_creds,
        signature_type=2,  # GNOSIS_SAFE
        funder=os.getenv("FUNDER_ADDRESS")
    )

    # Ready to send authenticated requests
    order = client.post_order(signed_order)
    ```
  </Tab>
</Tabs>

***

## Order Creation and Management

***

### createAndPostOrder

Convenience method that creates, signs, and posts a limit order in a single call. Use when you want to buy or sell at a specific price.

```typescript Signature theme={null}
async createAndPostOrder(
  userOrder: UserOrder,
  options?: Partial<CreateOrderOptions>,
  orderType?: OrderType.GTC | OrderType.GTD, // Defaults to GTC
): Promise<OrderResponse>
```

**Params**

<ResponseField name="tokenID" type="string">
  The token ID of the outcome to trade.
</ResponseField>

<ResponseField name="price" type="number">
  The limit price for the order.
</ResponseField>

<ResponseField name="size" type="number">
  The size of the order.
</ResponseField>

<ResponseField name="side" type="Side">
  The side of the order (buy or sell).
</ResponseField>

<ResponseField name="feeRateBps" type="number">
  Optional fee rate in basis points.
</ResponseField>

<ResponseField name="nonce" type="number">
  Optional nonce for the order.
</ResponseField>

<ResponseField name="expiration" type="number">
  Optional expiration timestamp for the order.
</ResponseField>

<ResponseField name="taker" type="string">
  Optional taker address.
</ResponseField>

<ResponseField name="tickSize" type="TickSize">
  Tick size for the order. One of `"0.1"`, `"0.01"`, `"0.001"`, `"0.0001"`.
</ResponseField>

<ResponseField name="negRisk" type="boolean">
  Optional. Whether the market uses negative risk.
</ResponseField>

**Response**

<ResponseField name="success" type="boolean">
  Whether the order was successfully placed.
</ResponseField>

<ResponseField name="errorMsg" type="string">
  Error message if the order was not successful.
</ResponseField>

<ResponseField name="orderID" type="string">
  The ID of the placed order.
</ResponseField>

<ResponseField name="transactionsHashes" type="string[]">
  Array of transaction hashes associated with the order.
</ResponseField>

<ResponseField name="status" type="string">
  The current status of the order.
</ResponseField>

<ResponseField name="takingAmount" type="string">
  The amount being taken in the order.
</ResponseField>

<ResponseField name="makingAmount" type="string">
  The amount being made in the order.
</ResponseField>

***

### createAndPostMarketOrder

Convenience method that creates, signs, and posts a market order in a single call. Use when you want to buy or sell at the current market price.

```typescript Signature theme={null}
async createAndPostMarketOrder(
  userMarketOrder: UserMarketOrder,
  options?: Partial<CreateOrderOptions>,
  orderType?: OrderType.FOK | OrderType.FAK, // Defaults to FOK
): Promise<OrderResponse>
```

**Params**

<ResponseField name="tokenID" type="string">
  The token ID of the outcome to trade.
</ResponseField>

<ResponseField name="amount" type="number">
  The amount for the market order.
</ResponseField>

<ResponseField name="side" type="Side">
  The side of the order (buy or sell).
</ResponseField>

<ResponseField name="price" type="number">
  Optional price hint for the market order.
</ResponseField>

<ResponseField name="feeRateBps" type="number">
  Optional fee rate in basis points.
</ResponseField>

<ResponseField name="nonce" type="number">
  Optional nonce for the order.
</ResponseField>

<ResponseField name="taker" type="string">
  Optional taker address.
</ResponseField>

<ResponseField name="orderType" type="OrderType.FOK | OrderType.FAK">
  Optional order type override. Defaults to FOK.
</ResponseField>

**Response**

<ResponseField name="success" type="boolean">
  Whether the order was successfully placed.
</ResponseField>

<ResponseField name="errorMsg" type="string">
  Error message if the order was not successful.
</ResponseField>

<ResponseField name="orderID" type="string">
  The ID of the placed order.
</ResponseField>

<ResponseField name="transactionsHashes" type="string[]">
  Array of transaction hashes associated with the order.
</ResponseField>

<ResponseField name="status" type="string">
  The current status of the order.
</ResponseField>

<ResponseField name="takingAmount" type="string">
  The amount being taken in the order.
</ResponseField>

<ResponseField name="makingAmount" type="string">
  The amount being made in the order.
</ResponseField>

***

### postOrder

Posts a pre-signed order to the CLOB. Use with [`createOrder()`](/trading/clients/l1#createorder) or [`createMarketOrder()`](/trading/clients/l1#createmarketorder) from L1 methods.

```typescript Signature theme={null}
async postOrder(
  order: SignedOrder,
  orderType?: OrderType, // Defaults to GTC
  postOnly?: boolean,    // Defaults to false
): Promise<OrderResponse>
```

***

### postOrders

Posts up to 15 pre-signed orders in a single batch.

```typescript Signature theme={null}
async postOrders(
  args: PostOrdersArgs[],
): Promise<OrderResponse[]>
```

**Params**

<ResponseField name="order" type="SignedOrder">
  The pre-signed order to post.
</ResponseField>

<ResponseField name="orderType" type="OrderType">
  The order type (e.g. GTC, FOK, FAK).
</ResponseField>

<ResponseField name="postOnly" type="boolean">
  Optional. Whether to post the order as post-only. Defaults to false.
</ResponseField>

***

### cancelOrder

Cancels a single open order.

```typescript Signature theme={null}
async cancelOrder(orderID: string): Promise<CancelOrdersResponse>
```

**Response**

<ResponseField name="canceled" type="string[]">
  Array of order IDs that were successfully canceled.
</ResponseField>

<ResponseField name="not_canceled" type="Record<string, any>">
  Map of order IDs to reasons why they could not be canceled.
</ResponseField>

***

### cancelOrders

Cancels multiple orders in a single batch.

```typescript Signature theme={null}
async cancelOrders(orderIDs: string[]): Promise<CancelOrdersResponse>
```

***

### cancelAll

Cancels all open orders.

```typescript Signature theme={null}
async cancelAll(): Promise<CancelOrdersResponse>
```

***

### cancelMarketOrders

Cancels all open orders for a specific market.

```typescript Signature theme={null}
async cancelMarketOrders(
  payload: OrderMarketCancelParams
): Promise<CancelOrdersResponse>
```

**Params**

<ResponseField name="market" type="string">
  Optional. The market condition ID to cancel orders for.
</ResponseField>

<ResponseField name="asset_id" type="string">
  Optional. The token ID to cancel orders for.
</ResponseField>

***

## Order and Trade Queries

***

### getOrder

Get details for a specific order by ID.

```typescript Signature theme={null}
async getOrder(orderID: string): Promise<OpenOrder>
```

**Response**

<ResponseField name="id" type="string">
  The unique order ID.
</ResponseField>

<ResponseField name="status" type="string">
  The current status of the order.
</ResponseField>

<ResponseField name="owner" type="string">
  The API key of the order owner.
</ResponseField>

<ResponseField name="maker_address" type="string">
  The on-chain address of the order maker.
</ResponseField>

<ResponseField name="market" type="string">
  The market condition ID the order belongs to.
</ResponseField>

<ResponseField name="asset_id" type="string">
  The token ID the order is for.
</ResponseField>

<ResponseField name="side" type="string">
  The side of the order (BUY or SELL).
</ResponseField>

<ResponseField name="original_size" type="string">
  The original size of the order when it was placed.
</ResponseField>

<ResponseField name="size_matched" type="string">
  The amount of the order that has been matched so far.
</ResponseField>

<ResponseField name="price" type="string">
  The limit price of the order.
</ResponseField>

<ResponseField name="associate_trades" type="string[]">
  Array of trade IDs associated with this order.
</ResponseField>

<ResponseField name="outcome" type="string">
  The outcome label for the order's token.
</ResponseField>

<ResponseField name="created_at" type="number">
  Unix timestamp of when the order was created.
</ResponseField>

<ResponseField name="expiration" type="string">
  The expiration time of the order.
</ResponseField>

<ResponseField name="order_type" type="string">
  The order type (e.g. GTC, FOK, FAK, GTD).
</ResponseField>

***

### getOpenOrders

Get all your open orders.

```typescript Signature theme={null}
async getOpenOrders(
  params?: OpenOrderParams,
  only_first_page?: boolean,
): Promise<OpenOrder[]>
```

**Params**

<ResponseField name="id" type="string">
  Optional. Filter by order ID.
</ResponseField>

<ResponseField name="market" type="string">
  Optional. Filter by market condition ID.
</ResponseField>

<ResponseField name="asset_id" type="string">
  Optional. Filter by token ID.
</ResponseField>

***

### getTrades

Get your trade history (filled orders).

```typescript Signature theme={null}
async getTrades(
  params?: TradeParams,
  only_first_page?: boolean,
): Promise<Trade[]>
```

**Params**

<ResponseField name="id" type="string">
  Optional. Filter by trade ID.
</ResponseField>

<ResponseField name="maker_address" type="string">
  Optional. Filter by maker address.
</ResponseField>

<ResponseField name="market" type="string">
  Optional. Filter by market condition ID.
</ResponseField>

<ResponseField name="asset_id" type="string">
  Optional. Filter by token ID.
</ResponseField>

<ResponseField name="before" type="string">
  Optional. Return trades before this timestamp.
</ResponseField>

<ResponseField name="after" type="string">
  Optional. Return trades after this timestamp.
</ResponseField>

**Response**

<ResponseField name="id" type="string">
  The unique trade ID.
</ResponseField>

<ResponseField name="taker_order_id" type="string">
  The order ID of the taker side.
</ResponseField>

<ResponseField name="market" type="string">
  The market condition ID for the trade.
</ResponseField>

<ResponseField name="asset_id" type="string">
  The token ID for the trade.
</ResponseField>

<ResponseField name="side" type="Side">
  The side of the trade (BUY or SELL).
</ResponseField>

<ResponseField name="size" type="string">
  The size of the trade.
</ResponseField>

<ResponseField name="fee_rate_bps" type="string">
  The fee rate in basis points.
</ResponseField>

<ResponseField name="price" type="string">
  The price at which the trade was matched.
</ResponseField>

<ResponseField name="status" type="string">
  The current status of the trade.
</ResponseField>

<ResponseField name="match_time" type="string">
  The time at which the trade was matched.
</ResponseField>

<ResponseField name="last_update" type="string">
  The time of the last update to this trade.
</ResponseField>

<ResponseField name="outcome" type="string">
  The outcome label for the traded token.
</ResponseField>

<ResponseField name="bucket_index" type="number">
  The bucket index for the trade.
</ResponseField>

<ResponseField name="owner" type="string">
  The API key of the trade owner.
</ResponseField>

<ResponseField name="maker_address" type="string">
  The on-chain address of the maker.
</ResponseField>

<ResponseField name="maker_orders" type="MakerOrder[]">
  Array of maker order objects that participated in this trade. Each `MakerOrder` contains the following fields:
</ResponseField>

<ResponseField name="maker_orders[].order_id" type="string">
  The maker order ID.
</ResponseField>

<ResponseField name="maker_orders[].owner" type="string">
  The API key of the maker order owner.
</ResponseField>

<ResponseField name="maker_orders[].maker_address" type="string">
  The on-chain address of the maker order maker.
</ResponseField>

<ResponseField name="maker_orders[].matched_amount" type="string">
  The amount matched for this maker order.
</ResponseField>

<ResponseField name="maker_orders[].price" type="string">
  The price of the maker order.
</ResponseField>

<ResponseField name="maker_orders[].fee_rate_bps" type="string">
  The fee rate in basis points for the maker order.
</ResponseField>

<ResponseField name="maker_orders[].asset_id" type="string">
  The token ID for the maker order.
</ResponseField>

<ResponseField name="maker_orders[].outcome" type="string">
  The outcome label for the maker order's token.
</ResponseField>

<ResponseField name="maker_orders[].side" type="Side">
  The side of the maker order (BUY or SELL).
</ResponseField>

<ResponseField name="transaction_hash" type="string">
  The on-chain transaction hash for the trade.
</ResponseField>

<ResponseField name="trader_side" type="&#x22;TAKER&#x22; | &#x22;MAKER&#x22;">
  Whether the authenticated user is the taker or a maker in this trade.
</ResponseField>

***

### getTradesPaginated

Get trade history with pagination for large result sets.

```typescript Signature theme={null}
async getTradesPaginated(
  params?: TradeParams,
): Promise<TradesPaginatedResponse>
```

**Response**

<ResponseField name="trades" type="Trade[]">
  Array of trade objects for the current page.
</ResponseField>

<ResponseField name="limit" type="number">
  The maximum number of trades returned per page.
</ResponseField>

<ResponseField name="count" type="number">
  The total number of trades matching the query.
</ResponseField>

***

## Balance and Allowances

***

### getBalanceAllowance

Get your balance and allowance for specific tokens.

```typescript Signature theme={null}
async getBalanceAllowance(
  params?: BalanceAllowanceParams
): Promise<BalanceAllowanceResponse>
```

**Params**

<ResponseField name="asset_type" type="AssetType">
  The type of asset to query. One of `"COLLATERAL"` or `"CONDITIONAL"`.
</ResponseField>

<ResponseField name="token_id" type="string">
  Optional. The token ID to query (required when `asset_type` is `CONDITIONAL`).
</ResponseField>

**Response**

<ResponseField name="balance" type="string">
  The current balance for the specified asset.
</ResponseField>

<ResponseField name="allowance" type="string">
  The current allowance for the specified asset.
</ResponseField>

***

### updateBalanceAllowance

Updates the cached balance and allowance for specific tokens.

```typescript Signature theme={null}
async updateBalanceAllowance(
  params?: BalanceAllowanceParams
): Promise<void>
```

***

## API Key Management

***

### getApiKeys

Get all API keys associated with your account.

```typescript Signature theme={null}
async getApiKeys(): Promise<ApiKeysResponse>
```

**Response**

<ResponseField name="apiKeys" type="ApiKeyCreds[]">
  Array of API key credential objects associated with the account.
</ResponseField>

***

### deleteApiKey

Deletes (revokes) the currently authenticated API key.

```typescript Signature theme={null}
async deleteApiKey(): Promise<any>
```

***

## Notifications

***

### getNotifications

Retrieves all event notifications for the authenticated user. Records are automatically removed after 48 hours.

```typescript Signature theme={null}
async getNotifications(): Promise<Notification[]>
```

**Response**

<ResponseField name="id" type="number">
  Unique notification ID.
</ResponseField>

<ResponseField name="owner" type="string">
  The user's API key, or an empty string for global notifications.
</ResponseField>

<ResponseField name="payload" type="any">
  Type-specific payload data for the notification.
</ResponseField>

<ResponseField name="timestamp" type="number">
  Optional Unix timestamp of when the notification was created.
</ResponseField>

<ResponseField name="type" type="number">
  Notification type (see below).
</ResponseField>

| Name               | Value | Description                              |
| ------------------ | ----- | ---------------------------------------- |
| Order Cancellation | `1`   | User's order was canceled                |
| Order Fill         | `2`   | User's order was filled (maker or taker) |
| Market Resolved    | `4`   | Market was resolved                      |

***

### dropNotifications

Mark notifications as read/dismissed.

```typescript Signature theme={null}
async dropNotifications(params?: DropNotificationParams): Promise<void>
```

**Params**

<ResponseField name="ids" type="string[]">
  Array of notification IDs to dismiss.
</ResponseField>

***

## See Also

<CardGroup cols={2}>
  <Card title="Authentication" icon="shield" href="/api-reference/authentication">
    Deep dive into L1 and L2 authentication.
  </Card>

  <Card title="L1 Methods" icon="key" href="/trading/clients/l1">
    Sign orders and derive API credentials with your private key.
  </Card>

  <Card title="Public Methods" icon="globe" href="/trading/clients/public">
    Read market data and orderbooks without auth.
  </Card>

  <Card title="WebSocket" icon="bolt" href="/market-data/websocket/overview">
    Real-time market data streaming.
  </Card>
</CardGroup>


Built with [Mintlify](https://mintlify.com).

> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Fees

> Understanding trading fees on Polymarket

Polymarket charges a small taker fee on certain markets. These fees fund the [Maker Rebates Program](/market-makers/maker-rebates), which redistributes fees daily to market makers to incentivize deeper liquidity and tighter spreads.

**Geopolitical and world events markets are fee-free.** Polymarket does not charge fees or profit from trading activity on these markets. There are also no Polymarket fees to deposit or withdraw USDC (though intermediaries like Coinbase or MoonPay may charge their own fees).

<Note>
  Fees apply only to markets deployed on or after the activation date. Pre-existing markets are unaffected. Markets with fees enabled have `feesEnabled` set to `true` on the market object.
</Note>

***

## Current Fee Structure

The following fee parameters are currently live. **New fee parameters will take effect on March 30, 2026** — see [Upcoming Fee Structure](#upcoming-fee-structure) below.

Currently, only **Crypto** and **Sports** markets have taker fees enabled.

| Category | Fee Rate | Exponent | Maker Rebate | Peak Effective Rate |
| -------- | -------- | -------- | ------------ | ------------------- |
| Crypto   | 0.25     | 2        | 20%          | 1.56%               |
| Sports   | 0.0175   | 1        | 25%          | 0.44%               |

<Frame>
  <div className="p-3 bg-white rounded-xl">
    <iframe title="Fee Curves (Current)" aria-label="Line chart" id="datawrapper-chart-Z7OnS" src="https://datawrapper.dwcdn.net/Z7OnS/" scrolling="no" frameborder="0" width={700} style={{ width: "0", minWidth: "100% !important", border: "none" }} height="450" data-external="1" />
  </div>
</Frame>

<Tabs>
  <Tab title="Crypto">
    | Price  | Trade Value | Fee (USDC) | Effective Rate |
    | ------ | ----------- | ---------- | -------------- |
    | \$0.01 | \$1         | \$0.00     | 0.00%          |
    | \$0.05 | \$5         | \$0.003    | 0.06%          |
    | \$0.10 | \$10        | \$0.02     | 0.20%          |
    | \$0.15 | \$15        | \$0.06     | 0.41%          |
    | \$0.20 | \$20        | \$0.13     | 0.64%          |
    | \$0.25 | \$25        | \$0.22     | 0.88%          |
    | \$0.30 | \$30        | \$0.33     | 1.10%          |
    | \$0.35 | \$35        | \$0.45     | 1.29%          |
    | \$0.40 | \$40        | \$0.58     | 1.44%          |
    | \$0.45 | \$45        | \$0.69     | 1.53%          |
    | \$0.50 | \$50        | \$0.78     | **1.56%**      |
    | \$0.55 | \$55        | \$0.84     | 1.53%          |
    | \$0.60 | \$60        | \$0.86     | 1.44%          |
    | \$0.65 | \$65        | \$0.84     | 1.29%          |
    | \$0.70 | \$70        | \$0.77     | 1.10%          |
    | \$0.75 | \$75        | \$0.66     | 0.88%          |
    | \$0.80 | \$80        | \$0.51     | 0.64%          |
    | \$0.85 | \$85        | \$0.35     | 0.41%          |
    | \$0.90 | \$90        | \$0.18     | 0.20%          |
    | \$0.95 | \$95        | \$0.05     | 0.06%          |
    | \$0.99 | \$99        | \$0.00     | 0.00%          |
  </Tab>

  <Tab title="Sports">
    | Price  | Trade Value | Fee (USDC) | Effective Rate |
    | ------ | ----------- | ---------- | -------------- |
    | \$0.01 | \$1         | \$0.00     | 0.02%          |
    | \$0.05 | \$5         | \$0.00     | 0.08%          |
    | \$0.10 | \$10        | \$0.02     | 0.16%          |
    | \$0.15 | \$15        | \$0.03     | 0.22%          |
    | \$0.20 | \$20        | \$0.06     | 0.28%          |
    | \$0.25 | \$25        | \$0.08     | 0.33%          |
    | \$0.30 | \$30        | \$0.11     | 0.37%          |
    | \$0.35 | \$35        | \$0.14     | 0.40%          |
    | \$0.40 | \$40        | \$0.17     | 0.42%          |
    | \$0.45 | \$45        | \$0.19     | 0.43%          |
    | \$0.50 | \$50        | \$0.22     | **0.44%**      |
    | \$0.55 | \$55        | \$0.24     | 0.43%          |
    | \$0.60 | \$60        | \$0.25     | 0.42%          |
    | \$0.65 | \$65        | \$0.26     | 0.40%          |
    | \$0.70 | \$70        | \$0.26     | 0.37%          |
    | \$0.75 | \$75        | \$0.25     | 0.33%          |
    | \$0.80 | \$80        | \$0.22     | 0.28%          |
    | \$0.85 | \$85        | \$0.19     | 0.22%          |
    | \$0.90 | \$90        | \$0.14     | 0.16%          |
    | \$0.95 | \$95        | \$0.08     | 0.08%          |
    | \$0.99 | \$99        | \$0.02     | 0.02%          |
  </Tab>
</Tabs>

***

## Upcoming Fee Structure

**Effective March 30, 2026**, fee parameters are expanding to cover more market categories with updated rates.

Fees are calculated using the following formula:

```text  theme={null}
fee = C × p × feeRate × (p × (1 - p))^exponent
```

Where **C** = number of shares traded and **p** = price of the shares. The fee parameters differ by market category:

| Category        | Fee Rate | Exponent | Maker Rebate | Peak Effective Rate |
| --------------- | -------- | -------- | ------------ | ------------------- |
| Crypto          | 0.072    | 1        | 20%          | 1.80%               |
| Sports          | 0.03     | 1        | 25%          | 0.75%               |
| Finance         | 0.04     | 1        | 50%          | 1.00%               |
| Politics        | 0.04     | 1        | 25%          | 1.00%               |
| Economics       | 0.03     | 0.5      | 25%          | 1.50%               |
| Culture         | 0.05     | 1        | 25%          | 1.25%               |
| Weather         | 0.025    | 0.5      | 25%          | 1.25%               |
| Other / General | 0.2      | 2        | 25%          | 1.25%               |
| Mentions        | 0.25     | 2        | 25%          | 1.56%               |
| Tech            | 0.04     | 1        | 25%          | 1.00%               |

Taker fees are calculated in USDC and vary based on the share price. However, fees are collected in shares on buy orders and USDC on sell orders. The effective rate **peaks at 50%** probability and decreases symmetrically toward the extremes.

<Frame>
  <div className="p-3 bg-white rounded-xl">
    <iframe title="Fee Curves" aria-label="Line chart" id="datawrapper-chart-qTzMH" src="https://datawrapper.dwcdn.net/qTzMH/1/" scrolling="no" frameborder="0" width={700} style={{ width: "0", minWidth: "100% !important", border: "none" }} height="450" data-external="1" />
  </div>
</Frame>

### Fee Tables (100 Shares)

<Tabs>
  <Tab title="Crypto">
    | Price  | Trade Value | Fee (USDC) | Effective Rate |
    | ------ | ----------- | ---------- | -------------- |
    | \$0.01 | \$1         | \$0.00     | 0.07%          |
    | \$0.05 | \$5         | \$0.02     | 0.34%          |
    | \$0.10 | \$10        | \$0.06     | 0.65%          |
    | \$0.15 | \$15        | \$0.14     | 0.92%          |
    | \$0.20 | \$20        | \$0.23     | 1.15%          |
    | \$0.25 | \$25        | \$0.34     | 1.35%          |
    | \$0.30 | \$30        | \$0.45     | 1.51%          |
    | \$0.35 | \$35        | \$0.57     | 1.64%          |
    | \$0.40 | \$40        | \$0.69     | 1.73%          |
    | \$0.45 | \$45        | \$0.80     | 1.78%          |
    | \$0.50 | \$50        | \$0.90     | **1.80%**      |
    | \$0.55 | \$55        | \$0.98     | 1.78%          |
    | \$0.60 | \$60        | \$1.04     | 1.73%          |
    | \$0.65 | \$65        | \$1.06     | 1.64%          |
    | \$0.70 | \$70        | \$1.06     | 1.51%          |
    | \$0.75 | \$75        | \$1.01     | 1.35%          |
    | \$0.80 | \$80        | \$0.92     | 1.15%          |
    | \$0.85 | \$85        | \$0.78     | 0.92%          |
    | \$0.90 | \$90        | \$0.58     | 0.65%          |
    | \$0.95 | \$95        | \$0.32     | 0.34%          |
    | \$0.99 | \$99        | \$0.07     | 0.07%          |

    The maximum effective fee rate is **1.80%** at 50% probability. Fees decrease symmetrically toward both extremes.
  </Tab>

  <Tab title="Sports">
    | Price  | Trade Value | Fee (USDC) | Effective Rate |
    | ------ | ----------- | ---------- | -------------- |
    | \$0.01 | \$1         | \$0.00     | 0.03%          |
    | \$0.05 | \$5         | \$0.01     | 0.14%          |
    | \$0.10 | \$10        | \$0.03     | 0.27%          |
    | \$0.15 | \$15        | \$0.06     | 0.38%          |
    | \$0.20 | \$20        | \$0.10     | 0.48%          |
    | \$0.25 | \$25        | \$0.14     | 0.56%          |
    | \$0.30 | \$30        | \$0.19     | 0.63%          |
    | \$0.35 | \$35        | \$0.24     | 0.68%          |
    | \$0.40 | \$40        | \$0.29     | 0.72%          |
    | \$0.45 | \$45        | \$0.33     | 0.74%          |
    | \$0.50 | \$50        | \$0.38     | **0.75%**      |
    | \$0.55 | \$55        | \$0.41     | 0.74%          |
    | \$0.60 | \$60        | \$0.43     | 0.72%          |
    | \$0.65 | \$65        | \$0.44     | 0.68%          |
    | \$0.70 | \$70        | \$0.44     | 0.63%          |
    | \$0.75 | \$75        | \$0.42     | 0.56%          |
    | \$0.80 | \$80        | \$0.38     | 0.48%          |
    | \$0.85 | \$85        | \$0.33     | 0.38%          |
    | \$0.90 | \$90        | \$0.24     | 0.27%          |
    | \$0.95 | \$95        | \$0.14     | 0.14%          |
    | \$0.99 | \$99        | \$0.03     | 0.03%          |

    The maximum effective fee rate is **0.75%** at 50% probability. Fees decrease symmetrically toward both extremes.
  </Tab>

  <Tab title="Finance / Politics / Tech">
    | Price  | Trade Value | Fee (USDC) | Effective Rate |
    | ------ | ----------- | ---------- | -------------- |
    | \$0.01 | \$1         | \$0.00     | 0.04%          |
    | \$0.05 | \$5         | \$0.01     | 0.19%          |
    | \$0.10 | \$10        | \$0.04     | 0.36%          |
    | \$0.15 | \$15        | \$0.08     | 0.51%          |
    | \$0.20 | \$20        | \$0.13     | 0.64%          |
    | \$0.25 | \$25        | \$0.19     | 0.75%          |
    | \$0.30 | \$30        | \$0.25     | 0.84%          |
    | \$0.35 | \$35        | \$0.32     | 0.91%          |
    | \$0.40 | \$40        | \$0.38     | 0.96%          |
    | \$0.45 | \$45        | \$0.45     | 0.99%          |
    | \$0.50 | \$50        | \$0.50     | **1.00%**      |
    | \$0.55 | \$55        | \$0.54     | 0.99%          |
    | \$0.60 | \$60        | \$0.58     | 0.96%          |
    | \$0.65 | \$65        | \$0.59     | 0.91%          |
    | \$0.70 | \$70        | \$0.59     | 0.84%          |
    | \$0.75 | \$75        | \$0.56     | 0.75%          |
    | \$0.80 | \$80        | \$0.51     | 0.64%          |
    | \$0.85 | \$85        | \$0.43     | 0.51%          |
    | \$0.90 | \$90        | \$0.32     | 0.36%          |
    | \$0.95 | \$95        | \$0.18     | 0.19%          |
    | \$0.99 | \$99        | \$0.04     | 0.04%          |

    The maximum effective fee rate is **1.00%** at 50% probability. Fees decrease symmetrically toward both extremes.
  </Tab>

  <Tab title="Culture">
    | Price  | Trade Value | Fee (USDC) | Effective Rate |
    | ------ | ----------- | ---------- | -------------- |
    | \$0.01 | \$1         | \$0.00     | 0.05%          |
    | \$0.05 | \$5         | \$0.01     | 0.24%          |
    | \$0.10 | \$10        | \$0.05     | 0.45%          |
    | \$0.15 | \$15        | \$0.10     | 0.64%          |
    | \$0.20 | \$20        | \$0.16     | 0.80%          |
    | \$0.25 | \$25        | \$0.23     | 0.94%          |
    | \$0.30 | \$30        | \$0.32     | 1.05%          |
    | \$0.35 | \$35        | \$0.40     | 1.14%          |
    | \$0.40 | \$40        | \$0.48     | 1.20%          |
    | \$0.45 | \$45        | \$0.56     | 1.24%          |
    | \$0.50 | \$50        | \$0.62     | **1.25%**      |
    | \$0.55 | \$55        | \$0.68     | 1.24%          |
    | \$0.60 | \$60        | \$0.72     | 1.20%          |
    | \$0.65 | \$65        | \$0.74     | 1.14%          |
    | \$0.70 | \$70        | \$0.74     | 1.05%          |
    | \$0.75 | \$75        | \$0.70     | 0.94%          |
    | \$0.80 | \$80        | \$0.64     | 0.80%          |
    | \$0.85 | \$85        | \$0.54     | 0.64%          |
    | \$0.90 | \$90        | \$0.40     | 0.45%          |
    | \$0.95 | \$95        | \$0.23     | 0.24%          |
    | \$0.99 | \$99        | \$0.05     | 0.05%          |

    The maximum effective fee rate is **1.25%** at 50% probability. Fees decrease symmetrically toward both extremes.
  </Tab>

  <Tab title="Economics">
    | Price  | Trade Value | Fee (USDC) | Effective Rate |
    | ------ | ----------- | ---------- | -------------- |
    | \$0.01 | \$1         | \$0.00     | 0.30%          |
    | \$0.05 | \$5         | \$0.03     | 0.65%          |
    | \$0.10 | \$10        | \$0.09     | 0.90%          |
    | \$0.15 | \$15        | \$0.16     | 1.07%          |
    | \$0.20 | \$20        | \$0.24     | 1.20%          |
    | \$0.25 | \$25        | \$0.32     | 1.30%          |
    | \$0.30 | \$30        | \$0.41     | 1.37%          |
    | \$0.35 | \$35        | \$0.50     | 1.43%          |
    | \$0.40 | \$40        | \$0.59     | 1.47%          |
    | \$0.45 | \$45        | \$0.67     | 1.49%          |
    | \$0.50 | \$50        | \$0.75     | **1.50%**      |
    | \$0.55 | \$55        | \$0.82     | 1.49%          |
    | \$0.60 | \$60        | \$0.88     | 1.47%          |
    | \$0.65 | \$65        | \$0.93     | 1.43%          |
    | \$0.70 | \$70        | \$0.96     | 1.37%          |
    | \$0.75 | \$75        | \$0.97     | 1.30%          |
    | \$0.80 | \$80        | \$0.96     | 1.20%          |
    | \$0.85 | \$85        | \$0.91     | 1.07%          |
    | \$0.90 | \$90        | \$0.81     | 0.90%          |
    | \$0.95 | \$95        | \$0.62     | 0.65%          |
    | \$0.99 | \$99        | \$0.30     | 0.30%          |

    The maximum effective fee rate is **1.50%** at 50% probability. Fees decrease symmetrically toward both extremes.
  </Tab>

  <Tab title="Weather">
    | Price  | Trade Value | Fee (USDC) | Effective Rate |
    | ------ | ----------- | ---------- | -------------- |
    | \$0.01 | \$1         | \$0.00     | 0.25%          |
    | \$0.05 | \$5         | \$0.03     | 0.54%          |
    | \$0.10 | \$10        | \$0.08     | 0.75%          |
    | \$0.15 | \$15        | \$0.13     | 0.89%          |
    | \$0.20 | \$20        | \$0.20     | 1.00%          |
    | \$0.25 | \$25        | \$0.27     | 1.08%          |
    | \$0.30 | \$30        | \$0.34     | 1.15%          |
    | \$0.35 | \$35        | \$0.42     | 1.19%          |
    | \$0.40 | \$40        | \$0.49     | 1.22%          |
    | \$0.45 | \$45        | \$0.56     | 1.24%          |
    | \$0.50 | \$50        | \$0.62     | **1.25%**      |
    | \$0.55 | \$55        | \$0.68     | 1.24%          |
    | \$0.60 | \$60        | \$0.73     | 1.22%          |
    | \$0.65 | \$65        | \$0.78     | 1.19%          |
    | \$0.70 | \$70        | \$0.80     | 1.15%          |
    | \$0.75 | \$75        | \$0.81     | 1.08%          |
    | \$0.80 | \$80        | \$0.80     | 1.00%          |
    | \$0.85 | \$85        | \$0.76     | 0.89%          |
    | \$0.90 | \$90        | \$0.67     | 0.75%          |
    | \$0.95 | \$95        | \$0.52     | 0.54%          |
    | \$0.99 | \$99        | \$0.25     | 0.25%          |

    The maximum effective fee rate is **1.25%** at 50% probability. Fees decrease symmetrically toward both extremes.
  </Tab>

  <Tab title="Other / General">
    | Price  | Trade Value | Fee (USDC) | Effective Rate |
    | ------ | ----------- | ---------- | -------------- |
    | \$0.01 | \$1         | \$0.00     | 0.00%          |
    | \$0.05 | \$5         | \$0.00     | 0.05%          |
    | \$0.10 | \$10        | \$0.02     | 0.16%          |
    | \$0.15 | \$15        | \$0.05     | 0.33%          |
    | \$0.20 | \$20        | \$0.10     | 0.51%          |
    | \$0.25 | \$25        | \$0.18     | 0.70%          |
    | \$0.30 | \$30        | \$0.26     | 0.88%          |
    | \$0.35 | \$35        | \$0.36     | 1.04%          |
    | \$0.40 | \$40        | \$0.46     | 1.15%          |
    | \$0.45 | \$45        | \$0.55     | 1.23%          |
    | \$0.50 | \$50        | \$0.62     | **1.25%**      |
    | \$0.55 | \$55        | \$0.67     | 1.23%          |
    | \$0.60 | \$60        | \$0.69     | 1.15%          |
    | \$0.65 | \$65        | \$0.67     | 1.04%          |
    | \$0.70 | \$70        | \$0.62     | 0.88%          |
    | \$0.75 | \$75        | \$0.53     | 0.70%          |
    | \$0.80 | \$80        | \$0.41     | 0.51%          |
    | \$0.85 | \$85        | \$0.28     | 0.33%          |
    | \$0.90 | \$90        | \$0.15     | 0.16%          |
    | \$0.95 | \$95        | \$0.04     | 0.05%          |
    | \$0.99 | \$99        | \$0.00     | 0.00%          |

    The maximum effective fee rate is **1.25%** at 50% probability. Fees decrease symmetrically toward both extremes.
  </Tab>

  <Tab title="Mentions">
    | Price  | Trade Value | Fee (USDC) | Effective Rate |
    | ------ | ----------- | ---------- | -------------- |
    | \$0.01 | \$1         | \$0.00     | 0.00%          |
    | \$0.05 | \$5         | \$0.00     | 0.06%          |
    | \$0.10 | \$10        | \$0.02     | 0.20%          |
    | \$0.15 | \$15        | \$0.06     | 0.41%          |
    | \$0.20 | \$20        | \$0.13     | 0.64%          |
    | \$0.25 | \$25        | \$0.22     | 0.88%          |
    | \$0.30 | \$30        | \$0.33     | 1.10%          |
    | \$0.35 | \$35        | \$0.45     | 1.29%          |
    | \$0.40 | \$40        | \$0.58     | 1.44%          |
    | \$0.45 | \$45        | \$0.69     | 1.53%          |
    | \$0.50 | \$50        | \$0.78     | **1.56%**      |
    | \$0.55 | \$55        | \$0.84     | 1.53%          |
    | \$0.60 | \$60        | \$0.86     | 1.44%          |
    | \$0.65 | \$65        | \$0.84     | 1.29%          |
    | \$0.70 | \$70        | \$0.77     | 1.10%          |
    | \$0.75 | \$75        | \$0.66     | 0.88%          |
    | \$0.80 | \$80        | \$0.51     | 0.64%          |
    | \$0.85 | \$85        | \$0.35     | 0.41%          |
    | \$0.90 | \$90        | \$0.18     | 0.20%          |
    | \$0.95 | \$95        | \$0.05     | 0.06%          |
    | \$0.99 | \$99        | \$0.00     | 0.00%          |

    The maximum effective fee rate is **1.56%** at 50% probability. Fees decrease symmetrically toward both extremes.
  </Tab>
</Tabs>

### Fee Precision

Fees are rounded to 4 decimal places. The smallest fee charged is **0.0001 USDC**. Anything smaller rounds to zero, so very small trades near the extremes may incur no fee at all.

***

## Identifying Fee-Enabled Markets

Markets with fees have `feesEnabled` set to `true` on the market object. You can also query the fee-rate endpoint to check any specific market. See the [API Reference](/api-reference/introduction) for full endpoint documentation.

```bash  theme={null}
GET https://clob.polymarket.com/fee-rate?token_id={token_id}
```

***

## Fee Handling for API Users

### Using the SDK

The official CLOB clients **automatically handle fees** for you — they fetch the fee rate and include it in the signed order payload.

<CardGroup cols={3}>
  <Card title="TypeScript" icon="js" href="https://github.com/Polymarket/clob-client">
    npm install @polymarket/clob-client\@latest
  </Card>

  <Card title="Python" icon="python" href="https://github.com/Polymarket/py-clob-client">
    pip install --upgrade py-clob-client
  </Card>

  <Card title="Rust" icon="rust" href="https://github.com/Polymarket/rs-clob-client">
    cargo add polymarket-client-sdk
  </Card>
</CardGroup>

**What the client does automatically:**

1. Fetches the fee rate for the market's token ID
2. Includes `feeRateBps` in the order structure
3. Signs the order with the fee rate included

**You don't need to do anything extra.** Your orders will work on fee-enabled markets.

### Using the REST API

If you're calling the REST API directly or building your own order signing, you must manually include the fee rate in your signed order payload.

**Step 1:** Fetch the fee rate for the token ID before creating your order:

```bash  theme={null}
GET https://clob.polymarket.com/fee-rate?token_id={token_id}
```

See the [fee-rate API Reference](/api-reference/introduction) for full response details. Fee-enabled markets return a non-zero value; fee-free markets return `0`.

**Step 2:** Add the `feeRateBps` field to your order object. This value is part of the signed payload — the CLOB validates your signature against it.

```json  theme={null}
{
  "salt": "12345",
  "maker": "0x...",
  "signer": "0x...",
  "taker": "0x...",
  "tokenId": "71321045679252212594626385532706912750332728571942532289631379312455583992563",
  "makerAmount": "50000000",
  "takerAmount": "100000000",
  "expiration": "0",
  "nonce": "0",
  "feeRateBps": "1000",
  "side": "0",
  "signatureType": 2,
  "signature": "0x..."
}
```

**Step 3:** Sign and submit:

1. Include `feeRateBps` in the order object **before signing**
2. Sign the complete order
3. POST to the order endpoint

<Note>
  Always fetch `fee_rate_bps` dynamically — do not hardcode. The fee rate varies
  by market type and may change over time. You only need to pass `feeRateBps`.
</Note>

***

## Next Steps

<CardGroup cols={2}>
  <Card title="Maker Rebates Program" icon="coins" href="/market-makers/maker-rebates">
    Learn how taker fees fund daily USDC rebates for liquidity providers.
  </Card>

  <Card title="Place Orders" icon="plus" href="/trading/quickstart">
    Start placing orders on Polymarket.
  </Card>
</CardGroup>


Built with [Mintlify](https://mintlify.com).

> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Overview

> Real-time market data and trading updates via WebSocket

Polymarket provides WebSocket channels for near real-time streaming of orderbook data, trades, and personal order activity. There are four available channels: `market`, `user`, `sports`, and `RTDS` (Real-Time Data Socket).

## Channels

| Channel                             | Endpoint                                               | Auth     |
| ----------------------------------- | ------------------------------------------------------ | -------- |
| Market                              | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | No       |
| User                                | `wss://ws-subscriptions-clob.polymarket.com/ws/user`   | Yes      |
| Sports                              | `wss://sports-api.polymarket.com/ws`                   | No       |
| [RTDS](/market-data/websocket/rtds) | `wss://ws-live-data.polymarket.com`                    | Optional |

### Market Channel

| Type               | Description             | Custom Feature |
| ------------------ | ----------------------- | -------------- |
| `book`             | Full orderbook snapshot | No             |
| `price_change`     | Price level updates     | No             |
| `tick_size_change` | Tick size changes       | No             |
| `last_trade_price` | Trade executions        | No             |
| `best_bid_ask`     | Best prices update      | Yes            |
| `new_market`       | New market created      | Yes            |
| `market_resolved`  | Market resolution       | Yes            |

Types marked "Custom Feature" require `custom_feature_enabled: true` in your subscription.

### User Channel

| Type    | Description                                   |
| ------- | --------------------------------------------- |
| `trade` | Trade lifecycle updates (MATCHED → CONFIRMED) |
| `order` | Order placements, updates, and cancellations  |

### Sports

| Type           | Description                           |
| -------------- | ------------------------------------- |
| `sport_result` | Live game scores, periods, and status |

## Subscribing

Send a subscription message after connecting to specify which data you want to receive.

### Market Channel

```json  theme={null}
{
  "assets_ids": [
    "21742633143463906290569050155826241533067272736897614950488156847949938836455",
    "48331043336612883890938759509493159234755048973500640148014422747788308965732"
  ],
  "type": "market",
  "custom_feature_enabled": true
}
```

| Field                    | Type      | Description                                                       |
| ------------------------ | --------- | ----------------------------------------------------------------- |
| `assets_ids`             | string\[] | Token IDs to subscribe to                                         |
| `type`                   | string    | Channel identifier                                                |
| `custom_feature_enabled` | boolean   | Enable `best_bid_ask`, `new_market`, and `market_resolved` events |

### User Channel

```json  theme={null}
{
  "auth": {
    "apiKey": "your-api-key",
    "secret": "your-api-secret",
    "passphrase": "your-passphrase"
  },
  "markets": ["0x1234...condition_id"],
  "type": "user"
}
```

<Note>
  The `auth` fields (`apiKey`, `secret`, `passphrase`) are **only required for
  the user channel**. For the market channel, these fields are optional and can
  be omitted.
</Note>

| Field     | Type      | Description                                        |
| --------- | --------- | -------------------------------------------------- |
| `auth`    | object    | API credentials (`apiKey`, `secret`, `passphrase`) |
| `markets` | string\[] | Condition IDs to receive events for                |
| `type`    | string    | Channel identifier                                 |

<Note>
  The user channel subscribes by **condition IDs** (market identifiers), not
  asset IDs. Each market has one condition ID but two asset IDs (Yes and No
  tokens).
</Note>

### Sports Channel

No subscription message required. Connect and start receiving data for all active sports events.

## Dynamic Subscription

Modify subscriptions without reconnecting.

### Subscribe to more assets

```json  theme={null}
{
  "assets_ids": ["new_asset_id_1", "new_asset_id_2"],
  "operation": "subscribe",
  "custom_feature_enabled": true
}
```

### Unsubscribe from assets

```json  theme={null}
{
  "assets_ids": ["asset_id_to_remove"],
  "operation": "unsubscribe"
}
```

For the user channel, use `markets` instead of `assets_ids`:

```json  theme={null}
{
  "markets": ["0x1234...condition_id"],
  "operation": "subscribe"
}
```

## Heartbeats

### Market and User Channels

Send `PING` every 10 seconds. The server responds with `PONG`.

```
PING
```

### Sports Channel

The server sends `ping` every 5 seconds. Respond with `pong` within 10 seconds.

```
pong
```

<Warning>
  If you don't respond to the server's ping within 10 seconds, the connection
  will be closed.
</Warning>

## Troubleshooting

<Accordion title="Connection closes immediately after opening">
  Send a valid subscription message immediately after connecting. The server may
  close connections that don't subscribe within a timeout period.
</Accordion>

<Accordion title="Connection drops after about 10 seconds">
  You're not sending heartbeats. Send `PING` every 10 seconds for market/user
  channels, or respond to server `ping` with `pong` for the sports channel.
</Accordion>

<Accordion title="Not receiving any messages">
  1. Verify your asset IDs or condition IDs are correct 2. Check that the
     markets are active (not resolved) 3. Set `custom_feature_enabled: true` if
     expecting `best_bid_ask`, `new_market`, or `market_resolved` events
</Accordion>

<Accordion title="Authentication failed - user channel">
  Verify your API credentials are correct and haven't expired.
</Accordion>


Built with [Mintlify](https://mintlify.com).

> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Overview

> Real-time market data and trading updates via WebSocket

Polymarket provides WebSocket channels for near real-time streaming of orderbook data, trades, and personal order activity. There are four available channels: `market`, `user`, `sports`, and `RTDS` (Real-Time Data Socket).

## Channels

| Channel                             | Endpoint                                               | Auth     |
| ----------------------------------- | ------------------------------------------------------ | -------- |
| Market                              | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | No       |
| User                                | `wss://ws-subscriptions-clob.polymarket.com/ws/user`   | Yes      |
| Sports                              | `wss://sports-api.polymarket.com/ws`                   | No       |
| [RTDS](/market-data/websocket/rtds) | `wss://ws-live-data.polymarket.com`                    | Optional |

### Market Channel

| Type               | Description             | Custom Feature |
| ------------------ | ----------------------- | -------------- |
| `book`             | Full orderbook snapshot | No             |
| `price_change`     | Price level updates     | No             |
| `tick_size_change` | Tick size changes       | No             |
| `last_trade_price` | Trade executions        | No             |
| `best_bid_ask`     | Best prices update      | Yes            |
| `new_market`       | New market created      | Yes            |
| `market_resolved`  | Market resolution       | Yes            |

Types marked "Custom Feature" require `custom_feature_enabled: true` in your subscription.

### User Channel

| Type    | Description                                   |
| ------- | --------------------------------------------- |
| `trade` | Trade lifecycle updates (MATCHED → CONFIRMED) |
| `order` | Order placements, updates, and cancellations  |

### Sports

| Type           | Description                           |
| -------------- | ------------------------------------- |
| `sport_result` | Live game scores, periods, and status |

## Subscribing

Send a subscription message after connecting to specify which data you want to receive.

### Market Channel

```json  theme={null}
{
  "assets_ids": [
    "21742633143463906290569050155826241533067272736897614950488156847949938836455",
    "48331043336612883890938759509493159234755048973500640148014422747788308965732"
  ],
  "type": "market",
  "custom_feature_enabled": true
}
```

| Field                    | Type      | Description                                                       |
| ------------------------ | --------- | ----------------------------------------------------------------- |
| `assets_ids`             | string\[] | Token IDs to subscribe to                                         |
| `type`                   | string    | Channel identifier                                                |
| `custom_feature_enabled` | boolean   | Enable `best_bid_ask`, `new_market`, and `market_resolved` events |

### User Channel

```json  theme={null}
{
  "auth": {
    "apiKey": "your-api-key",
    "secret": "your-api-secret",
    "passphrase": "your-passphrase"
  },
  "markets": ["0x1234...condition_id"],
  "type": "user"
}
```

<Note>
  The `auth` fields (`apiKey`, `secret`, `passphrase`) are **only required for
  the user channel**. For the market channel, these fields are optional and can
  be omitted.
</Note>

| Field     | Type      | Description                                        |
| --------- | --------- | -------------------------------------------------- |
| `auth`    | object    | API credentials (`apiKey`, `secret`, `passphrase`) |
| `markets` | string\[] | Condition IDs to receive events for                |
| `type`    | string    | Channel identifier                                 |

<Note>
  The user channel subscribes by **condition IDs** (market identifiers), not
  asset IDs. Each market has one condition ID but two asset IDs (Yes and No
  tokens).
</Note>

### Sports Channel

No subscription message required. Connect and start receiving data for all active sports events.

## Dynamic Subscription

Modify subscriptions without reconnecting.

### Subscribe to more assets

```json  theme={null}
{
  "assets_ids": ["new_asset_id_1", "new_asset_id_2"],
  "operation": "subscribe",
  "custom_feature_enabled": true
}
```

### Unsubscribe from assets

```json  theme={null}
{
  "assets_ids": ["asset_id_to_remove"],
  "operation": "unsubscribe"
}
```

For the user channel, use `markets` instead of `assets_ids`:

```json  theme={null}
{
  "markets": ["0x1234...condition_id"],
  "operation": "subscribe"
}
```

## Heartbeats

### Market and User Channels

Send `PING` every 10 seconds. The server responds with `PONG`.

```
PING
```

### Sports Channel

The server sends `ping` every 5 seconds. Respond with `pong` within 10 seconds.

```
pong
```

<Warning>
  If you don't respond to the server's ping within 10 seconds, the connection
  will be closed.
</Warning>

## Troubleshooting

<Accordion title="Connection closes immediately after opening">
  Send a valid subscription message immediately after connecting. The server may
  close connections that don't subscribe within a timeout period.
</Accordion>

<Accordion title="Connection drops after about 10 seconds">
  You're not sending heartbeats. Send `PING` every 10 seconds for market/user
  channels, or respond to server `ping` with `pong` for the sports channel.
</Accordion>

<Accordion title="Not receiving any messages">
  1. Verify your asset IDs or condition IDs are correct 2. Check that the
     markets are active (not resolved) 3. Set `custom_feature_enabled: true` if
     expecting `best_bid_ask`, `new_market`, or `market_resolved` events
</Accordion>

<Accordion title="Authentication failed - user channel">
  Verify your API credentials are correct and haven't expired.
</Accordion>


Built with [Mintlify](https://mintlify.com).

> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Overview

> Real-time market data and trading updates via WebSocket

Polymarket provides WebSocket channels for near real-time streaming of orderbook data, trades, and personal order activity. There are four available channels: `market`, `user`, `sports`, and `RTDS` (Real-Time Data Socket).

## Channels

| Channel                             | Endpoint                                               | Auth     |
| ----------------------------------- | ------------------------------------------------------ | -------- |
| Market                              | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | No       |
| User                                | `wss://ws-subscriptions-clob.polymarket.com/ws/user`   | Yes      |
| Sports                              | `wss://sports-api.polymarket.com/ws`                   | No       |
| [RTDS](/market-data/websocket/rtds) | `wss://ws-live-data.polymarket.com`                    | Optional |

### Market Channel

| Type               | Description             | Custom Feature |
| ------------------ | ----------------------- | -------------- |
| `book`             | Full orderbook snapshot | No             |
| `price_change`     | Price level updates     | No             |
| `tick_size_change` | Tick size changes       | No             |
| `last_trade_price` | Trade executions        | No             |
| `best_bid_ask`     | Best prices update      | Yes            |
| `new_market`       | New market created      | Yes            |
| `market_resolved`  | Market resolution       | Yes            |

Types marked "Custom Feature" require `custom_feature_enabled: true` in your subscription.

### User Channel

| Type    | Description                                   |
| ------- | --------------------------------------------- |
| `trade` | Trade lifecycle updates (MATCHED → CONFIRMED) |
| `order` | Order placements, updates, and cancellations  |

### Sports

| Type           | Description                           |
| -------------- | ------------------------------------- |
| `sport_result` | Live game scores, periods, and status |

## Subscribing

Send a subscription message after connecting to specify which data you want to receive.

### Market Channel

```json  theme={null}
{
  "assets_ids": [
    "21742633143463906290569050155826241533067272736897614950488156847949938836455",
    "48331043336612883890938759509493159234755048973500640148014422747788308965732"
  ],
  "type": "market",
  "custom_feature_enabled": true
}
```

| Field                    | Type      | Description                                                       |
| ------------------------ | --------- | ----------------------------------------------------------------- |
| `assets_ids`             | string\[] | Token IDs to subscribe to                                         |
| `type`                   | string    | Channel identifier                                                |
| `custom_feature_enabled` | boolean   | Enable `best_bid_ask`, `new_market`, and `market_resolved` events |

### User Channel

```json  theme={null}
{
  "auth": {
    "apiKey": "your-api-key",
    "secret": "your-api-secret",
    "passphrase": "your-passphrase"
  },
  "markets": ["0x1234...condition_id"],
  "type": "user"
}
```

<Note>
  The `auth` fields (`apiKey`, `secret`, `passphrase`) are **only required for
  the user channel**. For the market channel, these fields are optional and can
  be omitted.
</Note>

| Field     | Type      | Description                                        |
| --------- | --------- | -------------------------------------------------- |
| `auth`    | object    | API credentials (`apiKey`, `secret`, `passphrase`) |
| `markets` | string\[] | Condition IDs to receive events for                |
| `type`    | string    | Channel identifier                                 |

<Note>
  The user channel subscribes by **condition IDs** (market identifiers), not
  asset IDs. Each market has one condition ID but two asset IDs (Yes and No
  tokens).
</Note>

### Sports Channel

No subscription message required. Connect and start receiving data for all active sports events.

## Dynamic Subscription

Modify subscriptions without reconnecting.

### Subscribe to more assets

```json  theme={null}
{
  "assets_ids": ["new_asset_id_1", "new_asset_id_2"],
  "operation": "subscribe",
  "custom_feature_enabled": true
}
```

### Unsubscribe from assets

```json  theme={null}
{
  "assets_ids": ["asset_id_to_remove"],
  "operation": "unsubscribe"
}
```

For the user channel, use `markets` instead of `assets_ids`:

```json  theme={null}
{
  "markets": ["0x1234...condition_id"],
  "operation": "subscribe"
}
```

## Heartbeats

### Market and User Channels

Send `PING` every 10 seconds. The server responds with `PONG`.

```
PING
```

### Sports Channel

The server sends `ping` every 5 seconds. Respond with `pong` within 10 seconds.

```
pong
```

<Warning>
  If you don't respond to the server's ping within 10 seconds, the connection
  will be closed.
</Warning>

## Troubleshooting

<Accordion title="Connection closes immediately after opening">
  Send a valid subscription message immediately after connecting. The server may
  close connections that don't subscribe within a timeout period.
</Accordion>

<Accordion title="Connection drops after about 10 seconds">
  You're not sending heartbeats. Send `PING` every 10 seconds for market/user
  channels, or respond to server `ping` with `pong` for the sports channel.
</Accordion>

<Accordion title="Not receiving any messages">
  1. Verify your asset IDs or condition IDs are correct 2. Check that the
     markets are active (not resolved) 3. Set `custom_feature_enabled: true` if
     expecting `best_bid_ask`, `new_market`, or `market_resolved` events
</Accordion>

<Accordion title="Authentication failed - user channel">
  Verify your API credentials are correct and haven't expired.
</Accordion>


Built with [Mintlify](https://mintlify.com).

> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Create Order

> Build, sign, and submit orders

All orders on Polymarket are expressed as **limit orders**. Market orders are supported by submitting a limit order with a marketable price — your order executes immediately at the best available price on the book.

<Info>
  The SDK handles EIP-712 signing and submission for you. If you prefer the REST
  API directly, see [Authentication](/api-reference/authentication) for constructing the
  required headers and the [API Reference](/api-reference/introduction) for full endpoint
  documentation including the raw order object fields and request/response schemas.
</Info>

***

## Order Types

| Type    | Behavior                                                             | Use Case                        |
| ------- | -------------------------------------------------------------------- | ------------------------------- |
| **GTC** | Good-Til-Cancelled — rests on the book until filled or cancelled     | Default for limit orders        |
| **GTD** | Good-Til-Date — active until a specified expiration time             | Auto-expire before known events |
| **FOK** | Fill-Or-Kill — must fill immediately and entirely, or cancel         | All-or-nothing market orders    |
| **FAK** | Fill-And-Kill — fills what's available immediately, cancels the rest | Partial-fill market orders      |

* **GTC** and **GTD** are limit order types — they rest on the book at your specified price.
* **FOK** and **FAK** are market order types — they execute against resting liquidity immediately.
  * **BUY**: specify the dollar amount you want to spend
  * **SELL**: specify the number of shares you want to sell

***

## Limit Orders

The simplest way to place a limit order — create, sign, and submit in one call:

<CodeGroup>
  ```typescript TypeScript theme={null}
  import { ClobClient, Side, OrderType } from "@polymarket/clob-client";

  const response = await client.createAndPostOrder(
    {
      tokenID: "TOKEN_ID",
      price: 0.5,
      size: 10,
      side: Side.BUY,
    },
    {
      tickSize: "0.01",
      negRisk: false,
    },
    OrderType.GTC,
  );

  console.log("Order ID:", response.orderID);
  console.log("Status:", response.status);
  ```

  ```python Python theme={null}
  from py_clob_client.clob_types import OrderArgs, OrderType
  from py_clob_client.order_builder.constants import BUY

  response = client.create_and_post_order(
      OrderArgs(
          token_id="TOKEN_ID",
          price=0.50,
          size=10,
          side=BUY,
      ),
      options={
          "tick_size": "0.01",
          "neg_risk": False,
      },
      order_type=OrderType.GTC
  )

  print("Order ID:", response["orderID"])
  print("Status:", response["status"])
  ```

  ```rust Rust theme={null}
  use polymarket_client_sdk::clob::types::Side;
  use polymarket_client_sdk::types::dec;

  let token_id = "TOKEN_ID".parse()?;
  let order = client
      .limit_order()
      .token_id(token_id)
      .price(dec!(0.50))
      .size(dec!(10))
      .side(Side::Buy)
      .build()
      .await?;
  let signed = client.sign(&signer, order).await?;
  let response = client.post_order(signed).await?;

  println!("Order ID: {}", response.order_id);
  println!("Status: {:?}", response.status);
  ```
</CodeGroup>

### Two-Step Sign Then Submit

For more control, you can separate signing from submission. This is useful for batch orders or custom submission logic:

<CodeGroup>
  ```typescript TypeScript theme={null}
  // Step 1: Create and sign locally
  const signedOrder = await client.createOrder(
    {
      tokenID: "TOKEN_ID",
      price: 0.5,
      size: 10,
      side: Side.BUY,
    },
    { tickSize: "0.01", negRisk: false },
  );

  // Step 2: Submit to the CLOB
  const response = await client.postOrder(signedOrder, OrderType.GTC);
  ```

  ```python Python theme={null}
  # Step 1: Create and sign locally
  signed_order = client.create_order(
      OrderArgs(
          token_id="TOKEN_ID",
          price=0.50,
          size=10,
          side=BUY,
      ),
      options={
          "tick_size": "0.01",
          "neg_risk": False,
      }
  )

  # Step 2: Submit to the CLOB
  response = client.post_order(signed_order, OrderType.GTC)
  ```

  ```rust Rust theme={null}
  // Step 1: Create order (auto-fetches tick size, neg risk, fee rate)
  let order = client
      .limit_order()
      .token_id("TOKEN_ID".parse()?)
      .price(dec!(0.50))
      .size(dec!(10))
      .side(Side::Buy)
      .build()
      .await?;

  // Step 2: Sign and submit separately
  let signed = client.sign(&signer, order).await?;
  let response = client.post_order(signed).await?;
  ```
</CodeGroup>

***

## GTD Orders

GTD orders auto-expire at a specified time. Useful for quoting around known events.

<CodeGroup>
  ```typescript TypeScript theme={null}
  // Expire in 1 hour (+ 60s security threshold buffer)
  const expiration = Math.floor(Date.now() / 1000) + 60 + 3600;

  const response = await client.createAndPostOrder(
    {
      tokenID: "TOKEN_ID",
      price: 0.5,
      size: 10,
      side: Side.BUY,
      expiration,
    },
    { tickSize: "0.01", negRisk: false },
    OrderType.GTD,
  );
  ```

  ```python Python theme={null}
  import time

  # Expire in 1 hour (+ 60s security threshold buffer)
  expiration = int(time.time()) + 60 + 3600

  response = client.create_and_post_order(
      OrderArgs(
          token_id="TOKEN_ID",
          price=0.50,
          size=10,
          side=BUY,
          expiration=expiration,
      ),
      options={
          "tick_size": "0.01",
          "neg_risk": False,
      },
      order_type=OrderType.GTD
  )
  ```

  ```rust Rust theme={null}
  use chrono::{TimeDelta, Utc};
  use polymarket_client_sdk::clob::types::OrderType;

  let order = client
      .limit_order()
      .token_id("TOKEN_ID".parse()?)
      .price(dec!(0.50))
      .size(dec!(10))
      .side(Side::Buy)
      .order_type(OrderType::GTD)
      .expiration(Utc::now() + TimeDelta::hours(1))
      .build()
      .await?;
  let signed = client.sign(&signer, order).await?;
  let response = client.post_order(signed).await?;
  ```
</CodeGroup>

<Note>
  There is a security threshold of one minute on GTD expiration. To set an
  effective lifetime of N seconds, use `now + 60 + N`. For example, for a
  30-second effective lifetime, set the expiration to `now + 60 + 30`.
</Note>

***

## Market Orders

Market orders execute immediately against resting liquidity using FOK or FAK types:

<CodeGroup>
  ```typescript TypeScript theme={null}
  import { Side, OrderType } from "@polymarket/clob-client";

  // FOK BUY: spend exactly $100 or cancel entirely
  const buyOrder = await client.createMarketOrder(
    {
      tokenID: "TOKEN_ID",
      side: Side.BUY,
      amount: 100, // dollar amount
      price: 0.5, // worst-price limit (slippage protection)
    },
    { tickSize: "0.01", negRisk: false },
  );
  await client.postOrder(buyOrder, OrderType.FOK);

  // FOK SELL: sell exactly 200 shares or cancel entirely
  const sellOrder = await client.createMarketOrder(
    {
      tokenID: "TOKEN_ID",
      side: Side.SELL,
      amount: 200, // number of shares
      price: 0.45, // worst-price limit (slippage protection)
    },
    { tickSize: "0.01", negRisk: false },
  );
  await client.postOrder(sellOrder, OrderType.FOK);
  ```

  ```python Python theme={null}
  from py_clob_client.order_builder.constants import BUY, SELL
  from py_clob_client.clob_types import OrderType

  # FOK BUY: spend exactly $100 or cancel entirely
  buy_order = client.create_market_order(
      token_id="TOKEN_ID",
      side=BUY,
      amount=100,  # dollar amount
      price=0.50,  # worst-price limit (slippage protection)
      options={"tick_size": "0.01", "neg_risk": False},
  )
  client.post_order(buy_order, OrderType.FOK)

  # FOK SELL: sell exactly 200 shares or cancel entirely
  sell_order = client.create_market_order(
      token_id="TOKEN_ID",
      side=SELL,
      amount=200,  # number of shares
      price=0.45,  # worst-price limit (slippage protection)
      options={"tick_size": "0.01", "neg_risk": False},
  )
  client.post_order(sell_order, OrderType.FOK)
  ```

  ```rust Rust theme={null}
  use polymarket_client_sdk::clob::types::{Amount, OrderType, Side};

  let token_id = "TOKEN_ID".parse()?;

  // FOK BUY: spend exactly $100 or cancel entirely
  let buy = client
      .market_order()
      .token_id(token_id)
      .amount(Amount::usdc(dec!(100))?)
      .price(dec!(0.50)) // worst-price limit (slippage protection)
      .side(Side::Buy)
      .order_type(OrderType::FOK)
      .build()
      .await?;
  let signed = client.sign(&signer, buy).await?;
  client.post_order(signed).await?;

  // FOK SELL: sell exactly 200 shares or cancel entirely
  let sell = client
      .market_order()
      .token_id(token_id)
      .amount(Amount::shares(dec!(200))?)
      .price(dec!(0.45)) // worst-price limit (slippage protection)
      .side(Side::Sell)
      .order_type(OrderType::FOK)
      .build()
      .await?;
  let signed = client.sign(&signer, sell).await?;
  client.post_order(signed).await?;
  ```
</CodeGroup>

* **FOK** — fill entirely or cancel the whole order
* **FAK** — fill what's available, cancel the rest

The `price` field on market orders acts as a **worst-price limit** (slippage protection), not a target execution price.

### One-Step Market Order

For convenience, `createAndPostMarketOrder` handles creation, signing, and submission in one call:

<CodeGroup>
  ```typescript TypeScript theme={null}
  const response = await client.createAndPostMarketOrder(
    {
      tokenID: "TOKEN_ID",
      side: Side.BUY,
      amount: 100,
      price: 0.5,
    },
    { tickSize: "0.01", negRisk: false },
    OrderType.FOK,
  );
  ```

  ```python Python theme={null}
  response = client.create_and_post_market_order(
      token_id="TOKEN_ID",
      side=BUY,
      amount=100,
      price=0.50,
      options={"tick_size": "0.01", "neg_risk": False},
      order_type=OrderType.FOK,
  )
  ```

  ```rust Rust theme={null}
  let order = client
      .market_order()
      .token_id("TOKEN_ID".parse()?)
      .amount(Amount::usdc(dec!(100))?)
      .price(dec!(0.50))
      .side(Side::Buy)
      .order_type(OrderType::FOK)
      .build()
      .await?;
  let signed = client.sign(&signer, order).await?;
  let response = client.post_order(signed).await?;
  ```
</CodeGroup>

***

## Post-Only Orders

Post-only orders guarantee you're always the maker. If the order would match immediately (cross the spread), it's rejected instead of executed.

<CodeGroup>
  ```typescript TypeScript theme={null}
  const response = await client.postOrder(signedOrder, OrderType.GTC, true);
  ```

  ```python Python theme={null}
  response = client.post_order(signed_order, OrderType.GTC, post_only=True)
  ```

  ```rust Rust theme={null}
  let order = client
      .limit_order()
      .token_id("TOKEN_ID".parse()?)
      .price(dec!(0.50))
      .size(dec!(10))
      .side(Side::Buy)
      .post_only(true)
      .build()
      .await?;
  let signed = client.sign(&signer, order).await?;
  let response = client.post_order(signed).await?;
  ```
</CodeGroup>

* Only works with **GTC** and **GTD** order types
* Rejected if combined with FOK or FAK

***

## Batch Orders

Place up to **15 orders** in a single request:

<CodeGroup>
  ```typescript TypeScript theme={null}
  import { OrderType, Side, PostOrdersArgs } from "@polymarket/clob-client";

  const orders: PostOrdersArgs[] = [
    {
      order: await client.createOrder(
        {
          tokenID: "TOKEN_ID",
          price: 0.48,
          side: Side.BUY,
          size: 500,
        },
        { tickSize: "0.01", negRisk: false },
      ),
      orderType: OrderType.GTC,
    },
    {
      order: await client.createOrder(
        {
          tokenID: "TOKEN_ID",
          price: 0.52,
          side: Side.SELL,
          size: 500,
        },
        { tickSize: "0.01", negRisk: false },
      ),
      orderType: OrderType.GTC,
    },
  ];

  const response = await client.postOrders(orders);
  ```

  ```python Python theme={null}
  from py_clob_client.clob_types import OrderArgs, OrderType, PostOrdersArgs
  from py_clob_client.order_builder.constants import BUY, SELL

  response = client.post_orders([
      PostOrdersArgs(
          order=client.create_order(OrderArgs(
              price=0.48,
              size=500,
              side=BUY,
              token_id="TOKEN_ID",
          ), options={"tick_size": "0.01", "neg_risk": False}),
          orderType=OrderType.GTC,
      ),
      PostOrdersArgs(
          order=client.create_order(OrderArgs(
              price=0.52,
              size=500,
              side=SELL,
              token_id="TOKEN_ID",
          ), options={"tick_size": "0.01", "neg_risk": False}),
          orderType=OrderType.GTC,
      ),
  ])
  ```

  ```rust Rust theme={null}
  let token_id = "TOKEN_ID".parse()?;

  let bid = client
      .limit_order()
      .token_id(token_id)
      .price(dec!(0.48))
      .size(dec!(500))
      .side(Side::Buy)
      .build()
      .await?;
  let ask = client
      .limit_order()
      .token_id(token_id)
      .price(dec!(0.52))
      .size(dec!(500))
      .side(Side::Sell)
      .build()
      .await?;

  let signed_bid = client.sign(&signer, bid).await?;
  let signed_ask = client.sign(&signer, ask).await?;
  let response = client.post_orders(vec![signed_bid, signed_ask]).await?;
  ```
</CodeGroup>

***

## Order Options

Every order requires two market-specific options: `tickSize` and `negRisk`. For details on signature types (`0` = EOA, `1` = POLY\_PROXY, `2` = GNOSIS\_SAFE), see [Authentication](/api-reference/authentication#signature-types-and-funder).

### Tick Sizes

Your order price must conform to the market's tick size, or the order is rejected.

| Tick Size | Precision  | Example Prices         |
| --------- | ---------- | ---------------------- |
| `0.1`     | 1 decimal  | 0.1, 0.2, 0.5          |
| `0.01`    | 2 decimals | 0.01, 0.50, 0.99       |
| `0.001`   | 3 decimals | 0.001, 0.500, 0.999    |
| `0.0001`  | 4 decimals | 0.0001, 0.5000, 0.9999 |

<CodeGroup>
  ```typescript TypeScript theme={null}
  const tickSize = await client.getTickSize("TOKEN_ID");
  ```

  ```python Python theme={null}
  tick_size = client.get_tick_size("TOKEN_ID")
  ```

  ```rust Rust theme={null}
  let token_id = "TOKEN_ID".parse()?;
  let tick_size = client.tick_size(token_id).await?;
  ```
</CodeGroup>

### Negative Risk

Multi-outcome events (3+ outcomes) use the Neg Risk CTF Exchange. Pass `negRisk: true` for these markets.

<CodeGroup>
  ```typescript TypeScript theme={null}
  const isNegRisk = await client.getNegRisk("TOKEN_ID");
  ```

  ```python Python theme={null}
  is_neg_risk = client.get_neg_risk("TOKEN_ID")
  ```

  ```rust Rust theme={null}
  let token_id = "TOKEN_ID".parse()?;
  let is_neg_risk = client.neg_risk(token_id).await?;
  ```
</CodeGroup>

<Tip>
  Both values are also available on the market object: `minimum_tick_size` and
  `neg_risk`. In Rust, the order builder auto-fetches both — you don't need to look them up manually.
</Tip>

***

## Prerequisites

Before placing an order, your funder address must have approved the Exchange contract to spend the relevant tokens:

* **BUY orders**: USDC.e allowance >= spending amount
* **SELL orders**: conditional token allowance >= selling amount

Order size is limited by your available balance minus amounts reserved by existing open orders:

$$
\text{maxOrderSize} = \text{balance} - \sum(\text{openOrderSize} - \text{filledAmount})
$$

<Warning>
  Orders are continuously monitored for validity — balances, allowances, and
  onchain cancellations are tracked in real time. Any maker caught intentionally
  abusing these checks will be blacklisted.
</Warning>

### Advanced Parameters

These optional fields can be passed in the `UserOrder` object for fine-grained control:

| Parameter    | Type   | Description                                     |
| ------------ | ------ | ----------------------------------------------- |
| `feeRateBps` | number | Fee rate in basis points (default: market rate) |
| `nonce`      | number | Custom nonce for order uniqueness               |
| `taker`      | string | Restrict the order to a specific taker address  |

### Sports Markets

Sports markets have additional behaviors:

* Outstanding limit orders are **automatically cancelled** once the game begins, clearing the entire order book at the official start time
* Marketable orders have a **3-second placement delay** before matching
* Game start times can shift — monitor your orders closely, as they may not be cleared if the start time changes unexpectedly

***

## Response

A successful order placement returns:

```json  theme={null}
{
  "success": true,
  "errorMsg": "",
  "orderID": "0xabc123...",
  "takingAmount": "",
  "makingAmount": "",
  "status": "live",
  "transactionsHashes": [],
  "tradeIDs": []
}
```

### Statuses

| Status      | Description                                                 |
| ----------- | ----------------------------------------------------------- |
| `live`      | Order resting on the book                                   |
| `matched`   | Order matched immediately with a resting order              |
| `delayed`   | Marketable order subject to a matching delay                |
| `unmatched` | Marketable but failed to delay — placement still successful |

### Error Messages

| Error                              | Description                                     |
| ---------------------------------- | ----------------------------------------------- |
| `INVALID_ORDER_MIN_TICK_SIZE`      | Price doesn't conform to the market's tick size |
| `INVALID_ORDER_MIN_SIZE`           | Order size below the minimum threshold          |
| `INVALID_ORDER_DUPLICATED`         | Identical order already placed                  |
| `INVALID_ORDER_NOT_ENOUGH_BALANCE` | Insufficient balance or allowance               |
| `INVALID_ORDER_EXPIRATION`         | Expiration timestamp is in the past             |
| `INVALID_POST_ONLY_ORDER_TYPE`     | Post-only used with FOK/FAK                     |
| `INVALID_POST_ONLY_ORDER`          | Post-only order would cross the book            |
| `FOK_ORDER_NOT_FILLED_ERROR`       | FOK order couldn't be fully filled              |
| `INVALID_ORDER_ERROR`              | System error inserting the order                |
| `EXECUTION_ERROR`                  | System error executing the trade                |
| `ORDER_DELAYED`                    | Order match delayed due to market conditions    |
| `DELAYING_ORDER_ERROR`             | System error while delaying the order           |
| `MARKET_NOT_READY`                 | Market not yet accepting orders                 |

***

## Heartbeat

The heartbeat endpoint maintains session liveness. If a valid heartbeat is not received within **10 seconds** (with a 5-second buffer), **all open orders are cancelled**.

<CodeGroup>
  ```typescript TypeScript theme={null}
  let heartbeatId = "";
  setInterval(async () => {
    const resp = await client.postHeartbeat(heartbeatId);
    heartbeatId = resp.heartbeat_id;
  }, 5000);
  ```

  ```python Python theme={null}
  import time

  heartbeat_id = ""
  while True:
      resp = client.post_heartbeat(heartbeat_id)
      heartbeat_id = resp["heartbeat_id"]
      time.sleep(5)
  ```

  ```rust Rust theme={null}
  // With the `heartbeats` feature, the Rust SDK can auto-send heartbeats
  // in a background task — no manual loop needed:
  Client::start_heartbeats(&mut client)?;
  // ... your trading logic ...
  client.stop_heartbeats().await?;

  // Or send manually:
  let resp = client.post_heartbeat(None).await?; // None for first call
  let resp = client.post_heartbeat(Some(resp.heartbeat_id)).await?;
  ```
</CodeGroup>

* Include the most recent `heartbeat_id` in each request. Use an empty string for the first request.
* If you send an expired ID, the server responds with `400` and the correct ID. Update and retry.

***

## Next Steps

<CardGroup cols={2}>
  <Card title="Cancel Orders" icon="xmark" href="/trading/orders/cancel">
    Cancel single, multiple, or all open orders
  </Card>

  <Card title="Order Attribution" icon="tag" href="/trading/orders/attribution">
    Attribute orders to your builder account for volume credit
  </Card>
</CardGroup>


Built with [Mintlify](https://mintlify.com).