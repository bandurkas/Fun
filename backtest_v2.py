#!/usr/bin/env python3
"""Honest spot-perp cash-and-carry backtest.

Differences vs the original engine (which inflated returns):
  * NO forward-fill. A trade can only open/close on a minute where the SPOT
    candle actually traded (volume > 0). Stale-spot phantom basis is removed.
  * Executable prices: buy spot at ask-side, sell perp at bid-side on entry,
    and the reverse on exit (slippage proxy for the bid/ask spread).
  * Realistic, leg-specific fees with a maker/taker switch.
  * PnL is split into basis-capture vs funding so we can see what feeds the edge.
  * Optional positive-funding entry filter (short perp earns funding only when
    the rate is positive).

NOTE: candle data has no L2 book, so the bid/ask is approximated by `slippage`.
Maker results assume the limit order fills (no fill-probability penalty), so they
are an optimistic upper bound for the maker case.
"""
import argparse
import json
import sys
from pathlib import Path

# Hyperliquid base-tier fees (fraction of notional), per leg.
FEES = {
    "taker": {"spot": 0.00070, "perp": 0.00035},
    "maker": {"spot": 0.00040, "perp": 0.00010},
}


def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"Error loading {path.name}: {e}", file=sys.stderr)
        return None


def latest_funding_rate(funding_times, funding_by_t, t):
    """Most recent funding rate at or before minute t (hourly settlement)."""
    import bisect
    i = bisect.bisect_right(funding_times, t) - 1
    if i < 0:
        return 0.0
    return funding_by_t[funding_times[i]]


