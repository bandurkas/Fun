#!/usr/bin/env python3
"""Concentrated-liquidity (Uniswap v3 style) LP backtest, with optional
delta-neutral perp hedge, on real or synthetic price paths.

Simulates an LP position in a symmetric range +-w around price. Models the
path-dependent parts honestly:
  * v3 position value via the real liquidity math (divergence loss / IL),
  * rebalancing when price exits the range (cost = swap fee + gas),
  * fees accrued only while IN range, at a gross APR that scales with the
    concentration multiplier E(w) (tighter range = higher in-range yield).

HEDGE (--hedge): short an ETH perp sized to the LP's live ETH delta, so the
position is (approximately) delta-neutral. The point is to strip out *direction*
(which dominates the unhedged result) and leave the real economics:
    net = fees + funding(short) - IL/LVR - hedge rebalance costs.
The hedge is re-sized only when delta drifts outside a band (--hedge-band), so
between rebalances a little directional leak accrues (this IS the LVR cost — the
sim captures it mechanically). Funding uses the REAL hourly ETH funding series
when available (a short RECEIVES funding when longs pay, which ETH does ~73% of
hours historically). Hedge margin is posted separately (perp on HL is NOT cross-
margined with an off-HL LP), so we track hedge-account equity and flag if the
short would have been liquidated.

The gross in-range fee APR (G_ref, at +-5%) is the one calibrated input (depends
on the pool's volume/liquidity) — shown across scenarios so you see sensitivity.
Everything else (time in range, IL, rebalances, hedge P&L) comes from the path.

Net is compared to HODLing 50/50 (unhedged benchmark) and, when hedging, the
right benchmark is ~0: a hedged LP only wins if fees + funding > LVR + costs.
"""
import argparse
import json
import math
import random
from pathlib import Path


def efficiency(w):
    return 1.0 / (1.0 - ((1 - w) / (1 + w)) ** 0.25)

E_REF = efficiency(0.05)


def v3_value(L, p, pa, pb):
    """USDC value of a v3 position with liquidity L at price p."""
    sp = math.sqrt(p); spa = math.sqrt(pa); spb = math.sqrt(pb)
    if p <= pa:
        x = L * (1 / spa - 1 / spb); y = 0.0
    elif p >= pb:
        x = 0.0; y = L * (spb - spa)
    else:
        x = L * (1 / sp - 1 / spb); y = L * (sp - spa)
    return x * p + y


def v3_eth_amount(L, p, pa, pb):
    """ETH (token0) held by the v3 position at price p — this is the LP's delta."""
    sp = math.sqrt(p); spa = math.sqrt(pa); spb = math.sqrt(pb)
    if p <= pa:
        return L * (1 / spa - 1 / spb)
    elif p >= pb:
        return 0.0
    return L * (1 / sp - 1 / spb)


def liquidity_for_capital(C, p, pa, pb):
    sp = math.sqrt(p); spa = math.sqrt(pa); spb = math.sqrt(pb)
    bracket = (1 / sp - 1 / spb) * p + (sp - spa)
    return C / bracket


