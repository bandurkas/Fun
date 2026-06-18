# HANDOFF — DEX Yield Research (Hyperliquid) — for next session

Last updated: 2026-06-19. Local: `/Users/sabar/Desktop/Fun` (git, origin `git@github.com:bandurkas/Fun.git`). VPS2 `168.231.118.173` `/root/Fun` (venv `.venv`, py3.12).

## Goal
Earn **$1–2/day** on a DEX, starting test deposit **$400**, willing to scale to **$1000 → $5000** if risk is minimal/controllable. User builds bots, can backtest, has Hyperliquid infra. Approach throughout: **measure before risking real money.**

## TL;DR verdict so far
Across every explored strategy the extractable edge for a small retail participant is thin — **cents/day on $400–1000**. Reliable **$1–2/day needs scale (~$5000) or accepted risk.** Gross yields (spread/fees/funding) are competed down to risk compensation; IL & adverse-selection transfer them to informed flow.

## Strategy 1 — Spot-perp basis arbitrage → REJECTED
- Original `spot_perp_arbitrage_backtest.py` used spot forward-fill = **phantom basis** (don't trust its numbers). HANDOFF claim about "volume>0 filter" was false.
- Honest `backtest_v2.py` (volume>0 filter, bid/ask proxy, leg-specific fees, maker/taker): no robust edge. Only liquid coin **HYPE** ~break-even; **PURR** "profit" is pure slippage-assumption artifact (break-even at ~0.10%/leg slippage; PURR ~$0.10 token real spread ≥0.10–0.20%). Win rate collapses 94%→9% as slippage 0.02%→0.20% = profit was smaller than spread (bid-ask noise between non-sync close prints). Funding $0 (10-min holds).
- Only **8 coins** have both spot+perp on HL; of those only 4 share the SAME underlying (HYPE, PURR, AZTEC, STABLE) — rest (BERA/PUMP/TRUMP...) are different meme tokens with same ticker.
- **Live gate DONE → CLOSED PERMANENTLY (2026-06-19).** `basis_logger.py` ran VPS2 screen `basis` 2026-06-18 16:50→17:26 UTC (WS dropped ~36 min in; screen since killed). 429 paired L2 snapshots/coin in `basis_log.csv` (pulled local). Real executable-basis result:
  - **HYPE:** net **taker** edge mean −0.0955%, **never positive (0/429)**, best −0.035% → taker arb is a structural loss. Net **maker-maker** edge mean +0.0145% (positive 75% of time) but tiny (p90 +0.045%) and *before* adverse selection — resting dual-maker fills land exactly when price runs through you, eroding this micro-edge to ≤0. ~$0.005/round-trip on $35; needs ~200 perfect dual fills/day for $1.
  - **PURR:** net taker mean −0.136% (10% positive, spread artifact), net maker mean **−0.026%** (negative). Wide spreads (spot 0.28% / perp 0.30%) → "profit" is bid-ask noise, confirming backtest_v2. Dead.
  - **Verdict:** live L2 confirms the honest backtest — no executable basis edge for a small taker; maker-maker is sub-fee micro-noise with adverse-selection risk. Strategy 1 closed; do not revisit. `live_bot.py` stays parked.

## Strategy 2 — Funding carry (delta-neutral) — CHOSEN direction, partially validated
`funding_scanner.py` ranks all ~230 HL perps by fundingHistory persistence (% hours positive) × magnitude.
- Stable carry = small. At **$1000 deposit (~$750 notional/side, 3x)**:
  - **PURR: 36% APR, ~$0.74/day** — CLEANEST: hedges on HL itself (spot+perp, unified cross-margin → hedge offsets liquidation). Controllable risk. Best single-venue option.
  - HYPE: 12.7% APR, ~$0.26/day (also HL-spot hedge).
  - HEMI: 109% APR, **~$2.24/day** but cross-venue hedge (no HL spot → long spot elsewhere, bridging/2 accounts) AND most volatile funding (σ 3× others → flips fast). High risk.
  - Others (XMR/MANTA/ZRO/...) cross-venue, $0.3–0.6/day.
- At $400: ~40% of above. PURR ~$0.30/day.
- Funding numbers are GROSS; **net = funding − hedge round-trip fees − spot swap/gas** → favors long holds of stable-positive-funding assets, not rotation.
- **NEXT: build carry P&L logger (net of fees) for PURR (+ maybe HEMI), run a few days on VPS.** PURR on-HL with unified margin is the leading candidate to push toward $1/day via modest leverage at $1000.

## Strategy 3 — Concentrated LP (Uniswap v3 style) — backtested, thin alpha
`lp_backtest.py` on real ETH 1h path (90d, was −20.7% downtrend). Models v3 value math (IL), rebalance-on-exit (swap+gas), fees accrued in-range scaling with concentration E(w). Calibrated input = gross in-range fee APR (G_ref at ±5%); shown across scenarios.
- Unhedged LP is dominated by **direction** (ETH −20% → LP loses holding ETH). HODL 50/50 itself −10.3%.
- **LP alpha over HODL is tiny/fragile:** best +$15.7/$1000 over 90d ≈ **6% APR**, only at gross 100% APR + wide ±10% range. At gross 50% APR LP loses to HODL (IL > fees). Tight ±2% = disaster (90 rebalances).
- Scaling (linear in capital): $1–2/day needs net APR **91–182% @ $400**, **36–73% @ $1000**, **7–15% @ $5000**. Unhedged LP doesn't deliver those at realistic fee APR.
- **HEDGED LP — built & tested 2026-06-19 → REJECTED as income source.** `lp_backtest.py --hedge` adds a delta-neutral short ETH perp sized to the LP's live ETH delta (band-rebalanced), real hourly ETH funding (mean **+3.83% APR, short RECEIVES**, 73% hrs positive), hedge fees + standalone margin/liquidation tracking, and synthetic bull/chop paths (vol-matched to the real path's 54%) alongside the real bear path.
  - **Validated** the hedge math against the AMM identity dV=∫x·dp (wide fixed-L ETH path: LP −$103.77, short offsets +$75.69, leaving −$28 pure IL/gamma). v3 holds only ~41% in ETH at center, so direction is smaller than a naive 50/50.
  - **Result:** hedging strips direction (bull/bear/chop now same order of magnitude, no longer drawdown-dominated) — and that exposes the truth: at realistic ETH/USDC gross fee APR (~50–100%), **hedged net is NEGATIVE in every regime** (−$0.23 to −$1.86/day on $1000). IL/LVR + rebalancing + hedge costs > fees; funding is only a small tailwind. Break-even ≈ **gross 150–200% APR**, which ETH/USDC does not sustain; even at an optimistic gross 200% it's only +$0.2–1.0/day on $1000 (not $1–2) and the 2× hedge got **liquidated in a rally** (bull ±10%). Economics ~linear in capital → scaling to $5000 doesn't fix a negative APR. This is the classic LVR result.
  - **Not done (optional, won't change verdict):** pull real ETH/USDC pool fee APR from Uniswap v3 subgraph (needs a Graph API key now) — would only confirm real APR < break-even. Realistic 0.05%-pool in-range APR is ~15–60%, well below the ~150–200% needed.
  - **Conclusion: LP (hedged or not) is not the income path at $400–5000.** PURR funding carry (Strategy 2) is the only surviving candidate.

## Files
- `live_bot.py` — hardened HL spot-perp live bot (real fill parsing, atomic-hedge rollback, state persistence+reconcile, market_close/reduce-only, timeout force-exit). Parked (strategy 1 weak). entry/exit defaults 0.80/0.05.
- `backtest_v2.py` — honest basis backtest. `spot_perp_arbitrage_backtest.py` — OLD, forward-fill, don't trust.
- `basis_logger.py` — read-only L2 basis logger (running on VPS, HYPE+PURR).
- `funding_scanner.py` — funding-carry ranker across all perps. `--notional` sets side notional ($300≈$400 dep, $750≈$1000 dep at 3x).
- `lp_backtest.py` — concentrated LP backtest. `--caps 400,1000 --widths ... --gross-aprs 0.50,1.00`.
- `fetch_hl_data.py` — candle/funding downloader (NOTE: bails on coins without HL spot, e.g. ETH; fetch perp candles directly via candleSnapshot if needed).
- `data/` — candle+funding JSON (8 spot+perp coins; ETH 1h 90d perp).

## Secrets / config
- `.env` (gitignored): `HL_PRIVATE_KEY` = API/agent wallet "AL" key; `HL_ACCOUNT_ADDRESS` = MASTER wallet `0x275CEF8C7125378142261d64eD36e1FbbFc0C701` (NOT agent `0x8E71...`); `HL_IS_TESTNET=True`.
- Earlier the privkey was briefly pasted into requirements.txt (tracked, NOT committed) — scrubbed. Verify it never gets committed.

## Open decisions for next session
1. ~~Analyze `basis_log.csv` → close strategy 1 or not.~~ **DONE 2026-06-19: closed permanently (see Strategy 1).**
2. ~~Build delta-neutral hedged LP + multi-regime + pool data.~~ **DONE 2026-06-19: hedged LP REJECTED (see Strategy 3) — net-negative at realistic fee APR in every regime; not the income path.**
3. **NEXT (now the lead):** build **carry P&L logger** for PURR (net of fees) — PURR delta-neutral on HL is the only surviving low-risk income candidate (~$0.74/day @ $1000). Run a few days on VPS to measure net carry after hedge round-trip fees + spot swap/gas.
4. Decide capital: with basis-arb and LP both closed, PURR carry is the only live lever — and it's ~cents-to-$0.7/day at $400–1000 unless leveraged. Honest expectation: low-risk income here is small; $1–2/day reliably needs ~$5000 in PURR carry or accepted leverage/risk.
