"""
Submit a real test order using the Python SDK to verify POLY_PROXY mode works.

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

# Get markets from CLOB directly (these definitely have orderbooks)
print("\n=== Finding markets via CLOB API ===")
clob_markets = requests.get(f"{host}/markets?next_cursor=LQ==").json()
print(f"Got {len(clob_markets)} markets from CLOB")

attempts = 0
for mkt in clob_markets:
    if attempts >= 5:
        break
    tokens = mkt.get("tokens", [])
    if not tokens:
        continue

    token_id = tokens[0].get("token_id")
    condition_id = mkt.get("condition_id", "?")
    neg_risk = mkt.get("neg_risk", False)
    question = mkt.get("question", "?")[:50]
    active = mkt.get("active", False)

    if not active or not token_id:
        continue

    attempts += 1
    print(f"\n--- Attempt {attempts}: {question} ---")
    print(f"  token:    {token_id[:40]}...")
    print(f"  negRisk:  {neg_risk}")
    print(f"  active:   {active}")

    for try_neg_risk in [neg_risk, not neg_risk]:
        exchange = "NegRisk" if try_neg_risk else "CTF"
        print(f"  Trying with neg_risk={try_neg_risk} ({exchange} exchange)...")
        try:
            signed_order = client.create_order(
                OrderArgs(token_id=token_id, price=0.02, size=5, side=BUY),
                options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=try_neg_risk),
            )

            order_dict = signed_order.dict()
            print(f"    signatureType: {order_dict.get('signatureType')}")
            print(f"    maker:  {order_dict.get('maker')}")
            print(f"    signer: {order_dict.get('signer')}")

            response = client.post_order(signed_order, OrderType.GTC)
            print(f"\n  RESPONSE: {json.dumps(response, indent=2)}")
            print("\n  SUCCESS! POLY_PROXY mode works!")
            try:
                client.cancel_all()
                print("  (Test order cancelled)")
            except:
                pass
            exit(0)
        except Exception as e:
            err = str(e)
            print(f"    Error: {err[:100]}")
            if "invalid signature" in err.lower():
                print("\n  SIGNATURE ISSUE confirmed in Python too!")
                exit(1)

print("\nAll attempts failed. Trying one more with manual token from CLOB book endpoint...")

# Last resort: get a token we KNOW has a book
try:
    # Use the sampling endpoint
    sampling = requests.get(f"{host}/sampling-markets?next_cursor=LQ==").json()
    for s_mkt in sampling.get("data", []):
        tokens = s_mkt.get("tokens", [])
        if not tokens:
            continue
        token_id = tokens[0].get("token_id")
        condition_id = s_mkt.get("condition_id", "?")
        neg_risk = s_mkt.get("neg_risk", False)

        print(f"\nSampling market: {s_mkt.get('question', '?')[:50]}")
        print(f"  token: {token_id[:40]}...")
        print(f"  negRisk: {neg_risk}")

        for try_neg_risk in [neg_risk, not neg_risk]:
            try:
                signed_order = client.create_order(
                    OrderArgs(token_id=token_id, price=0.02, size=5, side=BUY),
                    options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=try_neg_risk),
                )
                response = client.post_order(signed_order, OrderType.GTC)
                print(f"  SUCCESS with neg_risk={try_neg_risk}!")
                print(f"  RESPONSE: {json.dumps(response, indent=2)}")
                try:
                    client.cancel_all()
                except:
                    pass
                exit(0)
            except Exception as e:
                print(f"  neg_risk={try_neg_risk}: {str(e)[:80]}")
except Exception as e:
    print(f"  Sampling endpoint failed: {e}")

print("\nAll attempts exhausted.")
