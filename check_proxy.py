"""
Check if your EOA is an authorized operator for your proxy wallet on Polymarket.
Also check USDC balance and allowance for both wallets.
"""
import os
from web3 import Web3

rpc_url = os.environ.get("POLY_RPC_URL", "https://polygon-rpc.com")
eoa = os.environ["POLY_PRIVATE_KEY"]
proxy = os.environ["POLY_PROXY_ADDRESS"]

w3 = Web3(Web3.HTTPProvider(rpc_url))
eoa_address = w3.eth.account.from_key(eoa).address

print(f"EOA:   {eoa_address}")
print(f"Proxy: {proxy}")

# Check USDC balance on both
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

erc20_abi = [
    {"constant": True, "inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]

usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=erc20_abi)

eoa_bal = usdc.functions.balanceOf(Web3.to_checksum_address(eoa_address)).call()
proxy_bal = usdc.functions.balanceOf(Web3.to_checksum_address(proxy)).call()

print(f"\nUSDC Balances:")
print(f"  EOA:   ${eoa_bal / 1e6:.2f}")
print(f"  Proxy: ${proxy_bal / 1e6:.2f}")

# Check allowances for EOA -> CTF Exchange
eoa_allowance_ctf = usdc.functions.allowance(Web3.to_checksum_address(eoa_address), Web3.to_checksum_address(CTF_EXCHANGE)).call()
eoa_allowance_neg = usdc.functions.allowance(Web3.to_checksum_address(eoa_address), Web3.to_checksum_address(NEG_RISK_EXCHANGE)).call()
proxy_allowance_ctf = usdc.functions.allowance(Web3.to_checksum_address(proxy), Web3.to_checksum_address(CTF_EXCHANGE)).call()
proxy_allowance_neg = usdc.functions.allowance(Web3.to_checksum_address(proxy), Web3.to_checksum_address(NEG_RISK_EXCHANGE)).call()

print(f"\nUSDC Allowances for CTF Exchange:")
print(f"  EOA   -> CTF:     ${eoa_allowance_ctf / 1e6:.2f}")
print(f"  EOA   -> NegRisk: ${eoa_allowance_neg / 1e6:.2f}")
print(f"  Proxy -> CTF:     ${proxy_allowance_ctf / 1e6:.2f}")
print(f"  Proxy -> NegRisk: ${proxy_allowance_neg / 1e6:.2f}")

# Check if EOA is an authorized operator on the CTF Exchange for the proxy
# The CTF Exchange has an isOperatorFor or isApprovedForAll check
ctf_exchange_abi = [
    {"constant": True, "inputs": [{"name": "account", "type": "address"}, {"name": "operator", "type": "address"}], "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
]

ctf_token_abi = [
    {"constant": True, "inputs": [{"name": "account", "type": "address"}, {"name": "operator", "type": "address"}], "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
]

# Conditional Token Framework (ERC-1155)
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
ctf_token = w3.eth.contract(address=Web3.to_checksum_address(CTF_CONTRACT), abi=ctf_token_abi)

print(f"\nOperator Approvals (isApprovedForAll on CTF token contract):")
# Check if CTF Exchange is approved to spend proxy's tokens
proxy_approved_ctf = ctf_token.functions.isApprovedForAll(Web3.to_checksum_address(proxy), Web3.to_checksum_address(CTF_EXCHANGE)).call()
proxy_approved_neg = ctf_token.functions.isApprovedForAll(Web3.to_checksum_address(proxy), Web3.to_checksum_address(NEG_RISK_EXCHANGE)).call()
print(f"  Proxy -> CTF Exchange approved:     {proxy_approved_ctf}")
print(f"  Proxy -> NegRisk Exchange approved:  {proxy_approved_neg}")

# Check if EOA is approved as operator for proxy's tokens
eoa_operator_proxy = ctf_token.functions.isApprovedForAll(Web3.to_checksum_address(proxy), Web3.to_checksum_address(eoa_address)).call()
print(f"  Proxy -> EOA operator approved:      {eoa_operator_proxy}")

# Check proxy contract: does it recognize EOA as owner?
# Polymarket proxies are typically Gnosis Safe-compatible or custom minimal proxies
proxy_owner_abi = [
    {"constant": True, "inputs": [], "name": "getOwners", "outputs": [{"name": "", "type": "address[]"}], "type": "function"},
]
try:
    proxy_contract = w3.eth.contract(address=Web3.to_checksum_address(proxy), abi=proxy_owner_abi)
    owners = proxy_contract.functions.getOwners().call()
    print(f"\nProxy wallet owners: {owners}")
    print(f"  EOA is owner: {eoa_address.lower() in [o.lower() for o in owners]}")
except Exception as e:
    print(f"\nProxy is not a Gnosis Safe (getOwners failed: {e})")
    # Try checking if it's a simple proxy with an owner() function
    owner_abi = [{"constant": True, "inputs": [], "name": "owner", "outputs": [{"name": "", "type": "address"}], "type": "function"}]
    try:
        proxy_contract = w3.eth.contract(address=Web3.to_checksum_address(proxy), abi=owner_abi)
        owner = proxy_contract.functions.owner().call()
        print(f"  Proxy owner: {owner}")
        print(f"  EOA is owner: {owner.lower() == eoa_address.lower()}")
    except Exception as e2:
        print(f"  owner() also failed: {e2}")

print(f"\n=== DIAGNOSIS ===")
if proxy_bal > 0 and proxy_allowance_ctf > 0:
    print("Proxy has USDC and CTF Exchange allowance — POLY_PROXY mode should work IF EOA is authorized signer")
if eoa_bal == 0:
    print("EOA has $0 USDC — EOA mode (signatureType=0) will fail with 'not enough balance'")
print(f"\nTo use POLY_PROXY mode, your EOA ({eoa_address}) must be the")
print(f"authorized signer/owner of the proxy ({proxy}).")
print(f"This is set up when you first create your Polymarket account.")