def run(prices, hours, C, w, g_ref, fundings=None, swap_cost=0.0006, gas=0.30,
        hedge=False, hedge_fee=0.00035, hedge_band=0.10, leverage=2.0):
    days = (hours[-1] - hours[0]) / 86_400_000 if hours[-1] else 1.0
    g_w = g_ref * efficiency(w) / E_REF          # gross in-range APR for this width
    fee_per_hr_frac = g_w / 8760.0

    p0 = prices[0]
    eth0 = 0.5 * C / p0                            # HODL 50/50 benchmark
    usd0 = 0.5 * C

    cap = C
    center = p0
    pa, pb = center * (1 - w), center * (1 + w)
    L = liquidity_for_capital(cap, center, pa, pb)
    fees = 0.0
    costs = 0.0
    rebalances = 0
    hours_in_range = 0

    # ---- hedge state ----
    short_eth = v3_eth_amount(L, p0, pa, pb) if hedge else 0.0   # short this many ETH
    hedge_pnl = 0.0          # mark-to-market P&L of the short
    funding_recv = 0.0       # funding received (>0) / paid (<0) by the short
    hedge_costs = 0.0        # rebalance trading fees on the short
    hedge_rebalances = 0
    margin0 = (short_eth * p0) / leverage if hedge else 0.0      # standalone hedge margin
    min_hedge_equity = margin0
    liquidated = False

    prev_p = p0
    for i, p in enumerate(prices[1:], start=1):
        # ---- hedge: mark P&L + funding over the step that just happened ----
        if hedge and short_eth != 0.0:
            hedge_pnl += -short_eth * (p - prev_p)        # short gains when price falls
            rate = fundings[i] if fundings else 0.0       # hourly funding rate (longs pay if >0)
            funding_recv += short_eth * prev_p * rate     # short RECEIVES when rate > 0
            hedge_equity = margin0 + hedge_pnl + funding_recv - hedge_costs
            if hedge_equity < min_hedge_equity:
                min_hedge_equity = hedge_equity
            if hedge_equity <= 0 and not liquidated:
                liquidated = True

        in_range = pa <= p <= pb
        if in_range:
            val = v3_value(L, p, pa, pb)
            fees += val * fee_per_hr_frac
            hours_in_range += 1
        else:
            # price left the range -> realize, pay cost, re-center around p
            val = v3_value(L, p, pa, pb)
            cost = val * swap_cost + gas
            costs += cost
            cap = val - cost
            center = p
            pa, pb = center * (1 - w), center * (1 + w)
            L = liquidity_for_capital(cap, center, pa, pb)
            rebalances += 1

        # ---- hedge: re-size toward LP delta if drifted outside the band ----
        if hedge:
            target = v3_eth_amount(L, p, pa, pb)
            denom = max(target, 1e-12)
            if abs(short_eth - target) / denom > hedge_band:
                delta = abs(target - short_eth)
                hedge_costs += delta * p * hedge_fee
                short_eth = target
                hedge_rebalances += 1

        prev_p = p

    lp_value = v3_value(L, prices[-1], pa, pb)
    # margin0 is collateral (returned), not spent — include it so it nets against total_capital
    equity = lp_value + fees + (margin0 + hedge_pnl + funding_recv - hedge_costs if hedge else 0.0)
    total_capital = C + margin0
    net_pnl = equity - total_capital
    hodl = eth0 * prices[-1] + usd0

    net_apr = (equity / total_capital - 1) * (365 / days) if total_capital else 0.0
    return {
        "C": C, "w": w, "g_ref": g_ref, "days": days,
        "net_pnl": net_pnl, "net_per_day": net_pnl / days, "net_apr": net_apr * 100,
        "fees": fees, "costs": costs, "rebalances": rebalances,
        "in_range_pct": 100 * hours_in_range / (len(prices) - 1),
        "lp_vs_hodl": (lp_value + fees) - hodl, "hodl_pnl": hodl - C,
        # hedge
        "hedge": hedge, "hedge_pnl": hedge_pnl, "funding": funding_recv,
        "hedge_costs": hedge_costs, "hedge_rebalances": hedge_rebalances,
        "margin": margin0, "min_hedge_equity": min_hedge_equity,
        "liquidated": liquidated, "total_capital": total_capital,
    }


# ---------------------------------------------------------------------------
# Price paths: the real ETH path + synthetic regimes (clearly labelled)
# ---------------------------------------------------------------------------
def synth_path(n, p0, drift_apr, vol_apr, seed, mean_revert=0.0):
    """Hourly GBM (optionally Ornstein-Uhlenbeck-style mean reversion for 'chop')."""
    rng = random.Random(seed)
    dt = 1.0 / 8760.0
    mu = drift_apr * dt
    sig = vol_apr * math.sqrt(dt)
    logp = math.log(p0)
    log0 = logp
    out = [p0]
    for _ in range(n - 1):
        pull = -mean_revert * (logp - log0) * dt if mean_revert else 0.0
        logp += mu + pull - 0.5 * sig * sig + sig * rng.gauss(0, 1)
        out.append(math.exp(logp))
    return out


