#!/usr/bin/env python3
"""Scan Hyperliquid funding rates across all perps to find delta-neutral carry.

Strategy being measured: short a positive-funding perp, hedge delta with a long
spot of the same asset. While funding is positive, the short EARNS funding hourly.
The edge is real cash flow (not spread noise) — but it only works if funding stays
positive, so we rank by PERSISTENCE (share of hours positive) x magnitude, not by
peak rate. Peak-funding alts flip fast and are the riskiest to hold hedged.

Uses historical fundingHistory (last N days) so we get an answer immediately.
"""
import argparse
import sys
import time
import statistics
import requests

API = "https://api.hyperliquid.xyz/info"

# The 4 HL coins that also have a same-asset USDC spot market (can hedge on HL itself).
HL_SPOT_HEDGEABLE = {"HYPE", "PURR", "AZTEC", "STABLE"}


def post(payload, retries=5, backoff=1.5):
    for i in range(retries):
        try:
            r = requests.post(API, json=payload, headers={"Content-Type": "application/json"}, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(backoff * (2 ** i))
            else:
                time.sleep(backoff)
        except Exception:
            time.sleep(backoff)
    return None


def get_perps():
    meta = post({"type": "meta"})
    return [u["name"] for u in meta["universe"]]


def funding_history(coin, start_ms):
    out = post({"type": "fundingHistory", "coin": coin, "startTime": start_ms})
    return out or []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--notional", type=float, default=300.0,
                    help="Delta-neutral notional per side ($300 ~ $400 deposit at modest leverage)")
    ap.add_argument("--min-positive", type=float, default=70.0,
                    help="Min %% of hours funding must be positive to be a carry candidate")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    start_ms = int(time.time() * 1000) - args.days * 86_400_000
    perps = get_perps()
    print(f"Scanning {len(perps)} perps over last {args.days}d funding history...", file=sys.stderr)

    rows = []
    for i, coin in enumerate(perps):
        hist = funding_history(coin, start_ms)
        if not hist:
            continue
        rates = [float(h["fundingRate"]) for h in hist]  # hourly
        if len(rates) < 24:
            continue
        n = len(rates)
        mean_hr = statistics.mean(rates)
        pos_share = 100.0 * sum(1 for r in rates if r > 0) / n
        ann_pct = mean_hr * 24 * 365 * 100  # annualized %
        # If consistently SHORT-carry, we only earn on positive hours; approximate the
        # realized carry as the average of positive-only rates scaled by positive share.
        realized_hr = mean_hr if mean_hr > 0 else 0.0
        daily_usd = args.notional * realized_hr * 24
        std_hr = statistics.pstdev(rates)
        rows.append({
            "coin": coin, "n": n, "mean_hr": mean_hr, "ann_pct": ann_pct,
            "pos_share": pos_share, "daily_usd": daily_usd, "std_hr": std_hr,
            "hedge": "HL-spot" if coin.upper() in HL_SPOT_HEDGEABLE else "cross-venue",
        })
        time.sleep(0.03)
        if (i + 1) % 50 == 0:
            print(f"  ...{i+1}/{len(perps)}", file=sys.stderr)

    # Candidates: persistently positive funding.
    cands = [r for r in rows if r["pos_share"] >= args.min_positive and r["mean_hr"] > 0]
    cands.sort(key=lambda r: r["daily_usd"], reverse=True)

    print(f"\n{'='*92}")
    print(f"FUNDING CARRY CANDIDATES  (notional=${args.notional}/side, {args.days}d, >= {args.min_positive:.0f}% hours positive)")
    print(f"{'='*92}")
    hdr = f"{'COIN':<10}|{'ann%':>8}|{'pos hrs%':>9}|{'$/day':>8}|{'$/day x3':>9}|{'volat(σ/hr)':>12}|{'hedge':>11}"
    print(hdr)
    print("-" * len(hdr))
    for r in cands[:args.top]:
        print(f"{r['coin']:<10}|{r['ann_pct']:>8.1f}|{r['pos_share']:>8.0f}%|"
              f"{r['daily_usd']:>8.3f}|{r['daily_usd']*3:>9.3f}|{r['std_hr']*100:>11.4f}%|{r['hedge']:>11}")
    print("=" * len(hdr))

    if cands:
        best = cands[0]
        print(f"\nTop carry: {best['coin']} — {best['ann_pct']:.0f}% APR funding, "
              f"positive {best['pos_share']:.0f}% of hours, ~${best['daily_usd']:.3f}/day on ${args.notional} notional.")
        # How much notional / leverage is needed to hit $1/day at the median candidate rate.
        med = statistics.median([r["mean_hr"] for r in cands])
        if med > 0:
            need_notional = 1.0 / (med * 24)
            print(f"Median candidate funding => to earn $1/day you need ~${need_notional:,.0f} notional "
                  f"(={need_notional/400:.1f}x of a $400 deposit, delta-neutral).")
    else:
        print("\nNo perp had persistently positive funding over the window.")
    print("\nNOTE: 'cross-venue' hedge means no same-asset spot on HL — you must hold long spot on")
    print("another DEX/CEX, adding execution + bridging risk. Only HYPE/PURR/AZTEC/STABLE hedge on HL itself.")


if __name__ == "__main__":
    main()
