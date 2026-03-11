"""
Submit a real test order using the Python SDK to verify POLY_PROXY mode works.
Uses the CLOB API to find tokens with active orderbooks.

Usage: source .env && python test_order.py
"""
import os, json, requests

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, ApiCreds, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY

try:
    from py_clob_client.order_builder.constants import POLY_PROXY
except ImportError:
    POLY_PROXY = 1

host = "https://clob.polymarket.com"
chain_id = 137
private_key = os.environ["POLY_PRIVATE_KEY"]
proxy_address = os.environ["POLY_PROXY_ADDRESS"]
api_key = os.environ["POLY_API_KEY"]
api_secret = os.environ["POLY_API_SECRET"]
api_passphrase = os.environ["POLY_API_PASSPHRASE"]

creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
client = ClobClient(host, chain_id=chain_id, key=private_key, creds=creds,
                    funder=proxy_address, signature_type=POLY_PROXY)

print(f"EOA:   {client.get_address()}")
print(f"Proxy: {proxy_address}")

# Find markets with active CLOB orderbooks
print("\nFinding markets with active orderbooks...")
resp = requests.get("https://gamma-api.polymarket.com/events?active=true&closed=false&limit=10")
events = resp.json()

# Try each token until we find one with an active orderbook on the CLOB
for ev in events:
    neg_risk = ev.get("negRisk", False)
    for mkt in ev.get("markets", []):
        tokens = mkt.get("clobTokenIds", [])
        if not tokens:
            continue
        token_id = tokens[0]
        market_name = mkt["question"][:60]

        # Verify this token has an orderbook on the CLOB
        try:
            book = requests.get(f"{host}/book?token_id={token_id}", timeout=5).json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if not bids and not asks:
                continue
            best_bid = float(bids[0]["price"]) if bids else 0
            best_ask = float(asks[0]["price"]) if asks else 1
        except:
            continue

        print(f"\nMarket:  {market_name}")
        print(f"Token:   {token_id[:40]}...")
        print(f"NegRisk: {neg_risk}")
        print(f"Book:    bid={best_bid:.2f} ask={best_ask:.2f}")

        # Place order far from the market (won't fill)
        price = 0.02
        print(f"\nCreating order (BUY @ ${price}, 5 shares)...")
        try:
            signed_order = client.create_order(
                OrderArgs(token_id=token_id, price=price, size=5, side=BUY),
                options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=neg_risk),
            )

            order_dict = signed_order.dict()
            print(f"  signatureType: {order_dict.get('signatureType')}")
            print(f"  maker:  {order_dict.get('maker')}")
            print(f"  signer: {order_dict.get('signer')}")
            print(f"  feeRateBps: {order_dict.get('feeRateBps')}")

            response = client.post_order(signed_order, OrderType.GTC)
            print(f"\nRESPONSE: {json.dumps(response, indent=2)}")
            print("\nSUCCESS! Python SDK POLY_PROXY mode works!")

            # Cancel the order immediately
            try:
                client.cancel_all()
                print("(Test order cancelled)")
            except:
                pass
            exit(0)
        except Exception as e:
            err = str(e)
            print(f"  FAILED: {err}")
            if "invalid signature" in err.lower():
                print("  => Signature issue exists in Python too! Account setup problem.")
                exit(1)
            elif "invalid token" in err.lower():
                print(f"  => Token not valid on {'NegRisk' if neg_risk else 'CTF'} exchange, trying next...")
                # Try with opposite negRisk
                try:
                    signed_order2 = client.create_order(
                        OrderArgs(token_id=token_id, price=price, size=5, side=BUY),
                        options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=not neg_risk),
                    )
                    response2 = client.post_order(signed_order2, OrderType.GTC)
                    print(f"\n  WORKED with neg_risk={not neg_risk}!")
                    print(f"  RESPONSE: {json.dumps(response2, indent=2)}")
                    try:
                        client.cancel_all()
                    except:
                        pass
                    exit(0)
                except Exception as e2:
                    print(f"  Also failed with neg_risk={not neg_risk}: {e2}")
                continue
            else:
                print(f"  => Other error, trying next market...")
                continue

print("\nCould not find a working market. All attempts failed.")
