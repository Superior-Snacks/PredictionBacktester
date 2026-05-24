"""
Verify which exchange contract + signing method the web app used.

Paste the exact values from the captured web app order below.
This tries all 4 combinations (2 contracts × raw/safe-wrapped) and
reports which one ecrecovers to the EOA — that is the correct signing formula.

Run: python3 helpers/verify_web_order.py
"""
from eth_utils.crypto import keccak
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak as eth_keccak

# ── Paste web app order fields here ──────────────────────────────────────────
WEB_SIGNATURE = "0xa6901f35ca4c8a2cce40b5ba003c9a89f8ca02148403055ffbbe5c421c5e5e525d29419e5379fb49ba66c8260c357a1e3e41650e994d83069996b5c92b2de2871b"
EOA      = "0xf786a3DAe390d2342886ABA75e61529F75E953D7"
SAFE     = "0x595651219C3f6cBB5eb37fae5cEd4C6Caf4907CB"
SALT     = 437625480531
TOKEN_ID = 15446703673614499771405405557748265863218798432662907076142129469418578430476
MAKER_AMOUNT = 1000000
TAKER_AMOUNT = 1127390
SIDE     = 0        # BUY
SIG_TYPE = 2        # GNOSIS_SAFE
TIMESTAMP = 1779664566500
CHAIN    = 137

EXCHANGE_V2      = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_V2      = "0xe2222d279d744050d28e00520010520000310F59"

# ── Safe wrapping ─────────────────────────────────────────────────────────────
def pad_uint256(v: int) -> bytes: return v.to_bytes(32, 'big')
def pad_address(a: str) -> bytes:
    raw = bytes.fromhex(a[2:] if a.startswith('0x') else a)
    return b'\x00' * (32 - len(raw)) + raw

_safe_dom_typehash = keccak(text="EIP712Domain(uint256 chainId,address verifyingContract)")
_safe_msg_typehash = keccak(text="SafeMessage(bytes message)")
_safe_domain_sep   = keccak(_safe_dom_typehash + pad_uint256(CHAIN) + pad_address(SAFE))

def safe_wrap(raw_hash: bytes) -> bytes:
    safe_msg_hash = keccak(_safe_msg_typehash + keccak(primitive=raw_hash))
    return keccak(b'\x19\x01' + _safe_domain_sep + safe_msg_hash)

# ── EIP-712 typed data builder ────────────────────────────────────────────────
ORDER_TYPES = {
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
}

def build_digest(exchange: str) -> bytes:
    typed_data = {
        "primaryType": "Order",
        "types": ORDER_TYPES,
        "domain": {
            "name":              "Polymarket CTF Exchange",
            "version":           "2",
            "chainId":           CHAIN,
            "verifyingContract": exchange,
        },
        "message": {
            "salt":          SALT,
            "maker":         MAKER,
            "signer":        SIGNER,
            "tokenId":       TOKEN_ID,
            "makerAmount":   MAKER_AMOUNT,
            "takerAmount":   TAKER_AMOUNT,
            "side":          SIDE,
            "signatureType": SIG_TYPE,
            "timestamp":     TIMESTAMP,
            "metadata":      bytes(32),
            "builder":       bytes(32),
        },
    }
    encoded = encode_typed_data(full_message=typed_data)
    return eth_keccak(primitive=b"\x19" + encoded.version + encoded.header + encoded.body)

MAKER  = SAFE
SIGNER = EOA

sig_bytes = bytes.fromhex(WEB_SIGNATURE.replace("0x", ""))
print(f"EOA:  {EOA}")
print(f"Safe: {SAFE}")
print()

for exchange_name, exchange_addr in [("exchange_v2 (0xE111...)", EXCHANGE_V2), ("neg_risk_exchange_v2 (0xe222...)", NEG_RISK_V2)]:
    raw   = build_digest(exchange_addr)
    wrapped = safe_wrap(raw)

    rec_raw  = Account._recover_hash(raw,     signature=sig_bytes)
    rec_wrap = Account._recover_hash(wrapped, signature=sig_bytes)

    raw_match  = "✓ MATCH" if rec_raw.lower()  == EOA.lower() else f"✗ got {rec_raw}"
    wrap_match = "✓ MATCH" if rec_wrap.lower() == EOA.lower() else f"✗ got {rec_wrap}"

    print(f"Contract: {exchange_name}")
    print(f"  Raw digest:          0x{raw.hex()}")
    print(f"  ecrecover(raw):      {raw_match}")
    print(f"  Safe-wrapped digest: 0x{wrapped.hex()}")
    print(f"  ecrecover(wrapped):  {wrap_match}")
    print()
