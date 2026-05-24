"""
Test V2 order signing end-to-end using py_clob_client_v2.
Run on the Linux server (same machine as the C# bot).

Usage:
  export POLY_PRIVATE_KEY="0x..."
  export POLY_API_KEY="..."
  export POLY_API_SECRET="..."
  export POLY_API_PASSPHRASE="..."
  export POLY_PROXY_ADDRESS="0x..."          # maker/funder (proxy wallet)
  export POLY_SOCKS_PROXY="socks5://..."     # optional — same as C# SocksProxy config
  python helpers/test_order_v2.py

Expected outcomes:
  accepted/canceled  → Python V2 works. C# has a signing bug to find.
  "invalid signature" → Account needs V2 proxy wallet registration on-chain.
  403 geo-block       → Run from the Linux server, not Windows.
"""
import os, json, time, requests

PRIVATE_KEY    = os.environ["POLY_PRIVATE_KEY"]
API_KEY        = os.environ["POLY_API_KEY"]
API_SECRET     = os.environ["POLY_API_SECRET"]
API_PASSPHRASE = os.environ["POLY_API_PASSPHRASE"]
PROXY_ADDR     = os.environ["POLY_PROXY_ADDRESS"]
SOCKS_PROXY    = os.environ.get("POLY_SOCKS_PROXY", "")

HOST  = "https://clob.polymarket.com"
CHAIN = 137

# Use a very low price so a FAK order won't fill.
TOKEN_ID = "29880061952566489686808125557917525240335275846871233640387819113187553719242"
PRICE    = 0.02
SIZE     = 1.0

from py_clob_client_v2.order_utils.model.side import Side
from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2
from py_clob_client_v2.order_utils import ExchangeOrderBuilderV2
from py_clob_client_v2.order_utils.model.order_data_v2 import OrderDataV2, order_to_json_v2
from py_clob_client_v2.config import get_contract_config
from py_clob_client_v2.constants import BYTES32_ZERO
from py_clob_client_v2.signer import Signer
from py_clob_client_v2.clob_types import ApiCreds, RequestArgs
from py_clob_client_v2.headers.headers import create_level_2_headers

SIG_TYPE = SignatureTypeV2.POLY_GNOSIS_SAFE  # 2 — correct for Gnosis Safe wallets

signer  = Signer(PRIVATE_KEY, CHAIN)
cfg     = get_contract_config(CHAIN)
builder = ExchangeOrderBuilderV2(cfg.neg_risk_exchange_v2, CHAIN, signer)
ts_ms   = str(int(time.time() * 1000))

order_data = OrderDataV2(
    maker         = PROXY_ADDR,
    tokenId       = TOKEN_ID,
    makerAmount   = str(int(PRICE * SIZE * 1_000_000)),
    takerAmount   = str(int(SIZE * 1_000_000)),
    side          = Side.BUY,
    signer        = signer.address(),
    signatureType = SIG_TYPE,
    timestamp     = ts_ms,
    metadata      = BYTES32_ZERO,
    builder       = BYTES32_ZERO,
    expiration    = "0",
)

signed_order = builder.build_signed_order(order_data)
typed_data   = builder.build_order_typed_data(signed_order)
order_hash   = builder.build_order_hash(typed_data)
wire         = order_to_json_v2(signed_order, API_KEY, "FAK", False, False)
body_str     = json.dumps(wire, separators=(",", ":"), ensure_ascii=False)

print("=== Python V2 order ===")
print(f"orderHash     : {order_hash}")
print(f"signatureType : {int(SIG_TYPE)}")
print(f"timestamp     : {ts_ms}")
print(f"\n=== Wire body ===\n{json.dumps(wire, indent=2)}")

# Build L2 headers the same way ClobClient does internally
ts_sec = int(time.time())
creds  = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
args   = RequestArgs(method="POST", request_path="/order", body=wire, serialized_body=body_str)
headers = create_level_2_headers(signer, creds, args, timestamp=ts_sec)
headers["Content-Type"] = "application/json"

proxies = {"https": SOCKS_PROXY, "http": SOCKS_PROXY} if SOCKS_PROXY else None

print(f"\n=== Submitting (proxy={'yes' if SOCKS_PROXY else 'no'}) ===")
resp = requests.post(f"{HOST}/order", data=body_str, headers=headers, proxies=proxies, timeout=10)
print(f"HTTP {resp.status_code}: {resp.text}")
