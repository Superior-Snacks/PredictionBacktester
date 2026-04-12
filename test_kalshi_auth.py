"""
Quick auth test — mirrors the exact Python example from Kalshi docs.
Run: python test_kalshi_auth.py
"""
import os, datetime, base64, requests
from urllib.parse import urlparse
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding

def _load_dotenv(*dirs):
    for d in dirs:
        p = os.path.join(d, ".env")
        if not os.path.isfile(p):
            continue
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip(); v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        return

_sd = os.path.dirname(os.path.abspath(__file__))
_load_dotenv(_sd, os.path.dirname(_sd), os.getcwd())

API_KEY_ID       = os.environ.get("KALSHI_API_KEY_ID", "")
PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

# Try both endpoints
ENDPOINTS = [
    ("DEMO", "https://demo-api.kalshi.co/trade-api/v2"),
    ("PROD", "https://api.elections.kalshi.com/trade-api/v2"),
]

def load_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def sign(private_key, timestamp, method, path):
    msg = f"{timestamp}{method}{path}".encode("utf-8")
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256()
    )
    return base64.b64encode(sig).decode("utf-8")

if not API_KEY_ID or not PRIVATE_KEY_PATH:
    print("ERROR: Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
    exit(1)

print(f"Key ID:   {API_KEY_ID}")
print(f"Key file: {PRIVATE_KEY_PATH}\n")

try:
    private_key = load_key(PRIVATE_KEY_PATH)
    print("Key loaded OK\n")
except Exception as e:
    print(f"Key load FAILED: {e}")
    exit(1)

for label, base_url in ENDPOINTS:
    rel_path = "/portfolio/balance"
    full_path = urlparse(base_url + rel_path).path
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    sig = sign(private_key, ts, "GET", full_path)
    print(f"[{label}] Signing: {ts}GET{full_path}")
    try:
        r = requests.get(base_url + rel_path, headers={
            "KALSHI-ACCESS-KEY":       API_KEY_ID,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }, timeout=5)
        if r.status_code == 200:
            bal = r.json().get("balance", "?")
            print(f"[{label}] SUCCESS — balance: ${bal/100:.2f}\n")
        else:
            print(f"[{label}] FAILED — {r.status_code} {r.text[:200]}\n")
    except Exception as e:
        print(f"[{label}] ERROR — {e}\n")
