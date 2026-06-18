#!/usr/bin/env python3
"""Read-only live basis logger for Hyperliquid spot-perp pairs.

NO private key, NO orders — pure public L2 data. For every coin that has BOTH a
spot (USDC-quoted) and a perp market, it periodically computes the REAL executable
basis from the order book and appends a CSV row. Run it for ~24h, then analyze the
CSV to see whether any real, cost-covering basis ever appears on live bid/ask.

Entry basis  = (perp_bid - spot_ask) / spot_ask * 100   (buy spot @ask, short perp @bid)
net_edge     = entry_basis - round_trip_cost%            (>0 => a round trip would profit)

Round-trip cost (both legs, in & out), Hyperliquid base tier:
  taker = 2*(0.070% spot + 0.035% perp) = 0.210%
  maker = 2*(0.040% spot + 0.010% perp) = 0.100%
"""
import argparse
import csv
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from hyperliquid.utils import constants
from hyperliquid.info import Info
from hyperliquid.websocket_manager import WebsocketManager

RT_COST_TAKER = 2 * (0.070 + 0.035)  # % of notional
RT_COST_MAKER = 2 * (0.040 + 0.010)

lock = threading.Lock()
books = {}  # coin -> {"spot": {...}, "perp": {...}}


def vwap(levels, target_usd):
    """Volume-weighted avg price to fill target_usd, walking the book. 0 if too thin."""
    acc_usd = acc_qty = 0.0
    for lv in levels:
        px = float(lv["px"]); sz = float(lv["sz"]); usd = px * sz
        if acc_usd + usd >= target_usd:
            acc_qty += (target_usd - acc_usd) / px
            acc_usd = target_usd
            break
        acc_qty += sz; acc_usd += usd
    if acc_usd < target_usd:
        return 0.0
    return acc_usd / acc_qty


def make_handler(coin, leg):
    def h(msg):
        data = msg.get("data")
        if data and "levels" in data:
            with lock:
                books[coin][leg]["bids"] = data["levels"][0]
                books[coin][leg]["asks"] = data["levels"][1]
                books[coin][leg]["t"] = time.time()
    return h


