#!/usr/bin/env python3
"""Live net-carry logger for HL delta-neutral funding carry (read-only, no keys).

Measures the ONE surviving low-risk candidate: long spot + short perp on Hyperliquid
(same asset, unified margin), earning funding when the perp funding is positive.
Records, every interval, for each candidate coin:
  funding rate (hourly), perp & spot mid, top-of-book spreads (= real entry/exit cost),
  and cumulative ACCRUED funding for a notional $1000 short, net of an amortized
  round-trip cost estimate. Goal: confirm whether funding PERSISTS positive over days
  (current backtest is only ~3 days of data) and what net carry survives real spreads.

NO private key, NO orders — pure public data. Run on VPS2 in screen:
  screen -S carry -dm python3 carry_logger.py --coins PURR,HYPE,BERA,AZTEC
  ssh ... 'wc -l /root/Fun/carry_log.csv'
"""
import argparse
import csv
import sys
import time
from datetime import datetime, timezone

from hyperliquid.info import Info
from hyperliquid.utils import constants


def top_spread(info, name):
    """Return (mid, spread_pct) from top of book; (None,None) on failure."""
    try:
        book = info.l2_snapshot(name)
        bids, asks = book["levels"][0], book["levels"][1]
        if not bids or not asks:
            return None, None
        bid, ask = float(bids[0]["px"]), float(asks[0]["px"])
        mid = (bid + ask) / 2
        return mid, (ask - bid) / mid * 100
    except Exception:
        return None, None


def discover_spot(info, coins):
    """Map perp coin -> its USDC spot pair name (for spot-leg spread)."""
    sm = info.spot_meta()
    tok = {t["index"]: t["name"].upper() for t in sm["tokens"]}
    out = {}
    for p in sm["universe"]:
        a, b = p["tokens"]
        names = {tok.get(a), tok.get(b)}
        if "USDC" in names:
            base = (names - {"USDC"}).pop()
            if base in coins:
                out[base] = p["name"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="PURR,HYPE,BERA,AZTEC")
    ap.add_argument("--out", default="carry_log.csv")
    ap.add_argument("--interval", type=float, default=600.0, help="seconds between samples")
    ap.add_argument("--notional", type=float, default=1000.0, help="short notional $ for accrual")
    ap.add_argument("--rt-cost-pct", type=float, default=0.30,
                    help="assumed round-trip cost %% (4 fills; PURR spreads are wide)")
    args = ap.parse_args()

    coins = {c.strip().upper() for c in args.coins.split(",")}
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    spot_names = discover_spot(info, coins)
    print(f"[carry] coins={sorted(coins)} spot_pairs={spot_names}", flush=True)

    accrued = {c: 0.0 for c in coins}          # cumulative funding $ per `notional`
    f = open(args.out, "a", newline="")
    w = csv.writer(f)
    if f.tell() == 0:
        w.writerow(["ts_iso", "coin", "funding_hr", "funding_apr_pct", "perp_mid",
                    "perp_spread_pct", "spot_spread_pct", "accrued_funding_usd",
                    "net_carry_usd", "net_apr_pct_so_far", "hours_elapsed"])
        f.flush()

    t0 = time.time()
    last = t0
    while True:
        try:
            meta, ctxs = info.meta_and_asset_ctxs()
            name_to_ctx = {u["name"].upper(): c for u, c in zip(meta["universe"], ctxs)}
            now = time.time()
            dt_h = (now - last) / 3600.0
            last = now
            hrs = (now - t0) / 3600.0
            iso = datetime.now(timezone.utc).isoformat()
            for coin in sorted(coins):
                ctx = name_to_ctx.get(coin)
                if not ctx:
                    continue
                fr = float(ctx.get("funding", 0.0))             # hourly funding rate
                perp_mid, perp_sp = top_spread(info, coin)
                spot_sp = None
                if coin in spot_names:
                    _, spot_sp = top_spread(info, spot_names[coin])
                # short receives positive funding: accrue over elapsed dt
                accrued[coin] += fr * args.notional * dt_h
                rt_cost_usd = args.rt_cost_pct / 100.0 * args.notional   # one round-trip
                net = accrued[coin] - rt_cost_usd
                net_apr = (net / args.notional) / max(hrs / 8760.0, 1e-9) * 100 if hrs > 0.1 else 0.0
                w.writerow([iso, coin, f"{fr:.8f}", f"{fr*8760*100:.2f}",
                            f"{perp_mid}" if perp_mid else "",
                            f"{perp_sp:.4f}" if perp_sp is not None else "",
                            f"{spot_sp:.4f}" if spot_sp is not None else "",
                            f"{accrued[coin]:.4f}", f"{net:.4f}",
                            f"{net_apr:.2f}", f"{hrs:.2f}"])
            f.flush()
            print(f"[carry] {iso} sampled {len(coins)} coins, {hrs:.1f}h elapsed", flush=True)
        except Exception as e:
            print(f"[carry] error: {e}", file=sys.stderr, flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
