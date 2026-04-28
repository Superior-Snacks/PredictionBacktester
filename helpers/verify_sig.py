"""
Verify EIP-712 signing by computing all intermediate hashes for the exact
same order that C# will produce. Run this AFTER deploying C# to compare
the [EIP712] debug output line by line.

Usage: python verify_sig.py
"""
import os
from eth_utils.crypto import keccak
from eth_account import Account

private_key = os.environ["POLY_PRIVATE_KEY"]

# The EXACT values from the latest C# debug output
SALT = 4823755788
MAKER = "0x595651219C3f6cBB5eb37fae5cEd4C6Caf4907CB"
SIGNER = "0xf786a3DAe390d2342886ABA75e61529F75E953D7"
TAKER = "0x0000000000000000000000000000000000000000"
TOKEN_ID = 53846455285922100369759418522344319743746662149237832282068871274871494415918
MAKER_AMOUNT = 5002500
TAKER_AMOUNT = 5750000
EXPIRATION = 0
NONCE = 0
FEE_RATE_BPS = 0
SIDE = 0       # BUY
SIG_TYPE = 1   # POLY_PROXY

# CTF Exchange (non-negRisk)
EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
CHAIN_ID = 137

def pad_uint256(val):
    return val.to_bytes(32, byteorder='big', signed=False)

def pad_address(addr):
    addr_hex = addr[2:] if addr.startswith("0x") else addr
    raw = bytes.fromhex(addr_hex)
    return b'\x00' * (32 - len(raw)) + raw

# 1. Type hash
type_str = "Order(uint256 salt,address maker,address signer,address taker,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint256 expiration,uint256 nonce,uint256 feeRateBps,uint8 side,uint8 signatureType)"
order_type_hash = keccak(text=type_str)
print(f"[EIP712] typeHash:   0x{order_type_hash.hex()}")

# 2. Domain separator
domain_type_hash = keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
name_hash = keccak(text="Polymarket CTF Exchange")
version_hash = keccak(text="1")

domain_data = domain_type_hash + name_hash + version_hash + pad_uint256(CHAIN_ID) + pad_address(EXCHANGE)
domain_separator = keccak(domain_data)
print(f"[EIP712] domainSep:  0x{domain_separator.hex()}")

# 3. Struct hash
struct_data = (
    order_type_hash
    + pad_uint256(SALT)
    + pad_address(MAKER)
    + pad_address(SIGNER)
    + pad_address(TAKER)
    + pad_uint256(TOKEN_ID)
    + pad_uint256(MAKER_AMOUNT)
    + pad_uint256(TAKER_AMOUNT)
    + pad_uint256(EXPIRATION)
    + pad_uint256(NONCE)
    + pad_uint256(FEE_RATE_BPS)
    + pad_uint256(SIDE)
    + pad_uint256(SIG_TYPE)
)
struct_hash = keccak(struct_data)
print(f"[EIP712] structHash: 0x{struct_hash.hex()}")

# 4. Digest
digest = keccak(b'\x19\x01' + domain_separator + struct_hash)
print(f"[EIP712] digest:     0x{digest.hex()}")

# 5. Sign
sig = Account._sign_hash(digest, private_key)
signature = "0x" + sig.signature.hex()
print(f"[EIP712] signature:  {signature}")

# Also verify using poly_eip712_structs (the actual SDK method)
print("\n=== Verification via poly_eip712_structs ===")
from py_order_utils.model.order import Order as SdkOrder
from poly_eip712_structs import make_domain

domain = make_domain(
    name="Polymarket CTF Exchange",
    version="1",
    chainId=str(CHAIN_ID),
    verifyingContract=EXCHANGE,
)

sdk_order = SdkOrder(
    salt=SALT,
    maker=MAKER,
    signer=SIGNER,
    taker=TAKER,
    tokenId=TOKEN_ID,
    makerAmount=MAKER_AMOUNT,
    takerAmount=TAKER_AMOUNT,
    expiration=EXPIRATION,
    nonce=NONCE,
    feeRateBps=FEE_RATE_BPS,
    side=SIDE,
    signatureType=SIG_TYPE,
)

sdk_struct_hash = keccak(sdk_order.signable_bytes(domain=domain))
sdk_sig = Account._sign_hash(sdk_struct_hash, private_key)
print(f"SDK structHash: 0x{sdk_order.hash_struct().hex()}")
print(f"SDK domainSep:  0x{domain.hash_struct().hex()}")
print(f"SDK digest:     0x{sdk_struct_hash.hex()}")
print(f"SDK signature:  0x{sdk_sig.signature.hex()}")
print(f"\nDigest match: {digest.hex() == sdk_struct_hash.hex()}")
print(f"Sig match:    {signature == '0x' + sdk_sig.signature.hex()}")
