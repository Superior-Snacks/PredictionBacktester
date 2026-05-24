"""
Test V2 order signing end-to-end using py_clob_client_v2 directly.
This sends a real but tiny order to confirm:
  - Python SDK can sign V2 orders and the server accepts them
  - What the working wire body looks like

Run on Linux server:
  export POLY_PRIVATE_KEY="0x..."
  export POLY_API_KEY="..."
  export POLY_API_SECRET="..."
  export POLY_API_PASSPHRASE="..."
  export POLY_PROXY_ADDRESS="0x..."   # the maker/funder address
  python helpers/test_order_v2.py

Tweak TOKEN_ID + PRICE before running to a real market you want to test against.
"""
import os, json, time
from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, OrderArgsV2
from py_clob_client_v2.order_utils.model.side import Side
from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2
from py_clob_client_v2.order_utils import ExchangeOrderBuilderV2
from py_clob_client_v2.order_utils.model.order_data_v2 import OrderDataV2, order_to_json_v2
from py_clob_client_v2.config import get_contract_config
from py_clob_client_v2.constants import BYTES32_ZERO
from py_clob_client_v2.signer import Signer
from eth_utils.crypto import keccak

PRIVATE_KEY    = os.environ["POLY_PRIVATE_KEY"]
API_KEY        = os.environ["POLY_API_KEY"]
API_SECRET     = os.environ["POLY_API_SECRET"]
API_PASSPHRASE = os.environ["POLY_API_PASSPHRASE"]
PROXY_ADDR     = os.environ["POLY_PROXY_ADDRESS"]  # maker (funder)

HOST    = "https://clob.polymarket.com"
CHAIN   = 137

# Change these to a real open market token you can test against.
# Use a low price (0.02) so the FAK order almost certainly won't fill.
TOKEN_ID = "29880061952566489686808125557917525240335275846871233640387819113187553719242"
PRICE    = 0.02
SIZE     = 1.0
SIG_TYPE = SignatureTypeV2.POLY_PROXY  # try 1 first; change to GNOSIS_SAFE=2 or POLY_1271=3 if needed

# ---------- build the order manually so we can inspect intermediate hashes ----------
signer  = Signer(PRIVATE_KEY, CHAIN)
cfg     = get_contract_config(CHAIN)
builder = ExchangeOrderBuilderV2(cfg.exchange_v2, CHAIN, signer)

ts = str(int(time.time() * 1000))  # ms

order_data = OrderDataV2(
    maker       = PROXY_ADDR,
    tokenId     = TOKEN_ID,
    makerAmount = str(int(PRICE * SIZE * 1_000_000)),    # e.g. 20000 for $0.02
    takerAmount = str(int(SIZE * 1_000_000)),            # e.g. 1000000 for 1 share
    side        = Side.BUY,
    signer      = signer.address(),
    signatureType = SIG_TYPE,
    timestamp   = ts,
    metadata    = BYTES32_ZERO,
    builder     = BYTES32_ZERO,
    expiration  = "0",
)

signed_order = builder.build_signed_order(order_data)
typed_data   = builder.build_order_typed_data(signed_order)
order_hash   = builder.build_order_hash(typed_data)

# Show intermediate hashes for comparison with C# debug output
from eth_account.messages import encode_typed_data as _enc
encoded = _enc(full_message=typed_data)

print("=== Python V2 order hashes ===")
print(f"domainSep  (should match C# [EIP712]): check below")
print(f"orderHash  : {order_hash}")
print(f"timestamp  : {ts}")
print(f"signatureType: {int(SIG_TYPE)}")
print()
print("=== Wire body ===")
wire = order_to_json_v2(signed_order, API_KEY, "FAK", False, False)
print(json.dumps(wire, indent=2))

# ---------- submit via ClobClient ----------
print("\n=== Submitting order via ClobClient ===")
creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
client = ClobClient(
    host=HOST, chain_id=CHAIN, key=PRIVATE_KEY,
    creds=creds, signature_type=SIG_TYPE, funder=PROXY_ADDR
)

try:
    args = OrderArgsV2(
        token_id    = TOKEN_ID,
        price       = PRICE,
        size        = SIZE,
        side        = Side.BUY,
    )
    result = client.create_and_post_order(args)
    print(f"Result: {json.dumps(result, indent=2)}")
except Exception as e:
    print(f"Error: {e}")
