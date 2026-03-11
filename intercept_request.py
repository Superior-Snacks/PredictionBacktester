"""
Intercept the EXACT HTTP request the Python SDK sends when posting an order.
Compare headers and body format with what C# sends.
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
client = ClobClient(host, chain_id=chain_id, key=private_key, creds=creds, funder=proxy_address)

TOKEN_ID = "23913477838590520829397598255983621021172073199756406567397887821099989290311"

# Create and sign the order
signed_order = client.create_order(
    OrderArgs(token_id=TOKEN_ID, price=0.88, size=6.82, side=BUY),
    options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
)

# Print the signed order dict
order_dict = signed_order.dict()
print("=== SIGNED ORDER DICT ===")
print(json.dumps(order_dict, indent=2, default=str))

# Now intercept the actual HTTP POST
original_post = requests.Session.post

def intercepted_post(self, url, *args, **kwargs):
    print(f"\n=== INTERCEPTED HTTP POST ===")
    print(f"URL: {url}")
    print(f"\n--- HEADERS ---")
    if 'headers' in kwargs:
        for k, v in kwargs['headers'].items():
            print(f"  {k}: {v}")
    print(f"\n--- BODY ---")
    if 'data' in kwargs:
        try:
            body = json.loads(kwargs['data'])
            print(json.dumps(body, indent=2, default=str))
        except:
            print(kwargs['data'])
    if 'json' in kwargs:
        print(json.dumps(kwargs['json'], indent=2, default=str))

    # Still make the actual request
    return original_post(self, url, *args, **kwargs)

with patch.object(requests.Session, 'post', intercepted_post):
    try:
        response = client.post_order(signed_order, OrderType.GTC)
        print(f"\n=== RESPONSE ===")
        print(json.dumps(response, indent=2))
    except Exception as e:
        print(f"\n=== ERROR ===")
        print(e)
