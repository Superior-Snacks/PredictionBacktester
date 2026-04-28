"""
Compare EIP-712 signatures: manually sign the EXACT same order that C# produced
to see if the signatures match. This isolates whether the issue is in Nethereum's
EIP-712 signing or something else.

Uses the exact values from the C# debug output.
"""
import os
from eth_account import Account
from eip712_structs import EIP712Struct, Address, Uint, make_domain
from eth_utils import keccak

private_key = os.environ["POLY_PRIVATE_KEY"]

# EIP-712 Domain (must match C# exactly)
domain = make_domain(
    name="Polymarket CTF Exchange",
    version="1",
    chainId=137,
    verifyingContract="0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",  # CTF Exchange
)

# Order struct (must match C# PolymarketOrder exactly)
class Order(EIP712Struct):
    salt = Uint(256)
    maker = Address()
    signer = Address()
    taker = Address()
    tokenId = Uint(256)
    makerAmount = Uint(256)
    takerAmount = Uint(256)
    expiration = Uint(256)
    nonce = Uint(256)
    feeRateBps = Uint(256)
    side = Uint(8)
    signatureType = Uint(8)

# Use the EXACT values from C# debug output
order = Order(
    salt=8554568953,
    maker="0xdb7cd3dc6654e7692486D6F3487080bF3bFDf5C4",
    signer="0xf786a3DAe390d2342886ABA75e61529F75E953D7",
    taker="0x0000000000000000000000000000000000000000",
    tokenId=12262487801408199826925365097268129030954888398184175579680606275682448865163,
    makerAmount=5000000,
    takerAmount=62500000,
    expiration=0,
    nonce=0,
    feeRateBps=0,
    side=0,
    signatureType=0,  # Test with 0 first
)

# Compute struct hash
signable = order.signable_bytes(domain=domain)
struct_hash = keccak(signable)

print(f"=== signatureType=0 ===")
print(f"Struct hash: 0x{struct_hash.hex()}")

# Sign it
sig = Account._sign_hash(struct_hash, private_key)
signature = "0x" + sig.signature.hex()
print(f"Signature:   {signature}")
print(f"C# produced: 0x7920f6039c26831647a473a2fd16495d7d426ee5532df4e104717d2568f1b64a4c17accdbecf447d058f6d20ec7fa1823a3c8761837787d927ca2ee5c65250401c")
print(f"Match: {signature == '0x7920f6039c26831647a473a2fd16495d7d426ee5532df4e104717d2568f1b64a4c17accdbecf447d058f6d20ec7fa1823a3c8761837787d927ca2ee5c65250401c'}")

# Now try signatureType=1 (POLY_PROXY)
order2 = Order(
    salt=8554568953,
    maker="0xdb7cd3dc6654e7692486D6F3487080bF3bFDf5C4",
    signer="0xf786a3DAe390d2342886ABA75e61529F75E953D7",
    taker="0x0000000000000000000000000000000000000000",
    tokenId=12262487801408199826925365097268129030954888398184175579680606275682448865163,
    makerAmount=5000000,
    takerAmount=62500000,
    expiration=0,
    nonce=0,
    feeRateBps=0,
    side=0,
    signatureType=1,
)

signable2 = order2.signable_bytes(domain=domain)
struct_hash2 = keccak(signable2)

print(f"\n=== signatureType=1 ===")
print(f"Struct hash: 0x{struct_hash2.hex()}")

sig2 = Account._sign_hash(struct_hash2, private_key)
signature2 = "0x" + sig2.signature.hex()
print(f"Signature:   {signature2}")

# Also print the type hash for verification
print(f"\n=== Type hash ===")
type_hash = Order.type_hash()
print(f"Order type hash: 0x{type_hash.hex()}")
print(f"Expected:        0x5bab42a849a93a99b18c1e5a6a1cd8ee7bfdd5e20cce6e0e0dd9ee0773de2847")

# Print domain separator
domain_bytes = domain.hash_struct()
print(f"\nDomain separator: 0x{domain_bytes.hex()}")