def run(coin, data_dir, args, mode):
    spot = load_json(data_dir / f"hl_{coin.lower()}_spot.json")
    perp = load_json(data_dir / f"hl_{coin.lower()}_perp.json")
    funding = load_json(data_dir / f"hl_{coin.lower()}_funding.json") or []
    if not spot or not perp:
        return None

    fee_spot = FEES[mode]["spot"]
    fee_perp = FEES[mode]["perp"]
    slip = args.slippage / 100.0 if mode == "taker" else 0.0  # maker provides liquidity

    # Spot close + volume by minute; only minutes with real volume are tradeable.
    spot_close = {c["t"]: float(c["c"]) for c in spot}
    spot_vol = {c["t"]: float(c["v"]) for c in spot}
    perp_close = {c["t"]: float(c["c"]) for c in perp}

    funding_by_t = {int(f["time"]): float(f["fundingRate"]) for f in funding}
    funding_times = sorted(funding_by_t.keys())

    timeline = sorted(perp_close.keys())
    if not timeline:
        return None

    # Liquidity profile of the spot leg.
    n_perp_min = len(timeline)
    tradeable = [t for t in timeline if spot_vol.get(t, 0.0) > args.min_vol and t in spot_close]
    liq_pct = 100.0 * len(tradeable) / n_perp_min if n_perp_min else 0.0
    span_days = (timeline[-1] - timeline[0]) / 86_400_000 or 1.0

    leg_usd = args.leg_usd
    entry_th = args.entry
    exit_th = args.exit

    pos = None
    trades = []
    funding_total = 0.0
    fees_total = 0.0
    last_hour = None

    for t in timeline:
        perp_px = perp_close[t]
        has_spot = (spot_vol.get(t, 0.0) > args.min_vol) and (t in spot_close)
        spot_px = spot_close.get(t)

        # Accrue hourly funding on the open short-perp leg.
        if pos is not None:
            hour = (t // 3_600_000) * 3_600_000
            if last_hour is not None and hour > last_hour and hour in funding_by_t:
                fr = funding_by_t[hour]
                pay = pos["size"] * perp_px * fr  # short earns when fr > 0
                funding_total += pay
                pos["funding"] += pay
            last_hour = hour
        else:
            last_hour = (t // 3_600_000) * 3_600_000

        if not has_spot:
            continue  # cannot execute on a stale-spot minute

        if pos is None:
            basis = (perp_px - spot_px) / spot_px * 100.0
            if basis < entry_th:
                continue
            if args.require_positive_funding and latest_funding_rate(funding_times, funding_by_t, t) <= 0:
                continue
            spot_fill = spot_px * (1 + slip)
            perp_fill = perp_px * (1 - slip)
            size = leg_usd / spot_fill
            if size * spot_fill < args.min_notional:
                continue
            ef = spot_fill * size * fee_spot + perp_fill * size * fee_perp
            fees_total += ef
            pos = {"t": t, "spot_fill": spot_fill, "perp_fill": perp_fill, "size": size,
                   "entry_basis": basis, "funding": 0.0, "entry_fee": ef}
        else:
            basis = (perp_px - spot_px) / spot_px * 100.0
            elapsed_m = (t - pos["t"]) / 60_000
            if basis > exit_th and elapsed_m < args.timeout_min:
                continue
            spot_exit = spot_px * (1 - slip)
            perp_exit = perp_px * (1 + slip)
            size = pos["size"]
            spot_pnl = (spot_exit - pos["spot_fill"]) * size
            perp_pnl = (pos["perp_fill"] - perp_exit) * size
            xf = spot_exit * size * fee_spot + perp_exit * size * fee_perp
            fees_total += xf
            basis_pnl = spot_pnl + perp_pnl - pos["entry_fee"] - xf
            net = basis_pnl + pos["funding"]
            trades.append({
                "dur_m": elapsed_m, "entry_basis": pos["entry_basis"], "exit_basis": basis,
                "basis_pnl": basis_pnl, "funding": pos["funding"], "net": net,
                "reason": "timeout" if elapsed_m >= args.timeout_min else "converge",
            })
            pos = None

    n = len(trades)
    net_total = sum(tr["net"] for tr in trades)
    basis_total = sum(tr["basis_pnl"] for tr in trades)
    fund_in_trades = sum(tr["funding"] for tr in trades)
    wins = sum(1 for tr in trades if tr["net"] > 0)
    win_rate = 100.0 * wins / n if n else 0.0
    avg_dur = sum(tr["dur_m"] for tr in trades) / n if n else 0.0
    trades_per_day = n / span_days

    # Max drawdown on the per-trade equity curve.
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for tr in trades:
        eq += tr["net"]
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

    return {
        "coin": coin.upper(), "mode": mode, "entry": entry_th, "exit": exit_th,
        "trades": n, "trades_per_day": trades_per_day,
        "net": net_total, "net_per_day": net_total / span_days,
        "basis_pnl": basis_total, "funding_pnl": fund_in_trades,
        "win_rate": win_rate, "avg_dur_m": avg_dur, "max_dd": max_dd,
        "liq_pct": liq_pct, "span_days": span_days, "leg_usd": leg_usd,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", type=str, help="Comma list; default = scan data dir")
    ap.add_argument("--data-dir", type=str, default="data")
    ap.add_argument("--leg-usd", type=float, default=35.0)
    ap.add_argument("--slippage", type=float, default=0.02, help="Per-leg taker slippage %% (bid/ask proxy)")
    ap.add_argument("--min-notional", type=float, default=10.0)
    ap.add_argument("--min-vol", type=float, default=0.0, help="Min spot candle volume to treat minute as tradeable")
    ap.add_argument("--timeout-min", type=float, default=720.0)
    ap.add_argument("--require-positive-funding", action="store_true")
    ap.add_argument("--entries", type=str, default="0.10,0.15,0.20,0.30")
    ap.add_argument("--exits", type=str, default="0.0,0.05")
    ap.add_argument("--modes", type=str, default="taker,maker")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    if args.coin:
        coins = [c.strip().upper() for c in args.coin.split(",")]
    else:
        coins = sorted({f.name.split("_")[1].upper() for f in data_dir.glob("hl_*_spot.json")})

    entries = [float(x) for x in args.entries.split(",")]
    exits = [float(x) for x in args.exits.split(",")]
    modes = [m.strip() for m in args.modes.split(",")]

    # Liquidity overview first.
    print("\nLIQUIDITY (share of minutes where spot actually traded):")
    print(f"{'COIN':<8} | {'spot-active %':>13} | {'days':>6}")
    print("-" * 36)
    for coin in coins:
        r = run(coin, data_dir, argparse.Namespace(**{**vars(args), "entry": 99, "exit": 0}), "taker")
        if r:
            print(f"{coin:<8} | {r['liq_pct']:>12.1f}% | {r['span_days']:>6.1f}")

    header = (f"{'COIN':<7}|{'MODE':<6}|{'ENT':>5}|{'EXT':>5}|{'TRD':>5}|{'/day':>6}|"
              f"{'NET$':>8}|{'$/day':>7}|{'basis$':>8}|{'fund$':>7}|{'win%':>6}|{'dur_m':>7}|{'maxDD$':>7}")
    print("\n" + "=" * len(header))
    print("SWEEP  (leg=${:.0f}, slip={:.2f}% taker)".format(args.leg_usd, args.slippage))
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    best = []
    for coin in coins:
        for mode in modes:
            for e in entries:
                for x in exits:
                    a = argparse.Namespace(**{**vars(args), "entry": e, "exit": x})
                    r = run(coin, data_dir, a, mode)
                    if not r or r["trades"] == 0:
                        continue
                    best.append(r)
                    print(f"{r['coin']:<7}|{mode:<6}|{e:>5.2f}|{x:>5.2f}|{r['trades']:>5}|"
                          f"{r['trades_per_day']:>6.1f}|{r['net']:>8.2f}|{r['net_per_day']:>7.3f}|"
                          f"{r['basis_pnl']:>8.2f}|{r['funding_pnl']:>7.3f}|{r['win_rate']:>5.0f}%|"
                          f"{r['avg_dur_m']:>7.0f}|{r['max_dd']:>7.2f}")

    print("=" * len(header))
    profitable = [r for r in best if r["net"] > 0]
    if profitable:
        profitable.sort(key=lambda r: r["net_per_day"], reverse=True)
        print("\nTOP 5 by $/day (profitable only):")
        for r in profitable[:5]:
            print(f"  {r['coin']} {r['mode']} entry={r['entry']} exit={r['exit']}: "
                  f"${r['net_per_day']:.3f}/day, {r['trades_per_day']:.1f} trades/day, "
                  f"win {r['win_rate']:.0f}%, funding ${r['funding_pnl']:.2f}")
    else:
        print("\nNo profitable (entry,exit,mode) combination found on this data.")


if __name__ == "__main__":
    main()