def discover(info):
    """Returns {coin: spot_pair_name} for coins with both perp and USDC spot,
    keeping only pairs where spot and perp track the SAME underlying.

    A shared ticker is not enough: many HL spot tokens (BERA, PUMP, TRUMP, ...)
    are unrelated meme tokens whose price is orders of magnitude away from the
    perp. We require the spot mid and perp mid to be within 5% of each other.
    """
    meta = info.meta()
    sm = info.spot_meta()
    perps = {u["name"].upper() for u in meta["universe"]}
    idx_to_name = {t["index"]: t["name"].upper() for t in sm["tokens"]}
    pairs = {}
    for p in sm["universe"]:
        tks = p.get("tokens", [])
        if len(tks) == 2 and tks[1] == 0:  # quoted in USDC (token index 0)
            base = idx_to_name.get(tks[0])
            if base:
                pairs[base] = p["name"]

    candidates = sorted(perps & set(pairs.keys()))
    mids = info.all_mids()
    valid, dropped = {}, []
    for c in candidates:
        spot_name = pairs[c]
        try:
            pm = float(mids[c]); sm_mid = float(mids[spot_name])
        except (KeyError, ValueError, TypeError):
            dropped.append((c, "no mid"))
            continue
        if sm_mid <= 0 or not (0.95 <= pm / sm_mid <= 1.05):
            dropped.append((c, f"price mismatch perp={pm} spot={sm_mid}"))
            continue
        valid[c] = spot_name
    if dropped:
        print("Dropped (ticker shared but NOT same asset / no mid):")
        for c, why in dropped:
            print(f"  {c}: {why}")
    return valid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size-usd", type=float, default=35.0, help="Target fill size per leg")
    ap.add_argument("--interval", type=float, default=5.0, help="Seconds between samples")
    ap.add_argument("--summary-sec", type=float, default=300.0, help="Seconds between live summaries")
    ap.add_argument("--out", type=str, default="basis_log.csv")
    ap.add_argument("--coins", type=str, help="Comma list to restrict (default: all spot+perp coins)")
    ap.add_argument("--testnet", action="store_true")
    args = ap.parse_args()

    base_url = constants.TESTNET_API_URL if args.testnet else constants.MAINNET_API_URL
    info = Info(base_url, skip_ws=True)

    universe = discover(info)
    if args.coins:
        want = {c.strip().upper() for c in args.coins.split(",")}
        universe = {c: v for c, v in universe.items() if c in want}
    if not universe:
        print("No spot+perp coins found.", file=sys.stderr); sys.exit(1)

    print(f"Logging {len(universe)} spot+perp pairs: {', '.join(universe)}")
    print(f"Round-trip cost: taker {RT_COST_TAKER:.3f}% | maker {RT_COST_MAKER:.3f}%")
    print(f"Sampling every {args.interval}s, size ${args.size_usd}/leg -> {args.out}")

    ws = WebsocketManager(base_url)
    ws.start()
    for coin, spot_name in universe.items():
        books[coin] = {"spot": {"bids": [], "asks": [], "t": 0.0},
                       "perp": {"bids": [], "asks": [], "t": 0.0}}
        ws.subscribe({"type": "l2Book", "coin": spot_name}, make_handler(coin, "spot"))
        ws.subscribe({"type": "l2Book", "coin": coin}, make_handler(coin, "perp"))

    out_path = Path(args.out)
    new_file = not out_path.exists()
    f = out_path.open("a", newline="")
    w = csv.writer(f)
    if new_file:
        w.writerow(["ts_iso", "coin", "size_usd", "spot_ask", "perp_bid", "entry_basis_pct",
                    "spot_bid", "perp_ask", "exit_basis_pct", "spot_spread_pct", "perp_spread_pct",
                    "net_edge_taker_pct", "net_edge_maker_pct"])

    stats = {c: {"n": 0, "entry": [], "spot_spr": [], "perp_spr": [],
                 "taker_pos": 0, "maker_pos": 0} for c in universe}
    last_summary = time.time()
    started = time.time()

    def print_summary():
        elapsed_h = (time.time() - started) / 3600
        print(f"\n===== SUMMARY @ {elapsed_h:.2f}h =====")
        print(f"{'COIN':<7}|{'n':>5}|{'maxEntry%':>9}|{'p95Entry%':>9}|{'medSpotSpr%':>11}|"
              f"{'medPerpSpr%':>11}|{'taker+%':>8}|{'maker+%':>8}")
        for c, s in stats.items():
            if s["n"] == 0:
                print(f"{c:<7}|{0:>5}|{'-':>9}|{'-':>9}|{'-':>11}|{'-':>11}|{'-':>8}|{'-':>8}")
                continue
            mx = max(s["entry"]); p95 = sorted(s["entry"])[int(0.95 * (len(s["entry"]) - 1))]
            msp = statistics.median(s["spot_spr"]); mpp = statistics.median(s["perp_spr"])
            tp = 100 * s["taker_pos"] / s["n"]; mp = 100 * s["maker_pos"] / s["n"]
            print(f"{c:<7}|{s['n']:>5}|{mx:>9.3f}|{p95:>9.3f}|{msp:>11.3f}|{mpp:>11.3f}|"
                  f"{tp:>7.1f}%|{mp:>7.1f}%")
        print("(taker+% / maker+% = share of samples where net_edge > 0)\n")

    print("Logging... Ctrl+C to stop.")
    try:
        while True:
            time.sleep(args.interval)
            now = time.time()
            ts_iso = datetime.now(timezone.utc).isoformat()
            with lock:
                snap = {c: {leg: {"bids": list(b[leg]["bids"]), "asks": list(b[leg]["asks"]),
                                  "t": b[leg]["t"]} for leg in ("spot", "perp")}
                        for c, b in books.items()}
            for coin, b in snap.items():
                sp, pp = b["spot"], b["perp"]
                if now - sp["t"] > 15 or now - pp["t"] > 15:
                    continue
                if not (sp["asks"] and sp["bids"] and pp["asks"] and pp["bids"]):
                    continue
                spot_ask = vwap(sp["asks"], args.size_usd)
                perp_bid = vwap(pp["bids"], args.size_usd)
                spot_bid = vwap(sp["bids"], args.size_usd)
                perp_ask = vwap(pp["asks"], args.size_usd)
                if min(spot_ask, perp_bid, spot_bid, perp_ask) == 0.0:
                    continue
                entry_basis = (perp_bid - spot_ask) / spot_ask * 100
                exit_basis = (perp_ask - spot_bid) / spot_bid * 100
                spot_spr = (float(sp["asks"][0]["px"]) - float(sp["bids"][0]["px"])) / float(sp["bids"][0]["px"]) * 100
                perp_spr = (float(pp["asks"][0]["px"]) - float(pp["bids"][0]["px"])) / float(pp["bids"][0]["px"]) * 100
                net_taker = entry_basis - RT_COST_TAKER
                net_maker = entry_basis - RT_COST_MAKER
                w.writerow([ts_iso, coin, args.size_usd, f"{spot_ask:.8f}", f"{perp_bid:.8f}",
                            f"{entry_basis:.4f}", f"{spot_bid:.8f}", f"{perp_ask:.8f}",
                            f"{exit_basis:.4f}", f"{spot_spr:.4f}", f"{perp_spr:.4f}",
                            f"{net_taker:.4f}", f"{net_maker:.4f}"])
                s = stats[coin]
                s["n"] += 1; s["entry"].append(entry_basis)
                s["spot_spr"].append(spot_spr); s["perp_spr"].append(perp_spr)
                if net_taker > 0: s["taker_pos"] += 1
                if net_maker > 0: s["maker_pos"] += 1
            f.flush()
            if now - last_summary >= args.summary_sec:
                print_summary(); last_summary = now
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        print_summary()
        f.close()
        ws.stop()
        print(f"Saved CSV to {out_path}")


if __name__ == "__main__":
    main()
