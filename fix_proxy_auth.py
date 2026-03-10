"""
Fix POLY_PROXY authorization by re-deriving API credentials.

When you derive API keys through the SDK with funder=proxy, the CLOB server
registers the EOA -> proxy mapping. This might fix the 'invalid signature' error.

Usage: python fix_proxy_auth.py
"""
import os, json

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

host = "https://clob.polymarket.com"
chain_id = 137
private_key = os.environ["POLY_PRIVATE_KEY"]
proxy_address = os.environ["POLY_PROXY_ADDRESS"]

# Step 1: Create client WITHOUT existing creds, WITH funder (proxy)
# This tells the SDK to use POLY_PROXY mode
print("=== Step 1: Create client with funder (POLY_PROXY mode) ===")
client = ClobClient(host, chain_id=chain_id, key=private_key, funder=proxy_address)

print(f"  EOA signer: {client.get_address()}")
print(f"  Funder/proxy: {proxy_address}")

# Step 2: Derive API key — this signs a CLOB auth message and registers with the server
print("\n=== Step 2: Derive API key (this registers EOA-proxy mapping) ===")
try:
    api_creds = client.derive_api_key()
    print(f"  Success! New API credentials:")
    print(f"  API Key:      {api_creds.api_key}")
    print(f"  API Secret:   {api_creds.api_secret}")
    print(f"  Passphrase:   {api_creds.api_passphrase}")
    print(f"\n  UPDATE your environment variables:")
    print(f"    POLY_API_KEY={api_creds.api_key}")
    print(f"    POLY_API_SECRET={api_creds.api_secret}")
    print(f"    POLY_API_PASSPHRASE={api_creds.api_passphrase}")
except Exception as e:
    print(f"  Failed: {e}")
    print(f"\n  If this failed, try Step 3 below.")

# Step 3: Alternative — try to create API key (slightly different endpoint)
print("\n=== Step 3: Try create_api_key as alternative ===")
try:
    api_creds2 = client.create_api_key()
    print(f"  Success! New API credentials:")
    print(f"  API Key:      {api_creds2.api_key}")
    print(f"  API Secret:   {api_creds2.api_secret}")
    print(f"  Passphrase:   {api_creds2.api_passphrase}")
except Exception as e:
    print(f"  Failed: {e}")

# Step 4: Now test with the new creds
print("\n=== Step 4: Test order with new credentials ===")
try:
    # Re-read the newly derived creds
    new_key = api_creds.api_key
    new_secret = api_creds.api_secret
    new_passphrase = api_creds.api_passphrase

    new_creds = ApiCreds(api_key=new_key, api_secret=new_secret, api_passphrase=new_passphrase)
    client2 = ClobClient(host, chain_id=chain_id, key=private_key, creds=new_creds, funder=proxy_address)

    from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
    from py_clob_client.order_builder.constants import BUY

    TOKEN_ID = "23913477838590520829397598255983621021172073199756406567397887821099989290311"

    signed_order = client2.create_order(
        OrderArgs(token_id=TOKEN_ID, price=0.88, size=6.82, side=BUY),
        options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
    )

    order_dict = signed_order.dict()
    print(f"  signatureType: {order_dict.get('signatureType')}")
    print(f"  maker:  {order_dict.get('maker')}")
    print(f"  signer: {order_dict.get('signer')}")

    response = client2.post_order(signed_order, OrderType.GTC)
    print(f"\n  ORDER RESPONSE: {json.dumps(response, indent=2)}")
    print("\n  SUCCESS! POLY_PROXY mode is now working!")
except Exception as e:
    print(f"  Test failed: {e}")
    print(f"\n  If the test still fails with 'invalid signature',")
    print(f"  your EOA private key may not be the one that controls this proxy.")
    print(f"  Try: Polymarket.com → Settings → Export Private Key")
    print(f"  and make sure POLY_PRIVATE_KEY matches exactly.")
