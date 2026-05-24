"""
Test V2 order with type=0 (EOA as maker, no Safe involved).
The CLOB verifies: ecrecover(orderDigest, sig) == maker.
No registry or Safe needed — the EOA IS the maker and signer.

If this gets past "invalid signature" (even if it fails for balance/allowance),
the EOA account is valid and we just need to fund + set V2 allowances.

Usage:
  export POLY_PRIVATE_KEY="0x..."
  export POLY_API_KEY="..."  POLY_API_SECRET="..."  POLY_API_PASSPHRASE="..."
  export POLY_SOCKS_PROXY="socks5://127.0.0.1:8081"
  python3 helpers/test_order_v2_eoa.py
"""
import os, json, time, requests
from eth_account import Account

PRIVATE_KEY    = os.environ["POLY_PRIVATE_KEY"]
API_KEY        = os.environ["POLY_API_KEY"]
API_SECRET     = os.environ["POLY_API_SECRET"]
API_PASSPHRASE = os.environ["POLY_API_PASSPHRASE"]
SOCKS_PROXY    = os.environ.get("POLY_SOCKS_PROXY", "")

HOST  = "https://clob.polymarket.com"
CHAIN = 137
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

signer    = Signer(PRIVATE_KEY, CHAIN)
EOA_ADDR  = signer.address()
print(f"EOA (maker+signer): {EOA_ADDR}")

cfg     = get_contract_config(CHAIN)
builder = ExchangeOrderBuilderV2(cfg.neg_risk_exchange_v2, CHAIN, signer)
ts_ms   = str(int(time.time() * 1000))

order_data = OrderDataV2(
    maker         = EOA_ADDR,       # EOA is both maker and signer
    tokenId       = TOKEN_ID,
    makerAmount   = str(int(PRICE * SIZE * 1_000_000)),
    takerAmount   = str(int(SIZE * 1_000_000)),
    side          = Side.BUY,
    signer        = EOA_ADDR,
    signatureType = SignatureTypeV2.EOA,   # type=0
    timestamp     = ts_ms,
    metadata      = BYTES32_ZERO,
    builder       = BYTES32_ZERO,
    expiration    = "0",
)

signed_order = builder.build_signed_order(order_data)
typed_data   = builder.build_order_typed_data(signed_order)
from py_clob_client_v2.order_utils import ExchangeOrderBuilderV2
order_hash   = builder.build_order_hash(typed_data)
wire         = order_to_json_v2(signed_order, API_KEY, "FAK", False, False)
body_str     = json.dumps(wire, separators=(",", ":"), ensure_ascii=False)

print(f"orderHash:     {order_hash}")
print(f"signatureType: {int(SignatureTypeV2.EOA)}")
print(f"\n=== Wire body ===\n{json.dumps(wire, indent=2)}")

ts_sec  = int(time.time())
creds   = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
args    = RequestArgs(method="POST", request_path="/order", body=wire, serialized_body=body_str)
headers = create_level_2_headers(signer, creds, args, timestamp=ts_sec)
headers["Content-Type"] = "application/json"

proxies = {"https": SOCKS_PROXY, "http": SOCKS_PROXY} if SOCKS_PROXY else None

print(f"\n=== Submitting (proxy={'yes' if SOCKS_PROXY else 'no'}) ===")
resp = requests.post(f"{HOST}/order", data=body_str, headers=headers, proxies=proxies, timeout=10)
print(f"HTTP {resp.status_code}: {resp.text}")
