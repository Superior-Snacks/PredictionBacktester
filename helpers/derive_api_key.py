"""
Re-derive (or create) Polymarket CLOB API credentials for a POLY_GNOSIS_SAFE wallet.

Run from the Linux server (needs unrestricted or proxied access to clob.polymarket.com).

Usage:
  export POLY_PRIVATE_KEY="0x..."
  export POLY_PROXY_ADDRESS="0x595651219C3f6cBB5eb37fae5cEd4C6Caf4907CB"
  export POLY_SOCKS_PROXY="socks5://127.0.0.1:8081"   # same proxy the bot uses
  python3 helpers/derive_api_key.py

Output:
  Prints new POLY_API_KEY / POLY_API_SECRET / POLY_API_PASSPHRASE.
  Paste those into your .env file to replace the current values.
"""
import os, time, requests

PRIVATE_KEY   = os.environ["POLY_PRIVATE_KEY"]
PROXY_ADDRESS = os.environ["POLY_PROXY_ADDRESS"]
SOCKS_PROXY   = os.environ.get("POLY_SOCKS_PROXY", "")

HOST = "https://clob.polymarket.com"

from py_clob_client_v2.signer import Signer
from py_clob_client_v2.signing.eip712 import sign_clob_auth_message

CHAIN_ID = 137
signer   = Signer(PRIVATE_KEY, CHAIN_ID)
print(f"EOA (signer):  {signer.address()}")
print(f"Proxy (funder): {PROXY_ADDRESS}")

proxies = {"https": SOCKS_PROXY, "http": SOCKS_PROXY} if SOCKS_PROXY else None

def l1_headers(poly_address: str) -> dict:
    ts  = int(time.time())
    sig = sign_clob_auth_message(signer, ts, 0)
    return {
        "POLY_ADDRESS":   poly_address,
        "POLY_SIGNATURE": sig,
        "POLY_TIMESTAMP": str(ts),
        "POLY_NONCE":     "0",
    }

def try_derive(label: str, address: str) -> bool:
    print(f"\n── Trying POLY_ADDRESS={label} ({address}) ──")
    for method, path in [("GET", f"{HOST}/auth/api-key"), ("POST", f"{HOST}/auth/api-key")]:
        try:
            hdrs = l1_headers(address)
            if method == "GET":
                resp = requests.get(path, headers=hdrs, proxies=proxies, timeout=10)
            else:
                resp = requests.post(path, headers=hdrs, proxies=proxies, timeout=10)
            print(f"  {method} /auth/api-key → HTTP {resp.status_code}: {resp.text}")
            if resp.status_code == 200:
                data = resp.json()
                key  = data.get("apiKey")  or data.get("api_key")
                sec  = data.get("secret")  or data.get("api_secret")
                pw   = data.get("passphrase") or data.get("api_passphrase")
                if key:
                    print(f"\n=== Credentials for POLY_ADDRESS={label} ===")
                    print(f"POLY_API_KEY={key}")
                    print(f"POLY_API_SECRET={sec}")
                    print(f"POLY_API_PASSPHRASE={pw}")
                    return True
        except Exception as e:
            print(f"  {method} error: {e}")
    return False

# Try both the EOA address and the proxy/Safe address as POLY_ADDRESS.
# For POLY_PROXY wallets the EOA address is correct.
# For POLY_GNOSIS_SAFE wallets Polymarket may expect the Safe address.
found  = try_derive("EOA",   signer.address())
found |= try_derive("Proxy", PROXY_ADDRESS)

if not found:
    print("\n[RESULT] Neither address worked. The CLOB may require a different registration flow.")
    print("  → Try logging into app.polymarket.com and re-exporting credentials from Settings.")
