import os
import time
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, ApiCreds
from py_clob_client.order_builder.constants import BUY, SELL

# Load environment variables (automatically ignores the 'export ' prefixes)
load_dotenv()

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

def run_penny_test(token_id: str):
    # 1. Pulling exact variables from your .env
    private_key = os.getenv("POLY_PRIVATE_KEY")
    proxy_address = os.getenv("POLY_PROXY_ADDRESS")
    
    api_key = os.getenv("POLY_API_KEY")
    api_secret = os.getenv("POLY_API_SECRET")
    api_passphrase = os.getenv("POLY_API_PASSPHRASE")

    # 2. Build the exact credentials object so it doesn't generate new ones
    creds = ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase
    )

    print("Authenticating with Polymarket CLOB using existing API credentials...")
    client = ClobClient(
        host=HOST,
        key=private_key,
        chain_id=CHAIN_ID,
        signature_type=2, # POLY_GNOSIS_SAFE — matches C# production SignatureType = 2
        funder=proxy_address,
        creds=creds
    )
    print("Authenticated successfully.\n")

    # We aggressively cross the spread with FAK to guarantee an instant fill
    buy_price = 0.99
    sell_price = 0.01
    
    # Set to $5.00 to safely bypass Polymarket's minimum order size rules on Tier-1 markets
    size = 5.0  

    print(f"--- STARTING PENNY TEST FOR TOKEN: {token_id} ---")
    
    # 3. Prepare BUY Order
    buy_order_args = OrderArgs(price=buy_price, size=size, side=BUY, token_id=token_id)
    signed_buy = client.create_order(buy_order_args)

    print(f"Firing BUY Order for {size} shares...")
    t0 = time.time()
    
    # POST BUY
    buy_resp = client.post_order(signed_buy, OrderType.FAK)
    
    t1 = time.time()
    buy_latency = (t1 - t0) * 1000
    print(f"-> Buy Response ({buy_latency:.1f}ms): {buy_resp.get('status')} | {buy_resp.get('errorMsg', 'No Errors')}")
    
    # 4. Check if we got an instant off-chain fill
    if buy_resp.get("success") and buy_resp.get("status") == "matched":
        print("\nBuy order filled! Firing SELL Order IMMEDIATELY...")
        
        # POST SELL
        t2 = time.time()
        sell_order_args = OrderArgs(price=sell_price, size=size, side=SELL, token_id=token_id)
        signed_sell = client.create_order(sell_order_args)
        sell_resp = client.post_order(signed_sell, OrderType.FAK)
        
        t3 = time.time()
        sell_latency = (t3 - t2) * 1000
        total_turnaround = (t3 - t1) * 1000
        
        print(f"-> Sell Response ({sell_latency:.1f}ms): {sell_resp.get('status')} | {sell_resp.get('errorMsg', 'No Errors')}")
        print("\n================ TEST COMPLETE ================")
        print(f"Time between Buy Confirmation and Sell Execution: {total_turnaround:.1f}ms")
        
        if sell_resp.get("success") and sell_resp.get("status") == "matched":
             print("✅ VERDICT: SUCCESS. The exchange allowed you to sell instantly. The 5-second Web3 delay is a myth.")
        else:
             print("❌ VERDICT: FAILED. The exchange rejected the immediate sell. The 5-second Web3 delay is real.")
             
    elif buy_resp.get("status") == "delayed":
        print("\n⚠️ VERDICT: HARDCODED DELAY DETECTED.")
        print("Polymarket flagged this specific market with a placement delay (Standard for Sports & Fast Crypto).")
        print("Your order is resting in a queue and will not execute instantly.")
    else:
        print(f"\n❌ VERDICT: ERROR. Buy order failed. Response: {buy_resp}")

if __name__ == "__main__":
    # TODO: Replace with a Token ID from a standard Politics/Economics market
    TARGET_TOKEN_ID = "YOUR_TOKEN_ID_HERE"
    
    run_penny_test(TARGET_TOKEN_ID)