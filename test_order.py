"""
Minimal test: place a tiny order using the official Polymarket Python SDK.
If this works, the issue is in our C# EIP-712 signing.
If this also fails, the issue is credentials/wallet setup.

Install: pip install py-clob-client
Usage:  Set env vars then run: python test_order.py
"""
import os, json

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, ApiCreds
from py_clob_client.order_builder.constants import BUY

# Load credentials from env
host = "https://clob.polymarket.com"
chain_id = 137
key = os.environ["POLY_API_KEY"]
secret = os.environ["POLY_API_SECRET"]
passphrase = os.environ["POLY_API_PASSPHRASE"]
private_key = os.environ["POLY_PRIVATE_KEY"]

creds = ApiCreds(api_key=key, api_secret=secret, api_passphrase=passphrase)
client = ClobClient(host, chain_id=chain_id, key=private_key, creds=creds)

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
    options={"tick_size": "0.01", "neg_risk": False},
)

# Step 2: Print the EXACT payload that would be sent
payload = {
    "order": signed_order.dict(),
    "owner": key,
    "orderType": "GTC",
}
print("=== PYTHON SDK PAYLOAD ===")
print(json.dumps(payload, indent=2))

# Step 3: Actually post it (uncomment when ready)
# response = client.post_order(signed_order, OrderType.GTC)
# print("\n=== RESPONSE ===")
# print(json.dumps(response, indent=2))
