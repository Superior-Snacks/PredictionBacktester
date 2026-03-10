import requests
import json

def fetch_polymarket_token():
    # The URL provided in your request
    url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=1"
    
    try:
        # Send the GET request
        response = requests.get(url)
        response.raise_for_status() # Check for HTTP errors
        
        # Parse JSON response
        markets = response.json()
        
        if not markets:
            print("No active markets found.")
            return

        market = markets[0]
        
        # Extract details
        question = market.get("question", "No question found")
        
        # clobTokenIds is usually a stringified list in Gamma API, 
        # though sometimes it's returned as a proper list.
        token_ids_raw = market.get("clobTokenIds")
        
        # Handle cases where the API returns a string instead of a list
        if isinstance(token_ids_raw, str):
            token_ids = json.loads(token_ids_raw)
        else:
            token_ids = token_ids_raw

        print(f"--- Market Info ---")
        print(f"Question: {question}")
        if token_ids and len(token_ids) >= 2:
            print(f"YES Token ID: {token_ids[0]}")
            print(f"NO Token ID:  {token_ids[1]}")
        else:
            print(f"Token IDs: {token_ids}")

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    fetch_polymarket_token()