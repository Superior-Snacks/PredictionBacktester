"""
Minimal test: place a tiny order using the official Polymarket Python SDK.
If this works, the issue is in our C# EIP-712 signing.
If this also fails, the issue is credentials/wallet setup.

Install: pip install py-clob-client
Usage:  Set env vars then run: python test_order.py
"""
import os, json

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, ApiCreds, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY

# Load credentials from env
host = "https://clob.polymarket.com"
chain_id = 137
key = os.environ["POLY_API_KEY"]
secret = os.environ["POLY_API_SECRET"]
passphrase = os.environ["POLY_API_PASSPHRASE"]
private_key = os.environ["POLY_PRIVATE_KEY"]

proxy_address = os.environ["POLY_PROXY_ADDRESS"]

creds = ApiCreds(api_key=key, api_secret=secret, api_passphrase=passphrase)
# funder=proxy uses POLY_PROXY mode (signatureType=1, maker=proxy, signer=EOA)
client = ClobClient(host, chain_id=chain_id, key=private_key, creds=creds, funder=proxy_address)

# Use a known active token ID (replace with one from your bot's logs)
TOKEN_ID = "23913477838590520829397598255983621021172073199756406567397887821099989290311"

# Step 1: Create and sign the order (don't post yet)
signed_order = client.create_order(
    OrderArgs(
        token_id=TOKEN_ID,
        price=0.88,
        size=6.82,
        side=BUY,
    ),
    options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
)

# Step 2: Print the EXACT payload that would be sent
payload = {
    "order": signed_order.dict(),
    "owner": key,
    "orderType": "GTC",
}
print("=== PYTHON SDK PAYLOAD ===")
print(json.dumps(payload, indent=2))

# Step 3: Check what signature type the SDK chose
order_dict = signed_order.dict()
print(f"\nsignatureType: {order_dict.get('signatureType')}")
print(f"maker:  {order_dict.get('maker')}")
print(f"signer: {order_dict.get('signer')}")
print(f"feeRateBps: {order_dict.get('feeRateBps')}")

# Step 4: Actually post it
response = client.post_order(signed_order, OrderType.GTC)
print("\n=== RESPONSE ===")
print(json.dumps(response, indent=2))
