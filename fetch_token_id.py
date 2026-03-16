import requests
import json
import sys

def search_markets(query: str):
    """Search Polymarket events API for markets matching a query string."""
    url = "https://gamma-api.polymarket.com/events"
    query_lower = query.lower()
    matches = []
    limit = 100
    total_events = 0

    try:
        # Paginate through ALL events
        offset = 0
        while True:
            params = {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
            }
            response = requests.get(url, params=params)
            response.raise_for_status()
            events = response.json()

            if not events:
                break

            total_events += len(events)

            for event in events:
                for market in event.get("markets", []):
                    question = market.get("question", "")
                    slug = market.get("slug", "")
                    description = market.get("description", "")
                    group_title = event.get("title", "")

                    searchable = f"{question} {slug} {description} {group_title}".lower()
                    if query_lower in searchable:
                        matches.append(market)

            print(f"  Scanned {total_events} events, {len(matches)} matches so far...", end="\r")
            offset += limit

        print(f"  Scanned {total_events} events total.                    ")

        if not matches:
            print(f"No markets found matching '{query}'.")
            return

        print(f"  Found {len(matches)} matching market(s).\n")

        for i, market in enumerate(matches):
            question = market.get("question", "?")
            token_ids_raw = market.get("clobTokenIds")
            if isinstance(token_ids_raw, str):
                token_ids = json.loads(token_ids_raw)
            else:
                token_ids = token_ids_raw or []

            end_date = market.get("endDate", "N/A")
            if end_date and len(end_date) > 16:
                end_date = end_date[:16]

            print(f"--- Market {i+1} ---")
            print(f"  Question:  {question}")
            print(f"  Slug:      {market.get('slug', 'N/A')}")
            print(f"  End Date:  {end_date}")
            if token_ids and len(token_ids) >= 2:
                print(f"  YES Token: {token_ids[0]}")
                print(f"  NO Token:  {token_ids[1]}")
            elif token_ids:
                print(f"  Token IDs: {token_ids}")
            print()

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python fetch_token_id.py <search query>")
        print("Example: python fetch_token_id.py 'Bitcoin'")
        print("Example: python fetch_token_id.py 'Lakers'")
        print("Example: python fetch_token_id.py '5 minute'")
    else:
        query = " ".join(sys.argv[1:])
        search_markets(query)
