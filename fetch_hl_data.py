#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from pathlib import Path
import requests

API_URL = "https://api.hyperliquid.xyz/info"

def post_request(payload: dict, retries=5, backoff=2.0) -> dict:
    for i in range(retries):
        try:
            response = requests.post(API_URL, json=payload, headers={"Content-Type": "application/json"})
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:
                sleep_time = backoff * (2 ** i)
                print(f"Rate limited (429). Retrying in {sleep_time:.2f} seconds...", file=sys.stderr)
                time.sleep(sleep_time)
            else:
                print(f"HTTP Error {response.status_code}: {response.text}", file=sys.stderr)
                time.sleep(backoff)
        except Exception as e:
            print(f"Connection error: {e}. Retrying...", file=sys.stderr)
            time.sleep(backoff)
    raise Exception(f"Failed to fetch data from Hyperliquid API after {retries} retries.")

def get_spot_index(coin: str) -> str:
    """Finds the spot universe coin name (e.g., '@107' or 'PURR/USDC') for `{coin}`."""
    print("Fetching spot metadata...")
    data = post_request({"type": "spotMeta"})
    
    tokens = data.get("tokens", [])
    universe = data.get("universe", [])
    
    # 1. Find the token index for the requested coin
    coin_token = next((t for t in tokens if t.get("name", "").upper() == coin.upper()), None)
    if not coin_token:
        available_tokens = [t.get("name") for t in tokens]
        raise ValueError(f"Could not find token '{coin}' in spotMeta tokens. Available: {available_tokens[:30]}...")
        
    token_idx = coin_token["index"]
    
    # 2. Find the pair in universe that has [token_idx, 0] (0 is USDC)
    pair = next((p for p in universe if p.get("tokens", [])[0] == token_idx), None)
    if not pair:
        raise ValueError(f"Could not find spot pair in universe for token '{coin}' (index {token_idx})")
        
    pair_name = pair.get("name")
    print(f"Found spot pair for '{coin}' (token index {token_idx}): name='{pair_name}', index={pair.get('index')}")
    return pair_name

def fetch_candles(coin_id: str, interval: str, start_ms: int, end_ms: int) -> list:
    """Fetches candle history with backward pagination."""
    candles = []
    current_end = end_ms
    
    print(f"Fetching candles for {coin_id} ({interval})...")
    
    while current_end > start_ms:
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin_id,
                "interval": interval,
                "startTime": start_ms,
                "endTime": current_end
            }
        }
        
        batch = post_request(payload)
        if not batch:
            break
            
        # Ensure batch is sorted by time
        batch = sorted(batch, key=lambda x: x["t"])
        
        # Filter candles within our range
        new_candles = [c for c in batch if c["t"] <= current_end and c["t"] >= start_ms]
        if not new_candles:
            break
            
        candles.extend(new_candles)
        
        # Move end time backward based on the oldest candle's time
        first_t = new_candles[0]["t"]
        if first_t >= current_end:
            # Prevent infinite loop if timestamp does not advance backward
            current_end -= 60000
        else:
            current_end = first_t - 1000  # Move end time before the oldest candle in this batch
            
        print(f"  Downloaded {len(candles)} candles so far (current oldest timestamp: {first_t})...")
        time.sleep(0.2)  # Mild rate-limiting friendly delay
        
    # Final deduplication by timestamp
    seen = set()
    unique_candles = []
    for c in candles:
        if c["t"] not in seen:
            seen.add(c["t"])
            unique_candles.append(c)
            
    return sorted(unique_candles, key=lambda x: x["t"])

def fetch_funding_history(coin: str, start_ms: int) -> list:
    """Fetches funding rate history with pagination."""
    funding = []
    current_start = start_ms
    print(f"Fetching funding history for {coin}...")
    
    while True:
        payload = {
            "type": "fundingHistory",
            "coin": coin,
            "startTime": current_start
        }
        
        batch = post_request(payload)
        if not batch:
            break
            
        # Sort batch by time
        batch = sorted(batch, key=lambda x: x["time"])
        
        # Filter out old timestamps
        new_funding = [f for f in batch if f["time"] >= current_start]
        if not new_funding:
            break
            
        funding.extend(new_funding)
        
        # Move start time forward
        last_time = new_funding[-1]["time"]
        if last_time <= current_start:
            current_start += 3600000  # Advance 1 hour
        else:
            current_start = last_time + 1000
            
        print(f"  Downloaded {len(funding)} funding records so far...")
        time.sleep(0.2)
        
        # Hyperliquid returns funding history up to the current time, so if batch is small or we reached current time, stop
        if len(batch) < 100:  # If we get fewer than a typical page size, we are likely done
            break
            
    # Deduplicate
    seen = set()
    unique_funding = []
    for f in funding:
        if f["time"] not in seen:
            seen.add(f["time"])
            unique_funding.append(f)
            
    return sorted(unique_funding, key=lambda x: x["time"])

def main():
    parser = argparse.ArgumentParser(description="Hyperliquid Historical Data Collector")
    parser.add_argument("--coin", type=str, required=True, help="Coin symbol (e.g. HYPE)")
    parser.add_argument("--days", type=int, default=30, help="Number of days of history to fetch")
    parser.add_argument("--interval", type=str, default="1m", help="Candle interval (e.g. 1m, 5m, 1h)")
    parser.add_argument("--output-dir", type=str, default="data", help="Output directory")
    args = parser.parse_args()
    
    output_path = Path(args.output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Calculate timestamps
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (args.days * 24 * 60 * 60 * 1000)
    
    print(f"Target: {args.coin} (last {args.days} days, interval={args.interval})")
    
    # 1. Resolve Spot Coin ID
    try:
        spot_coin_id = get_spot_index(args.coin)
    except Exception as e:
        print(f"Error resolving spot index: {e}", file=sys.stderr)
        sys.exit(1)
        
    # 2. Fetch Spot Candles
    try:
        spot_candles = fetch_candles(spot_coin_id, args.interval, start_ms, end_ms)
        spot_file = output_path / f"hl_{args.coin.lower()}_spot.json"
        spot_file.write_text(json.dumps(spot_candles, indent=2))
        print(f"Successfully saved {len(spot_candles)} spot candles to {spot_file}")
    except Exception as e:
        print(f"Failed to fetch spot candles: {e}", file=sys.stderr)
        
    # 3. Fetch Perp Candles
    try:
        perp_candles = fetch_candles(args.coin, args.interval, start_ms, end_ms)
        perp_file = output_path / f"hl_{args.coin.lower()}_perp.json"
        perp_file.write_text(json.dumps(perp_candles, indent=2))
        print(f"Successfully saved {len(perp_candles)} perp candles to {perp_file}")
    except Exception as e:
        print(f"Failed to fetch perp candles: {e}", file=sys.stderr)
        
    # 4. Fetch Funding History
    try:
        funding_data = fetch_funding_history(args.coin, start_ms)
        funding_file = output_path / f"hl_{args.coin.lower()}_funding.json"
        funding_file.write_text(json.dumps(funding_data, indent=2))
        print(f"Successfully saved {len(funding_data)} funding records to {funding_file}")
    except Exception as e:
        print(f"Failed to fetch funding history: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
