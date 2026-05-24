"""
Test V2 order signing with explicit Gnosis Safe wrapping.

For POLY_GNOSIS_SAFE (type=2), the CLOB calls Safe.isValidSignature(orderDigest, sig).
The Safe internally wraps orderDigest before calling ecrecover, so the EOA must sign
the safe-wrapped digest — NOT the raw EIP-712 order digest.

If this submits successfully, C# must do the same and the issue is confirmed.

Usage:
  export POLY_PRIVATE_KEY="0x..."
  export POLY_API_KEY="..."  POLY_API_SECRET="..."  POLY_API_PASSPHRASE="..."
  export POLY_PROXY_ADDRESS="0x..."
  export POLY_SOCKS_PROXY="socks5://127.0.0.1:8081"
  python3 helpers/test_order_v2_safe.py
"""
import os, json, time, requests
from eth_utils.crypto import keccak
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak as eth_keccak

PRIVATE_KEY    = os.environ["POLY_PRIVATE_KEY"]
API_KEY        = os.environ["POLY_API_KEY"]
API_SECRET     = os.environ["POLY_API_SECRET"]
API_PASSPHRASE = os.environ["POLY_API_PASSPHRASE"]
PROXY_ADDR     = os.environ["POLY_PROXY_ADDRESS"]
SOCKS_PROXY    = os.environ.get("POLY_SOCKS_PROXY", "")

HOST  = "https://clob.polymarket.com"
CHAIN = 137
TOKEN_ID = "29880061952566489686808125557917525240335275846871233640387819113187553719242"
PRICE    = 0.02
SIZE     = 1.0

from py_clob_client_v2.order_utils.model.side import Side
from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2
from py_clob_client_v2.order_utils import ExchangeOrderBuilderV2
from py_clob_client_v2.order_utils.model.order_data_v2 import OrderDataV2, SignedOrderV2, order_to_json_v2
from py_clob_client_v2.config import get_contract_config
from py_clob_client_v2.constants import BYTES32_ZERO
from py_clob_client_v2.signer import Signer
from py_clob_client_v2.clob_types import ApiCreds, RequestArgs
from py_clob_client_v2.headers.headers import create_level_2_headers

# ── Gnosis Safe wrapping ──────────────────────────────────────────────────────
def pad_uint256(v: int) -> bytes: return v.to_bytes(32, 'big')
def pad_address(a: str) -> bytes:
    raw = bytes.fromhex(a[2:] if a.startswith('0x') else a)
    return b'\x00' * (32 - len(raw)) + raw

_safe_dom_typehash = keccak(text="EIP712Domain(uint256 chainId,address verifyingContract)")
_safe_msg_typehash = keccak(text="SafeMessage(bytes message)")
_safe_domain_sep   = keccak(_safe_dom_typehash + pad_uint256(CHAIN) + pad_address(PROXY_ADDR))

def safe_wrap(raw_hash: bytes) -> bytes:
    safe_msg_hash = keccak(_safe_msg_typehash + keccak(primitive=raw_hash))
    return keccak(b'\x19\x01' + _safe_domain_sep + safe_msg_hash)

print(f"Safe domain sep:  0x{_safe_domain_sep.hex()}")

# ── Build order ───────────────────────────────────────────────────────────────
signer  = Signer(PRIVATE_KEY, CHAIN)
cfg     = get_contract_config(CHAIN)
builder = ExchangeOrderBuilderV2(cfg.exchange_v2, CHAIN, signer)
ts_ms   = str(int(time.time() * 1000))

order_data = OrderDataV2(
    maker         = PROXY_ADDR,
    tokenId       = TOKEN_ID,
    makerAmount   = str(int(PRICE * SIZE * 1_000_000)),
    takerAmount   = str(int(SIZE * 1_000_000)),
    side          = Side.BUY,
    signer        = signer.address(),
    signatureType = SignatureTypeV2.POLY_GNOSIS_SAFE,
    timestamp     = ts_ms,
    metadata      = BYTES32_ZERO,
    builder       = BYTES32_ZERO,
    expiration    = "0",
)

order      = builder.build_order(order_data)
typed_data = builder.build_order_typed_data(order)

# ── Compute raw EIP-712 order digest ─────────────────────────────────────────
encoded    = encode_typed_data(full_message=typed_data)
raw_digest = eth_keccak(primitive=b"\x19" + encoded.version + encoded.header + encoded.body)
print(f"Raw EIP-712 digest:   0x{raw_digest.hex()}")

# ── Safe-wrap and sign ────────────────────────────────────────────────────────
wrapped = safe_wrap(raw_digest)
print(f"Safe-wrapped digest:  0x{wrapped.hex()}")

account = Account.from_key(PRIVATE_KEY)
sig = Account._sign_hash(wrapped, PRIVATE_KEY)
signature = "0x" + sig.signature.hex()

recovered = Account._recover_hash(wrapped, signature=sig.signature)
print(f"Ecrecover:   {recovered}")
print(f"EOA:         {account.address}")
print(f"Match:       {recovered.lower() == account.address.lower()}")

# ── Build wire body with the safe-wrapped signature ───────────────────────────
signed = SignedOrderV2(
    salt=order.salt, maker=order.maker, signer=order.signer,
    tokenId=order.tokenId, makerAmount=order.makerAmount, takerAmount=order.takerAmount,
    side=order.side, signatureType=order.signatureType, timestamp=order.timestamp,
    metadata=order.metadata, builder=order.builder, expiration=order.expiration,
    signature=signature,
)
wire = order_to_json_v2(signed, API_KEY, "FAK", False, False)
body_str = json.dumps(wire, separators=(",", ":"), ensure_ascii=False)

print(f"\n=== Wire body ===\n{json.dumps(wire, indent=2)}")

ts_sec = int(time.time())
creds   = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
args    = RequestArgs(method="POST", request_path="/order", body=wire, serialized_body=body_str)
headers = create_level_2_headers(signer, creds, args, timestamp=ts_sec)
headers["Content-Type"] = "application/json"

proxies = {"https": SOCKS_PROXY, "http": SOCKS_PROXY} if SOCKS_PROXY else None

print(f"\n=== Submitting (proxy={'yes' if SOCKS_PROXY else 'no'}) ===")
resp = requests.post(f"{HOST}/order", data=body_str, headers=headers, proxies=proxies, timeout=10)
print(f"HTTP {resp.status_code}: {resp.text}")
