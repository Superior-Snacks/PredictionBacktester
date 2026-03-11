"""
Submit a real test order using the Python SDK to verify POLY_PROXY mode works.
If this ALSO gets "invalid signature", the issue is with account setup, not C#.

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

# Find an active non-negRisk market with volume
print("\nFinding active market...")
resp = requests.get("https://gamma-api.polymarket.com/events?active=true&closed=false&limit=5")
events = resp.json()

token_id = None
market_name = None
neg_risk = False

for ev in events:
    for mkt in ev.get("markets", []):
        tokens = mkt.get("clobTokenIds")
        if tokens and len(tokens) > 0:
            token_id = tokens[0]
            market_name = mkt["question"][:60]
            neg_risk = ev.get("negRisk", False)
            break
    if token_id:
        break

print(f"Market: {market_name}")
print(f"Token:  {token_id[:40]}...")
print(f"NegRisk: {neg_risk}")

# Create and submit order at very low price (won't fill)
print("\nCreating order (BUY @ $0.01, 5 shares)...")
try:
    signed_order = client.create_order(
        OrderArgs(token_id=token_id, price=0.01, size=5, side=BUY),
        options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=neg_risk),
    )

    order_dict = signed_order.dict()
    print(f"  signatureType: {order_dict.get('signatureType')}")
    print(f"  maker:  {order_dict.get('maker')}")
    print(f"  signer: {order_dict.get('signer')}")
    print(f"  feeRateBps: {order_dict.get('feeRateBps')}")
    print(f"  signature: {order_dict.get('signature')[:40]}...")

    response = client.post_order(signed_order, OrderType.GTC)
    print(f"\nRESPONSE: {json.dumps(response, indent=2)}")
    print("\nSUCCESS! Python SDK can place orders.")
except Exception as e:
    print(f"\nFAILED: {e}")
    print("\nIf 'invalid signature': the issue is account setup, not C# code.")
    print("If other error: check the specific error message.")