def build_paths(regime, real_prices, real_fund, n_synth, seed, vol=0.54):
    """Return list of (name, prices, fundings). fundings aligned per-hour or None.
    Synthetic vol defaults to 0.54 = realized vol of the real ETH path (fair compare)."""
    p0 = real_prices[0]
    fr = sum(real_fund) / len(real_fund) if real_fund else 0.0
    paths = []
    if regime in ("real", "bear", "all"):
        paths.append(("real-bear(-21%)", real_prices, real_fund))
    if regime in ("bull", "all"):
        pr = synth_path(n_synth, p0, 0.60, vol, seed)        # +60% APR drift
        paths.append((f"synth-bull(v{int(vol*100)})", pr, [fr] * n_synth))
    if regime in ("chop", "choppy", "all"):
        pr = synth_path(n_synth, p0, 0.0, vol, seed + 1, mean_revert=12.0)
        paths.append((f"synth-chop(v{int(vol*100)})", pr, [fr] * n_synth))
    return paths


def align_funding(candle_times, funding_list):
    """For each candle time, pick the most recent funding rate at or before it."""
    if not funding_list:
        return None
    fl = sorted(funding_list, key=lambda x: x["time"])
    times = [x["time"] for x in fl]
    rates = [float(x["fundingRate"]) for x in fl]
    out = []
    j = 0
    for t in candle_times:
        while j + 1 < len(times) and times[j + 1] <= t:
            j += 1
        out.append(rates[j] if t >= times[0] else rates[0])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="data/hl_eth_perp.json")
    ap.add_argument("--funding-file", default="data/hl_eth_funding.json")
    ap.add_argument("--caps", default="400,1000")
    ap.add_argument("--widths", default="0.02,0.05,0.10,0.20")
    ap.add_argument("--gross-aprs", default="0.50,1.00", help="Gross in-range fee APR scenarios at +-5%")
    ap.add_argument("--gas", type=float, default=0.30)
    ap.add_argument("--hedge", action="store_true", help="add delta-neutral short-perp hedge")
    ap.add_argument("--hedge-band", type=float, default=0.10, help="re-hedge when delta drifts > this fraction")
    ap.add_argument("--hedge-fee", type=float, default=0.00035, help="perp taker fee per side")
    ap.add_argument("--leverage", type=float, default=2.0, help="perp leverage (sets hedge margin)")
    ap.add_argument("--regime", default="real", choices=["real", "bear", "bull", "chop", "all"])
    ap.add_argument("--synth-vol", type=float, default=0.54, help="annualized vol for synthetic paths (real path is ~0.54)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    candles = json.loads(Path(args.file).read_text())
    candles.sort(key=lambda c: c["t"])
    prices = [float(c["c"]) for c in candles]
    times = [c["t"] for c in candles]

    fund_list = None
    fp = Path(args.funding_file)
    if fp.exists():
        fund_list = json.loads(fp.read_text())
    real_fund = align_funding(times, fund_list)

    days = (times[-1] - times[0]) / 86_400_000
    print(f"Real ETH path: {len(prices)} hourly candles, {days:.0f} days, "
          f"${prices[0]:.0f} -> ${prices[-1]:.0f} ({(prices[-1]/prices[0]-1)*100:+.1f}%)")
    if real_fund:
        mf = sum(real_fund) / len(real_fund)
        print(f"Real ETH funding: mean {mf*8760*100:+.2f}% APR (short hedge RECEIVES when >0), "
              f"{100*sum(r>0 for r in real_fund)/len(real_fund):.0f}% of hours positive")
    print(f"Mode: {'HEDGED (delta-neutral short)' if args.hedge else 'UNHEDGED'} | "
          f"regime={args.regime} | band={args.hedge_band} lev={args.leverage}x" if args.hedge
          else f"Mode: UNHEDGED | regime={args.regime}")

    caps = [float(x) for x in args.caps.split(",")]
    widths = [float(x) for x in args.widths.split(",")]
    gross = [float(x) for x in args.gross_aprs.split(",")]

    paths = build_paths(args.regime, prices, real_fund, len(prices), args.seed, vol=args.synth_vol)
    synth_times = [times[0] + i * 3_600_000 for i in range(len(prices))]

    for name, pr, fnd in paths:
        ht = times if pr is prices else synth_times
        print(f"\n### regime: {name}  (${pr[0]:.0f} -> ${pr[-1]:.0f}, {(pr[-1]/pr[0]-1)*100:+.1f}%)")
        if args.hedge:
            hdr = (f"{'cap':>5}|{'rng':>6}|{'gAPR':>5}|{'inR%':>5}|{'reb':>4}|{'fees$':>7}|"
                   f"{'fund$':>7}|{'hedgePnL$':>10}|{'hCost$':>7}|{'netAPR%':>8}|{'net$/d':>7}|{'minMargin$':>10}|{'liq':>4}")
        else:
            hdr = (f"{'cap':>5}|{'rng':>6}|{'gAPR':>5}|{'inR%':>5}|{'reb':>4}|{'fees$':>7}|"
                   f"{'costs$':>7}|{'netAPR%':>8}|{'net$/d':>7}|{'LP-vs-HODL$':>12}")
        print("=" * len(hdr)); print(hdr); print("-" * len(hdr))
        for C in caps:
            for g in gross:
                for w in widths:
                    r = run(pr, ht, C, w, g, fundings=fnd, gas=args.gas, hedge=args.hedge,
                            hedge_fee=args.hedge_fee, hedge_band=args.hedge_band, leverage=args.leverage)
                    rng = '+-' + str(int(w * 100)) + '%'
                    if args.hedge:
                        print(f"{C:>5.0f}|{rng:>6}|{g*100:>4.0f}%|{r['in_range_pct']:>4.0f}%|"
                              f"{r['rebalances']:>4}|{r['fees']:>7.2f}|{r['funding']:>7.2f}|"
                              f"{r['hedge_pnl']:>10.2f}|{r['hedge_costs']:>7.2f}|{r['net_apr']:>7.1f}%|"
                              f"{r['net_per_day']:>7.3f}|{r['min_hedge_equity']:>10.2f}|{'YES' if r['liquidated'] else '-':>4}")
                    else:
                        print(f"{C:>5.0f}|{rng:>6}|{g*100:>4.0f}%|{r['in_range_pct']:>4.0f}%|"
                              f"{r['rebalances']:>4}|{r['fees']:>7.2f}|{r['costs']:>7.2f}|"
                              f"{r['net_apr']:>7.1f}%|{r['net_per_day']:>7.3f}|{r['lp_vs_hodl']:>12.2f}")
            print("-" * len(hdr))
        print("=" * len(hdr))

    if args.hedge:
        print("\nHEDGED: netAPR/net$/day = (LP fees + funding + hedge P&L - all costs) on (LP cap + hedge margin).")
        print("minMargin$ = lowest hedge-account equity reached (margin posted minus running short loss);")
        print("liq=YES => short would have been liquidated (hedge margin wiped) — needs more margin / lower lev.")
        print("A hedged LP should be ~direction-free: it wins only if fees + funding > LVR + rebalance costs.")
        print("VERDICT (2026-06-19): at realistic ETH/USDC gross fee APR (~50-100%) hedged LP is NET-NEGATIVE in")
        print("every regime (bear/bull/chop, vol-matched 54%) — IL/LVR + costs > fees; funding (+3.8% APR) is a")
        print("small tailwind. Break-even is ~gross 150-200% APR, which ETH/USDC does not sustain. Economics are")
        print("~linear in capital, so scaling to $5000 does NOT fix a negative APR. Hedge can also be liquidated")
        print("in a rally (LP gains aren't liquid to top up a standalone perp margin). LP path closed as income source.")
    else:
        print("\nUNHEDGED: netAPR/net$/day vs initial capital; LP-vs-HODL$ = beat holding 50/50? (neg => IL ate fees)")
        print(f"HODL 50/50 (real path): {(prices[-1]/prices[0]-1)*50:+.1f}% on capital.")


if __name__ == "__main__":
    main()
