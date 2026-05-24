"""
Test V2 order with POLY_1271 (type=3), maker=Safe, signer=Safe.

The API key was registered to the Safe address, so order.signer MUST be the Safe.
For type=3, the CLOB then calls Safe.isValidSignature(orderDigest, sig).
The Safe wraps the digest internally, so the EOA must sign the safe-wrapped version.

Two variants:
  A: EOA signs raw EIP-712 digest (simple ecrecover flow, in case CLOB doesn't call isValidSignature)
  B: EOA signs safe-wrapped digest (for Gnosis Safe isValidSignature)

Usage:
  export POLY_PRIVATE_KEY="0x..."
  export POLY_API_KEY="..."  POLY_API_SECRET="..."  POLY_API_PASSPHRASE="..."
  export POLY_PROXY_ADDRESS="0x..."   (the Gnosis Safe)
  export POLY_SOCKS_PROXY="socks5://127.0.0.1:8081"
  python3 helpers/test_order_v2_signer_safe.py
"""
import os, json, time, random, requests
from eth_utils.crypto import keccak
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak as eth_keccak

PRIVATE_KEY    = os.environ["POLY_PRIVATE_KEY"]
API_KEY        = os.environ["POLY_API_KEY"]
API_SECRET     = os.environ["POLY_API_SECRET"]
API_PASSPHRASE = os.environ["POLY_API_PASSPHRASE"]
PROXY_ADDR     = os.environ["POLY_PROXY_ADDRESS"]   # Gnosis Safe
SOCKS_PROXY    = os.environ.get("POLY_SOCKS_PROXY", "")

HOST     = "https://clob.polymarket.com"
CHAIN    = 137
EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"
TOKEN_ID = "29880061952566489686808125557917525240335275846871233640387819113187553719242"
PRICE    = 0.02
SIZE     = 1.0

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

print(f"Safe (maker+signer): {PROXY_ADDR}")
print(f"Safe domain sep:     0x{_safe_domain_sep.hex()}")

# ── Manually build typed data with signer=Safe (not EOA) ─────────────────────
salt   = str(int(random.random() * (time.time_ns() // 1_000_000)))
ts_ms  = str(int(time.time() * 1000))

typed_data = {
    "primaryType": "Order",
    "types": {
        "EIP712Domain": [
            {"name": "name",              "type": "string"},
            {"name": "version",           "type": "string"},
            {"name": "chainId",           "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
        "Order": [
            {"name": "salt",          "type": "uint256"},
            {"name": "maker",         "type": "address"},
            {"name": "signer",        "type": "address"},
            {"name": "tokenId",       "type": "uint256"},
            {"name": "makerAmount",   "type": "uint256"},
            {"name": "takerAmount",   "type": "uint256"},
            {"name": "side",          "type": "uint8"},
            {"name": "signatureType", "type": "uint8"},
            {"name": "timestamp",     "type": "uint256"},
            {"name": "metadata",      "type": "bytes32"},
            {"name": "builder",       "type": "bytes32"},
        ],
    },
    "domain": {
        "name":              "Polymarket CTF Exchange",
        "version":           "2",
        "chainId":           CHAIN,
        "verifyingContract": EXCHANGE,
    },
    "message": {
        "salt":          int(salt),
        "maker":         PROXY_ADDR,   # Safe
        "signer":        PROXY_ADDR,   # Safe (same — this is the key change)
        "tokenId":       int(TOKEN_ID),
        "makerAmount":   int(PRICE * SIZE * 1_000_000),
        "takerAmount":   int(SIZE * 1_000_000),
        "side":          0,            # BUY
        "signatureType": 3,            # POLY_1271
        "timestamp":     int(ts_ms),
        "metadata":      bytes(32),
        "builder":       bytes(32),
    },
}

encoded    = encode_typed_data(full_message=typed_data)
raw_digest = eth_keccak(primitive=b"\x19" + encoded.version + encoded.header + encoded.body)
wrapped    = safe_wrap(raw_digest)

print(f"Raw EIP-712 digest:   0x{raw_digest.hex()}")
print(f"Safe-wrapped digest:  0x{wrapped.hex()}")

account = Account.from_key(PRIVATE_KEY)
signer_sdk = Signer(PRIVATE_KEY, CHAIN)
creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
proxies = {"https": SOCKS_PROXY, "http": SOCKS_PROXY} if SOCKS_PROXY else None

def submit(sig_hex: str, label: str):
    wire = {
        "order": {
            "salt":          int(salt),
            "maker":         PROXY_ADDR,
            "signer":        PROXY_ADDR,   # Safe
            "tokenId":       TOKEN_ID,
            "makerAmount":   str(int(PRICE * SIZE * 1_000_000)),
            "takerAmount":   str(int(SIZE * 1_000_000)),
            "side":          "BUY",
            "expiration":    "0",
            "signatureType": 3,
            "timestamp":     ts_ms,
            "metadata":      "0x" + "00" * 32,
            "builder":       "0x" + "00" * 32,
            "signature":     sig_hex,
        },
        "owner":     API_KEY,
        "orderType": "FAK",
        "deferExec": False,
        "postOnly":  False,
    }
    body_str = json.dumps(wire, separators=(",", ":"), ensure_ascii=False)
    ts_sec = int(time.time())
    args   = RequestArgs(method="POST", request_path="/order", body=wire, serialized_body=body_str)
    hdrs   = create_level_2_headers(signer_sdk, creds, args, timestamp=ts_sec)
    hdrs["Content-Type"] = "application/json"
    resp = requests.post(f"{HOST}/order", data=body_str, headers=hdrs, proxies=proxies, timeout=10)
    print(f"  HTTP {resp.status_code}: {resp.text}")

# ── Test A: EOA signs raw digest ──────────────────────────────────────────────
raw_sig = "0x" + Account._sign_hash(raw_digest, PRIVATE_KEY).signature.hex()
print(f"\nTest A: signer=Safe, type=3, EOA signs RAW digest")
submit(raw_sig, "A")

# ── Test B: EOA signs safe-wrapped digest ─────────────────────────────────────
wrap_sig = "0x" + Account._sign_hash(wrapped, PRIVATE_KEY).signature.hex()
print(f"\nTest B: signer=Safe, type=3, EOA signs SAFE-WRAPPED digest")
submit(wrap_sig, "B")
