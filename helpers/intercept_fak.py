"""
Intercept a live FAK (Immediate-Or-Cancel) order to see exactly 
what the Polymarket matching engine returns for partial/full fills.
"""
import os, json
from unittest.mock import patch
import requests

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, ApiCreds, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY

host = "https://clob.polymarket.com"
chain_id = 137
key = os.environ["POLY_API_KEY"]
secret = os.environ["POLY_API_SECRET"]
passphrase = os.environ["POLY_API_PASSPHRASE"]
private_key = os.environ["POLY_PRIVATE_KEY"]
proxy_address = os.environ["POLY_PROXY_ADDRESS"]

creds = ApiCreds(api_key=key, api_secret=secret, api_passphrase=passphrase)
client = ClobClient(host, chain_id=chain_id, key=private_key, creds=creds, funder=proxy_address, signature_type=2)

# We will grab a highly liquid market (e.g., Bitcoin Up/Down) to ensure an instant fill
# sampling-markets only returns active, liquid markets
markets = requests.get(f"{host}/sampling-markets").json().get("data", [])
active_market = next(
    (m for m in markets if m.get("tokens")),
    None
)
if not active_market:
    print("No active market found from sampling-markets endpoint.")
    exit(1)
token_id = active_market["tokens"][0]["token_id"]
neg_risk = active_market.get("neg_risk", False)

print(f"Targeting Market: {active_market.get('question')}")

# Create a tiny order (e.g. buying 2 shares at $0.90 to guarantee an instant market-fill)
signed_order = client.create_order(
    OrderArgs(token_id=token_id, price=0.90, size=2.00, side=BUY),
    options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=neg_risk),
)

# Intercept the actual HTTP POST
original_post = requests.Session.post

def intercepted_post(self, url, *args, **kwargs):
    print(f"\n=== INTERCEPTED HTTP POST ===")
    print(f"URL: {url}")
    return original_post(self, url, *args, **kwargs)

with patch.object(requests.Session, 'post', intercepted_post):
    try:
        # THE CRITICAL DIFFERENCE: Firing as FAK!
        response = client.post_order(signed_order, OrderType.FAK)
        print(f"\n=== RAW JSON RESPONSE FROM POLYMARKET ===")
        print(json.dumps(response, indent=2))
    except Exception as e:
        print(f"\n=== ERROR ===")
        print(e)