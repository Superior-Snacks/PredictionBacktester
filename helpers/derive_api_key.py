"""
Re-register / re-derive Polymarket CLOB API credentials for a POLY_GNOSIS_SAFE wallet.

For Gnosis Safe wallets, the CLOB verifies L1 auth by calling isValidSignature on the
Safe. The EOA must sign the safe-wrapped version of the auth message, not the raw hash.

Tries three approaches (different POLY_ADDRESS and wrapping combos) so we can see which
one the CLOB accepts.

Usage:
  export POLY_PRIVATE_KEY="0x..."
  export POLY_PROXY_ADDRESS="0x..."
  export POLY_SOCKS_PROXY="socks5://127.0.0.1:8081"
  python3 helpers/derive_api_key.py
"""
import os, time, requests
from eth_utils.crypto import keccak
from eth_account import Account

PRIVATE_KEY   = os.environ["POLY_PRIVATE_KEY"]
PROXY_ADDRESS = os.environ["POLY_PROXY_ADDRESS"]
SOCKS_PROXY   = os.environ.get("POLY_SOCKS_PROXY", "")
CHAIN_ID      = 137
HOST          = "https://clob.polymarket.com"

account     = Account.from_key(PRIVATE_KEY)
EOA_ADDRESS = account.address
print(f"EOA (signer): {EOA_ADDRESS}")
print(f"Safe (proxy): {PROXY_ADDRESS}")
print(f"Proxy:        {'yes' if SOCKS_PROXY else 'no'}")

proxies = {"https": SOCKS_PROXY, "http": SOCKS_PROXY} if SOCKS_PROXY else None

def pad_uint256(v: int) -> bytes: return v.to_bytes(32, 'big')
def pad_address(a: str) -> bytes:
    raw = bytes.fromhex(a[2:] if a.startswith('0x') else a)
    return b'\x00' * (32 - len(raw)) + raw

# ── ClobAuth EIP-712 message hash ────────────────────────────────────────────
# Domain: EIP712Domain(string name,string version,uint256 chainId)
_domain_typehash = keccak(text="EIP712Domain(string name,string version,uint256 chainId)")
_name_hash       = keccak(text="ClobAuthDomain")
_version_hash    = keccak(text="1")
_auth_domain_sep = keccak(_domain_typehash + _name_hash + _version_hash + pad_uint256(CHAIN_ID))

_struct_typehash  = keccak(text="ClobAuth(address address,string timestamp,uint256 nonce,string message)")
_fixed_msg_hash   = keccak(text="This message attests that I control the given wallet")

def compute_raw_auth_hash(ts_seconds: int, address_field: str) -> bytes:
    """ClobAuth struct hash → EIP-712 digest (no safe-wrapping)."""
    ts_hash = keccak(str(ts_seconds).encode())
    struct_hash = keccak(
        _struct_typehash
        + pad_address(address_field)
        + ts_hash
        + pad_uint256(0)  # nonce = 0
        + _fixed_msg_hash
    )
    return keccak(b'\x19\x01' + _auth_domain_sep + struct_hash)

# ── Gnosis Safe wrapping ──────────────────────────────────────────────────────
_safe_dom_typehash = keccak(text="EIP712Domain(uint256 chainId,address verifyingContract)")
_safe_msg_typehash = keccak(text="SafeMessage(bytes message)")
_safe_domain_sep   = keccak(_safe_dom_typehash + pad_uint256(CHAIN_ID) + pad_address(PROXY_ADDRESS))

def safe_wrap(raw_hash: bytes) -> bytes:
    safe_msg_hash = keccak(_safe_msg_typehash + keccak(raw_hash))
    return keccak(b'\x19\x01' + _safe_domain_sep + safe_msg_hash)

def l1_headers(poly_address: str, digest: bytes, ts: int) -> dict:
    sig = Account._sign_hash(digest, PRIVATE_KEY)
    return {
        "POLY_ADDRESS":   poly_address,
        "POLY_SIGNATURE": "0x" + sig.signature.hex(),
        "POLY_TIMESTAMP": str(ts),
        "POLY_NONCE":     "0",
    }

def print_creds(data: dict):
    key = data.get("apiKey")    or data.get("api_key")
    sec = data.get("secret")    or data.get("api_secret")
    pw  = data.get("passphrase") or data.get("api_passphrase")
    if key:
        print(f"\n{'='*55}")
        print(f"  POLY_API_KEY={key}")
        print(f"  POLY_API_SECRET={sec}")
        print(f"  POLY_API_PASSPHRASE={pw}")
        print(f"{'='*55}")
        return True
    return False

ts = int(time.time())
found = False

# ── Phase 1: DERIVE existing key (correct endpoint) ──────────────────────────
# GET /auth/derive-api-key — retrieves the already-registered key for this wallet.
# This is what py_clob_client_v2.derive_api_key() actually calls.
print("\n── Phase 1: Derive existing key (EOA L1 → GET /auth/derive-api-key) ──")
hdrs = l1_headers(EOA_ADDRESS, compute_raw_auth_hash(ts, EOA_ADDRESS), ts)
resp = requests.get(f"{HOST}/auth/derive-api-key", headers=hdrs, proxies=proxies, timeout=10)
print(f"  HTTP {resp.status_code}: {resp.text}")
if resp.status_code == 200:
    found = print_creds(resp.json())

# ── Phase 2: List all keys ─────────────────────────────────────────────────────
print("\n── Phase 2: List all API keys (GET /auth/api-keys) ──")
hdrs = l1_headers(EOA_ADDRESS, compute_raw_auth_hash(ts, EOA_ADDRESS), ts)
resp = requests.get(f"{HOST}/auth/api-keys", headers=hdrs, proxies=proxies, timeout=10)
print(f"  HTTP {resp.status_code}: {resp.text[:500]}")

# ── Phase 3: Create with standard EOA flow ────────────────────────────────────
print("\n── Phase 3: Create (POST /auth/api-key, EOA L1) ──")
hdrs = l1_headers(EOA_ADDRESS, compute_raw_auth_hash(ts, EOA_ADDRESS), ts)
resp = requests.post(f"{HOST}/auth/api-key", headers=hdrs, proxies=proxies, timeout=10)
print(f"  HTTP {resp.status_code}: {resp.text}")
if resp.status_code == 200:
    found = print_creds(resp.json())

# ── Phase 4: Create with safe-wrapped L1 (Safe address) ──────────────────────
print("\n── Phase 4: Create (POST /auth/api-key, Safe-addr + safe-wrapped L1) ──")
hdrs = l1_headers(PROXY_ADDRESS, safe_wrap(compute_raw_auth_hash(ts, PROXY_ADDRESS)), ts)
resp = requests.post(f"{HOST}/auth/api-key", headers=hdrs, proxies=proxies, timeout=10)
print(f"  HTTP {resp.status_code}: {resp.text}")
if resp.status_code == 200:
    found = print_creds(resp.json())

if not found:
    print("\n[RESULT] No working method found.")
    print("  → Log into app.polymarket.com with the Gnosis Safe, go to Settings → API Keys,")
    print("    and export the POLY_API_KEY / POLY_API_SECRET / POLY_API_PASSPHRASE.")
