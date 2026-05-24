"""
Verify V2 EIP-712 signing by computing all intermediate hashes for the exact
same order that C# produces. Run this AFTER the bot logs [EIP712] debug output
and paste those values below to confirm Python and C# produce identical digests.

Usage:
  python verify_sig_v2.py

Then compare the printed [EIP712] lines against the C# [EIP712] debug output.
If they match, the signing is correct and order_version_mismatch is a server issue.
"""
import os
from eth_utils.crypto import keccak
from eth_account import Account

private_key = os.environ["POLY_PRIVATE_KEY"]

# Paste the EXACT values from the latest C# [ORDER DEBUG] output here:
SALT         = 4823755788    # Update with current value
MAKER        = "0x595651219C3f6cBB5eb37fae5cEd4C6Caf4907CB"
SIGNER       = "0xf786a3DAe390d2342886ABA75e61529F75E953D7"
TOKEN_ID     = 53846455285922100369759418522344319743746662149237832282068871274871494415918  # Update
MAKER_AMOUNT = 5002500       # Update
TAKER_AMOUNT = 5750000       # Update
TIMESTAMP    = 1748123456789  # Update: use the timestamp from C# debug output
SIDE         = 0              # 0=BUY, 1=SELL
SIG_TYPE     = 2              # POLY_GNOSIS_SAFE

# V2 exchange contracts (Polygon mainnet)
CTF_EXCHANGE_V2     = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"

EXCHANGE = CTF_EXCHANGE_V2   # Change to NEG_RISK_EXCHANGE_V2 for negRisk markets
CHAIN_ID = 137

# V2 type string: no taker/nonce/feeRateBps/expiration; adds timestamp/metadata/builder
TYPE_STR = "Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,bytes32 builder)"

METADATA = bytes(32)  # zero bytes32
BUILDER  = bytes(32)  # zero bytes32


def pad_uint256(val: int) -> bytes:
    return val.to_bytes(32, byteorder='big', signed=False)

def pad_address(addr: str) -> bytes:
    h = addr[2:] if addr.startswith("0x") else addr
    raw = bytes.fromhex(h)
    return b'\x00' * (32 - len(raw)) + raw

# 1. Type hash (V2)
order_type_hash = keccak(text=TYPE_STR)
print(f"[EIP712] typeHash   =0x{order_type_hash.hex()}")

# 2. Domain separator (version "2", V2 exchange contract)
domain_type_hash = keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
name_hash    = keccak(text="Polymarket CTF Exchange")
version_hash = keccak(text="2")

domain_data = domain_type_hash + name_hash + version_hash + pad_uint256(CHAIN_ID) + pad_address(EXCHANGE)
domain_separator = keccak(domain_data)
print(f"[EIP712] domainSep =0x{domain_separator.hex()}")

# 3. Struct hash (V2 field order)
struct_data = (
    order_type_hash
    + pad_uint256(SALT)
    + pad_address(MAKER)
    + pad_address(SIGNER)
    + pad_uint256(TOKEN_ID)
    + pad_uint256(MAKER_AMOUNT)
    + pad_uint256(TAKER_AMOUNT)
    + pad_uint256(SIDE)
    + pad_uint256(SIG_TYPE)
    + pad_uint256(TIMESTAMP)
    + METADATA    # bytes32 zero
    + BUILDER     # bytes32 zero
)
struct_hash = keccak(struct_data)
print(f"[EIP712] structHash=0x{struct_hash.hex()}")

# 4. Digest
digest = keccak(b'\x19\x01' + domain_separator + struct_hash)
print(f"[EIP712] digest    =0x{digest.hex()}")

# 5. Sign and verify recovery
sig = Account._sign_hash(digest, private_key)
print(f"[EIP712] signature =0x{sig.signature.hex()}")

recovered = Account._recover_hash(digest, signature=sig.signature)
print(f"\nRecovered signer:  {recovered}")
print(f"Expected signer:   {SIGNER}")
print(f"Match: {recovered.lower() == SIGNER.lower()}")
