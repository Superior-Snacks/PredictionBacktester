"""
Quick sanity check: verify that the C# signature from the debug output
recovers to the expected signer address.

Run on the Linux server:
  python check_sig.py

If "Match: True" → signing math is correct, issue is server-side.
If "Match: False" → Nethereum signing has a bug we need to fix.
"""
from eth_utils.crypto import keccak
from eth_account import Account

# Paste values from [ORDER DEBUG] + [EIP712] output here:
DIGEST    = "2abdd45674e50c849333c83585d9caeedc044831732a7a15e19a80a8e923d76e"
SIGNATURE = "727bf19f155b45ad85adb99075774509ff1876cbca9c782130a2a31b708e329c76f1c37902a614ff01066ecf0ffe0a1646fefac4e01a6fd17577ea292ef9a1ac1c"
SIGNER    = "0xf786a3DAe390d2342886ABA75e61529F75E953D7"  # EOA from debug

digest_bytes = bytes.fromhex(DIGEST)
sig_bytes    = bytes.fromhex(SIGNATURE)

recovered = Account._recover_hash(digest_bytes, signature=sig_bytes)
print(f"Digest:    0x{DIGEST}")
print(f"Signature: 0x{SIGNATURE[:16]}...{SIGNATURE[-16:]}")
print(f"Recovered: {recovered}")
print(f"Expected:  {SIGNER}")
print(f"Match: {recovered.lower() == SIGNER.lower()}")

# Also independently verify the domain separator
print("\n--- Domain separator check ---")
def pad_uint256(v): return v.to_bytes(32, 'big')
def pad_address(a): raw = bytes.fromhex(a[2:] if a.startswith('0x') else a); return b'\x00'*(32-len(raw))+raw

dth = keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
nh  = keccak(text="Polymarket CTF Exchange")
vh  = keccak(text="2")
ds  = keccak(dth + nh + vh + pad_uint256(137) + pad_address("0xE111180000d2663C0091e4f400237545B87B996B"))
print(f"DomainSep Python: 0x{ds.hex()}")
print(f"DomainSep C#:     0x3264e159346253e26a64e00b69032db0e7d32f94628de3e6eecb50304d7af3d2")
print(f"DomainSep match:  {ds.hex() == '3264e159346253e26a64e00b69032db0e7d32f94628de3e6eecb50304d7af3d2'}")
