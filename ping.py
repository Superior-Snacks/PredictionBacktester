import time
import statistics
import requests

ENDPOINTS = {
    "Gamma API":     "https://gamma-api.polymarket.com/markets?limit=1",
    "CLOB API":      "https://clob.polymarket.com/time",
    "Data API":      "https://data-api.polymarket.com/markets?limit=1",
}

ROUNDS = 5

def ping_endpoint(name, url, rounds):
    times = []
    for i in range(rounds):
        try:
            start = time.perf_counter()
            r = requests.get(url, timeout=10)
            elapsed_ms = (time.perf_counter() - start) * 1000
            times.append(elapsed_ms)
            status = r.status_code
        except requests.RequestException as e:
            print(f"  Round {i+1}: FAILED ({e})")
            continue
        print(f"  Round {i+1}: {elapsed_ms:.0f}ms (HTTP {status})")
    return times

def main():
    print(f"Pinging Polymarket servers ({ROUNDS} rounds each)\n")

    all_results = {}
    for name, url in ENDPOINTS.items():
        print(f"[{name}] {url}")
        times = ping_endpoint(name, url, ROUNDS)
        all_results[name] = times
        print()

    # Summary
    print("=" * 60)
    print(f"{'Endpoint':<15} {'Min':>8} {'Avg':>8} {'Max':>8} {'Jitter':>8}")
    print("-" * 60)
    for name, times in all_results.items():
        if not times:
            print(f"{name:<15} {'FAILED':>8}")
            continue
        mn = min(times)
        avg = statistics.mean(times)
        mx = max(times)
        jitter = statistics.stdev(times) if len(times) > 1 else 0
        print(f"{name:<15} {mn:>7.0f}ms {avg:>7.0f}ms {mx:>7.0f}ms {jitter:>7.0f}ms")
    print("=" * 60)

if __name__ == "__main__":
    main()
