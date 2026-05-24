"""
Checks if the proxy wallet is a Gnosis Safe and, if so, computes the
safe-wrapped digest that the EOA must sign for isValidSignature to pass.

Run on the Linux server:
  export POLY_PRIVATE_KEY="0x..."
  export POLY_RPC_URL="https://polygon-rpc.com"   # optional
  python helpers/check_safe.py

Outputs:
  - Whether the wallet is a Gnosis Safe
  - The safe-wrapped digest the EOA must sign
  - A test signature over that wrapped digest + ecrecover check
"""
import os
from eth_utils.crypto import keccak
from eth_account import Account

PROXY_ADDR  = "0x595651219C3f6cBB5eb37fae5cEd4C6Caf4907CB"
PRIVATE_KEY = os.environ["POLY_PRIVATE_KEY"]
RPC_URL     = os.environ.get("POLY_RPC_URL", "https://polygon-rpc.com")
CHAIN_ID    = 137

# ── 1. Check if wallet is a Gnosis Safe ──────────────────────────────────────
from web3 import Web3
w3 = Web3(Web3.HTTPProvider(RPC_URL))

safe_version_abi = [{"inputs":[],"name":"VERSION","outputs":[{"type":"string"}],"stateMutability":"view","type":"function"}]
try:
    safe = w3.eth.contract(address=Web3.to_checksum_address(PROXY_ADDR), abi=safe_version_abi)
    version = safe.functions.VERSION().call()
    print(f"Wallet {PROXY_ADDR} IS a Gnosis Safe — version: {version}")
    IS_GNOSIS_SAFE = True
except Exception as e:
    print(f"Wallet {PROXY_ADDR} is NOT a Gnosis Safe (or VERSION() failed): {e}")
    IS_GNOSIS_SAFE = False

if not IS_GNOSIS_SAFE:
    print("\nThis wallet is a POLY_PROXY (not Gnosis Safe). signatureType should be 1.")
    print("If you're getting 'invalid signature', the issue is something else.")
    exit(0)

# ── 2. Compute the Gnosis Safe wrapped digest ─────────────────────────────────
# The safe's isValidSignature(bytes32 _dataHash, bytes sig) checks:
#   messageHash = keccak256(SAFE_MSG_TYPEHASH || keccak256(_dataHash_as_bytes))
#   safeDigest  = keccak256(0x19 || 0x01 || safeDomainSep || messageHash)
#   ecrecover(safeDigest, sig) == owner
#
# From the last C# [EIP712] debug output:
ORDER_DIGEST = bytes.fromhex("801c1d2f3ee8ffd28817f1fe0f974b9678592fa919a3f61c888471cbbcabe3be")

DOMAIN_SEP_TYPEHASH = keccak(text="EIP712Domain(uint256 chainId,address verifyingContract)")
SAFE_MSG_TYPEHASH   = keccak(text="SafeMessage(bytes message)")

def pad_uint256(v): return v.to_bytes(32, 'big')
def pad_address(a):
    raw = bytes.fromhex(a[2:] if a.startswith('0x') else a)
    return b'\x00'*(32-len(raw))+raw

# Safe domain separator
safe_domain_sep = keccak(DOMAIN_SEP_TYPEHASH + pad_uint256(CHAIN_ID) + pad_address(PROXY_ADDR))
print(f"\nSafe domain sep: 0x{safe_domain_sep.hex()}")

# The "message" for SafeMessage is the raw 32 bytes of the order digest
# abi.encode(bytes32) = the 32 bytes directly (bytes32 is padded to 32 already)
# keccak256(abi.encode(_dataHash)) = keccak256(ORDER_DIGEST)
msg_hash = keccak(primitive=ORDER_DIGEST)
safe_msg_hash = keccak(SAFE_MSG_TYPEHASH + msg_hash)
print(f"Safe msg hash:   0x{safe_msg_hash.hex()}")

safe_digest = keccak(b'\x19\x01' + safe_domain_sep + safe_msg_hash)
print(f"Safe digest:     0x{safe_digest.hex()}")
print(f"\n→ The C# EOA must sign THIS digest (not the raw order digest)")

# Sign the safe digest and verify ecrecover
signer = Account.from_key(PRIVATE_KEY)
print(f"\nExpected signer: {signer.address}")
sig = Account._sign_hash(safe_digest, PRIVATE_KEY)
recovered = Account._recover_hash(safe_digest, signature=sig.signature)
print(f"Recovered:       {recovered}")
print(f"Match:           {recovered.lower() == signer.address.lower()}")
print(f"Safe-wrapped signature: 0x{sig.signature.hex()}")
