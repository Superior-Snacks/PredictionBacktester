"""
One-time setup: Approve the CTF Exchange and NegRisk Exchange to spend
your EOA's USDC and conditional tokens. This is required for EOA mode trading.

Prerequisites:
  - USDC on your EOA (withdraw from Polymarket first)
  - A small amount of MATIC on your EOA for gas (~0.1 MATIC)

Usage: python approve_eoa.py
"""
import os
from web3 import Web3

rpc_url = os.environ.get("POLY_RPC_URL", "https://polygon-rpc.com")
private_key = os.environ["POLY_PRIVATE_KEY"]

w3 = Web3(Web3.HTTPProvider(rpc_url))
account = w3.eth.account.from_key(private_key)
eoa = account.address

print(f"EOA: {eoa}")

# Check balances first
matic_bal = w3.eth.get_balance(Web3.to_checksum_address(eoa))
print(f"MATIC balance: {w3.from_wei(matic_bal, 'ether'):.4f}")

USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

MAX_UINT256 = 2**256 - 1

erc20_abi = [
    {"constant": True, "inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
]

erc1155_abi = [
    {"constant": True, "inputs": [{"name": "account", "type": "address"}, {"name": "operator", "type": "address"}], "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}], "name": "setApprovalForAll", "outputs": [], "type": "function"},
]

usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=erc20_abi)
ctf_token = w3.eth.contract(address=Web3.to_checksum_address(CTF_CONTRACT), abi=erc1155_abi)

usdc_bal = usdc.functions.balanceOf(Web3.to_checksum_address(eoa)).call()
print(f"USDC balance:  ${usdc_bal / 1e6:.2f}")

if matic_bal < w3.to_wei(0.01, 'ether'):
    print(f"\nERROR: Not enough MATIC for gas. Send at least 0.1 MATIC to {eoa}")
    exit(1)

if usdc_bal == 0:
    print(f"\nWARNING: No USDC on EOA. Withdraw from Polymarket to {eoa} first.")
    print("Continuing with approvals anyway (they'll be ready when USDC arrives).\n")

# Check and set USDC allowances
approvals_needed = []

for name, exchange in [("CTF Exchange", CTF_EXCHANGE), ("NegRisk Exchange", NEG_RISK_EXCHANGE)]:
    allowance = usdc.functions.allowance(Web3.to_checksum_address(eoa), Web3.to_checksum_address(exchange)).call()
    if allowance < MAX_UINT256 // 2:
        approvals_needed.append(("USDC", name, exchange, usdc))
        print(f"  USDC -> {name}: needs approval")
    else:
        print(f"  USDC -> {name}: already approved")

# Check and set CTF token (ERC-1155) approvals
for name, exchange in [("CTF Exchange", CTF_EXCHANGE), ("NegRisk Exchange", NEG_RISK_EXCHANGE)]:
    approved = ctf_token.functions.isApprovedForAll(Web3.to_checksum_address(eoa), Web3.to_checksum_address(exchange)).call()
    if not approved:
        approvals_needed.append(("CTF Token", name, exchange, ctf_token))
        print(f"  CTF Token -> {name}: needs approval")
    else:
        print(f"  CTF Token -> {name}: already approved")

if not approvals_needed:
    print("\nAll approvals are already set! You're ready to trade in EOA mode.")
    exit(0)

print(f"\n{len(approvals_needed)} approval(s) needed. Each costs a small amount of MATIC gas.")
confirm = input("Proceed? (y/n): ").strip().lower()
if confirm != 'y':
    print("Cancelled.")
    exit(0)

nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(eoa))

for token_name, exchange_name, exchange_addr, contract in approvals_needed:
    print(f"\nApproving {token_name} for {exchange_name}...")
    try:
        if token_name == "USDC":
            tx = contract.functions.approve(
                Web3.to_checksum_address(exchange_addr),
                MAX_UINT256
            ).build_transaction({
                'from': Web3.to_checksum_address(eoa),
                'nonce': nonce,
                'gas': 60000,
                'gasPrice': w3.eth.gas_price,
            })
        else:  # ERC-1155
            tx = contract.functions.setApprovalForAll(
                Web3.to_checksum_address(exchange_addr),
                True
            ).build_transaction({
                'from': Web3.to_checksum_address(eoa),
                'nonce': nonce,
                'gas': 60000,
                'gasPrice': w3.eth.gas_price,
            })

        signed_tx = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        print(f"  TX sent: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        print(f"  Confirmed! Gas used: {receipt.gasUsed}")
        nonce += 1
    except Exception as e:
        print(f"  Failed: {e}")

print("\nDone! Run check_proxy.py again to verify all approvals are set.")
print("Then start the C# bot — it will use EOA mode (signatureType=0).")
