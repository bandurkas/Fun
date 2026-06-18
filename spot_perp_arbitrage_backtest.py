#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path

def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"Error loading {path.name}: {e}", file=sys.stderr)
        return None

def run_backtest_for_coin(coin: str, data_dir: Path, args) -> dict:
    spot_path = data_dir / f"hl_{coin.lower()}_spot.json"
    perp_path = data_dir / f"hl_{coin.lower()}_perp.json"
    funding_path = data_dir / f"hl_{coin.lower()}_funding.json"
    
    spot_candles = load_json(spot_path)
    perp_candles = load_json(perp_path)
    funding_data = load_json(funding_path)
    
    if not spot_candles or not perp_candles:
        return {"error": f"Missing spot or perp candle data for {coin}."}
        
    print(f"\n=== Running Backtest for {coin.upper()} ===")
    print(f"Loaded {len(spot_candles)} spot candles, {len(perp_candles)} perp candles")
    
    # 1. Index data for fast lookup
    # Spot is often less liquid and might have missing minutes. We will forward-fill spot.
    spot_by_t = {c["t"]: float(c["c"]) for c in spot_candles}
    perp_by_t = {c["t"]: float(c["c"]) for c in perp_candles}
    
    # Funding by hour timestamp
    funding_by_t = {}
    if funding_data:
        for f in funding_data:
            funding_by_t[int(f["time"])] = float(f["fundingRate"])
        print(f"Loaded {len(funding_data)} funding rate records")
    else:
        print("Warning: No funding history found. Funding payments will be 0.")
        
    # Get sorted list of timestamps from perp candles (perp usually has no missing minutes)
    timestamps = sorted(perp_by_t.keys())
    if not timestamps:
        return {"error": f"No perp candles available for {coin}."}
        
    # Forward-fill spot prices
    aligned_spot = {}
    last_spot = None
    
    # Create a union of all timestamps to ensure we don't miss spot updates
    all_ts = sorted(list(set(timestamps) | set(spot_by_t.keys())))
    for ts in all_ts:
        if ts in spot_by_t:
            last_spot = spot_by_t[ts]
        aligned_spot[ts] = last_spot
        
    # Backtest parameters
    balance = args.deposit
    position = None
    trades = []
    fees_paid = 0.0
    funding_earned = 0.0
    
    # Slippage and Fee multipliers
    # e.g., fee = 0.025% = 0.00025
    fee_rate = args.fee / 100.0
    slip_rate = args.slippage / 100.0
    
    entry_threshold = args.entry
    exit_threshold = args.exit
    
    # Track hourly funding boundaries crossed
    last_hour_ts = None
    
    for t in timestamps:
        perp_price = perp_by_t[t]
        spot_price = aligned_spot.get(t)
        
        if not spot_price or not perp_price:
            continue
            
        basis_pct = ((perp_price - spot_price) / spot_price) * 100.0
        
        # Check if we crossed an hourly boundary while holding position
        # Hyperliquid funding settles on the hour clock (ts % 3600000 == 0)
        current_hour_ts = (t // 3600000) * 3600000
        is_funding_hour = (last_hour_ts is not None) and (current_hour_ts > last_hour_ts)
        last_hour_ts = current_hour_ts
        
        if position is not None:
            # Apply hourly funding if we crossed a boundary
            if is_funding_hour and current_hour_ts in funding_by_t:
                fr = funding_by_t[current_hour_ts]
                # In a Short Perp position, we EARN funding if rate is positive, PAY if negative
                # Funding payment = -1 * perp_notional * funding_rate
                # Note: Hyperliquid API fundingRate is hourly (e.g. 0.0001 means 0.01% per hour)
                perp_notional = position["perp_size"] * perp_price
                funding_payment = -1.0 * perp_notional * fr
                funding_earned += funding_payment
                balance += funding_payment
                position["funding_accumulated"] += funding_payment
                
            # Check for Exit Condition
            if basis_pct <= exit_threshold:
                # Close Spot (Sell)
                spot_exit_price = spot_price * (1.0 - slip_rate)
                spot_pnl = (spot_exit_price - position["entry_spot_price"]) * position["spot_size"]
                spot_fee = spot_exit_price * position["spot_size"] * fee_rate
                
                # Close Perp (Buy back short)
                perp_exit_price = perp_price * (1.0 + slip_rate)
                perp_pnl = (position["entry_perp_price"] - perp_exit_price) * position["perp_size"]
                perp_fee = perp_exit_price * position["perp_size"] * fee_rate
                
                trade_fees = spot_fee + perp_fee
                trade_pnl = spot_pnl + perp_pnl
                net_trade_pnl = trade_pnl + position["funding_accumulated"] - trade_fees
                
                fees_paid += trade_fees
                balance += (spot_pnl + perp_pnl - trade_fees)  # Update balance with trading PnL & fees
                
                trades.append({
                    "entry_t": position["entry_t"],
                    "exit_t": t,
                    "duration_m": (t - position["entry_t"]) / 60000,
                    "entry_basis": position["entry_basis"],
                    "exit_basis": basis_pct,
                    "spot_pnl": spot_pnl,
                    "perp_pnl": perp_pnl,
                    "funding": position["funding_accumulated"],
                    "fees": trade_fees,
                    "net_pnl": net_trade_pnl
                })
                
                position = None
                
        else:
            # Check for Entry Condition
            if basis_pct >= entry_threshold:
                # We want a truly delta-neutral position: equal coin sizes for spot and perp
                # entry_capital = Spot_notional + Perp_notional = N*S + N*P = N*(S + P)
                # => N = entry_capital / (S + P)
                entry_capital = min(balance, args.max_pos_size)
                
                # Apply slippage on entry
                spot_entry_price = spot_price * (1.0 + slip_rate)
                perp_entry_price = perp_price * (1.0 - slip_rate)
                
                coin_size = entry_capital / (spot_entry_price + perp_entry_price)
                spot_size = coin_size
                perp_size = coin_size
                
                entry_fees = (spot_entry_price * spot_size * fee_rate) + (perp_entry_price * perp_size * fee_rate)
                fees_paid += entry_fees
                balance -= entry_fees
                
                position = {
                    "entry_t": t,
                    "entry_spot_price": spot_entry_price,
                    "entry_perp_price": perp_entry_price,
                    "spot_size": spot_size,
                    "perp_size": perp_size,
                    "entry_basis": basis_pct,
                    "funding_accumulated": 0.0
                }
                
    # Close active position at the end of backtest if any
    if position is not None:
        last_t = timestamps[-1]
        perp_price = perp_by_t[last_t]
        spot_price = aligned_spot.get(last_t, spot_price)
        
        spot_exit_price = spot_price * (1.0 - slip_rate)
        spot_pnl = (spot_exit_price - position["entry_spot_price"]) * position["spot_size"]
        spot_fee = spot_exit_price * position["spot_size"] * fee_rate
        
        perp_exit_price = perp_price * (1.0 + slip_rate)
        perp_pnl = (position["entry_perp_price"] - perp_exit_price) * position["perp_size"]
        perp_fee = perp_exit_price * position["perp_size"] * fee_rate
        
        trade_fees = spot_fee + perp_fee
        trade_pnl = spot_pnl + perp_pnl
        net_trade_pnl = trade_pnl + position["funding_accumulated"] - trade_fees
        
        fees_paid += trade_fees
        balance += (spot_pnl + perp_pnl - trade_fees)
        
        trades.append({
            "entry_t": position["entry_t"],
            "exit_t": last_t,
            "duration_m": (last_t - position["entry_t"]) / 60000,
            "entry_basis": position["entry_basis"],
            "exit_basis": ((perp_price - spot_price) / spot_price) * 100,
            "spot_pnl": spot_pnl,
            "perp_pnl": perp_pnl,
            "funding": position["funding_accumulated"],
            "fees": trade_fees,
            "net_pnl": net_trade_pnl
        })
        position = None
        
    # Calculate performance metrics
    total_net_pnl = balance - args.deposit
    num_trades = len(trades)
    
    if num_trades > 0:
        win_trades = [t for t in trades if t["net_pnl"] > 0]
        win_rate = (len(win_trades) / num_trades) * 100.0
        avg_duration = sum(t["duration_m"] for t in trades) / num_trades
        avg_net_pnl = total_net_pnl / num_trades
        
        # Simple Max Drawdown calculation from trade equity curve
        equity = args.deposit
        peak = args.deposit
        max_dd = 0.0
        for t in trades:
            equity += t["net_pnl"]
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
    else:
        win_rate = 0.0
        avg_duration = 0.0
        avg_net_pnl = 0.0
        max_dd = 0.0
        
    days = args.days
    daily_avg_pnl = total_net_pnl / days if days > 0 else 0.0
    daily_return_pct = (daily_avg_pnl / args.deposit) * 100.0
    
    # Print metrics
    print(f"Results for {coin.upper()}:")
    print(f"  Total Net Profit: ${total_net_pnl:.2f} ({((balance - args.deposit)/args.deposit)*100:.2f}%)")
    print(f"  Daily Net Profit: ${daily_avg_pnl:.2f} ({daily_return_pct:.3f}% / day)")
    print(f"  Number of Trades: {num_trades}")
    print(f"  Win Rate: {win_rate:.1f}%")
    print(f"  Avg Trade Duration: {avg_duration:.1f} minutes")
    print(f"  Total Funding Earned: ${funding_earned:.2f}")
    print(f"  Total Fees Paid: ${fees_paid:.2f}")
    print(f"  Max Drawdown (Trade-to-Trade): {max_dd:.2f}%")
    
    return {
        "coin": coin.upper(),
        "net_pnl": total_net_pnl,
        "daily_pnl": daily_avg_pnl,
        "daily_pct": daily_return_pct,
        "trades": num_trades,
        "win_rate": win_rate,
        "avg_duration_min": avg_duration,
        "funding": funding_earned,
        "fees": fees_paid,
        "max_dd_pct": max_dd
    }

def main():
    parser = argparse.ArgumentParser(description="Hyperliquid Spot-Perp Arbitrage Backtest Simulator")
    parser.add_argument("--coin", type=str, help="Coin symbol or comma-separated list (e.g. HYPE,WIF). If omitted, scans data directory.")
    parser.add_argument("--deposit", type=float, default=400.0, help="Initial deposit in USD")
    parser.add_argument("--max-pos_size", type=float, default=400.0, help="Maximum allocation per trade (e.g. $400 means full deposit)")
    parser.add_argument("--days", type=int, default=30, help="Number of days in the dataset")
    parser.add_argument("--entry", type=float, default=0.20, help="Entry basis premium threshold in %")
    parser.add_argument("--exit", type=float, default=0.02, help="Exit basis premium threshold in %")
    parser.add_argument("--fee", type=float, default=0.025, help="Taker fee rate in % (default 0.025%)")
    parser.add_argument("--slippage", type=float, default=0.02, help="Slippage rate on execution in % (default 0.02%)")
    parser.add_argument("--data-dir", type=str, default="data", help="Data directory containing JSON logs")
    args = parser.parse_args()
    
    data_path = Path(args.data_dir)
    
    # Determine which coins to run
    coins = []
    if args.coin:
        coins = [c.strip().upper() for c in args.coin.split(",")]
    else:
        # Scan data directory for hl_*_spot.json
        for f in data_path.glob("hl_*_spot.json"):
            coin_name = f.name.split("_")[1].upper()
            coins.append(coin_name)
            
    if not coins:
        print("No coin data files found in data directory.", file=sys.stderr)
        sys.exit(1)
        
    results = []
    for coin in coins:
        res = run_backtest_for_coin(coin, data_path, args)
        if "error" not in res:
            results.append(res)
            
    # Print summary table if multiple coins were evaluated
    if len(results) > 1:
        print("\n" + "="*80)
        print("SUMMARY OF SPOT-PERP ARBITRAGE BACKTESTS")
        print("="*80)
        print(f"{'COIN':<10} | {'NET PNL':<10} | {'DAILY PNL':<10} | {'DAILY %':<10} | {'TRADES':<8} | {'WIN %':<8} | {'MAX DD %':<10}")
        print("-"*80)
        for r in results:
            print(f"{r['coin']:<10} | ${r['net_pnl']:<9.2f} | ${r['daily_pnl']:<9.2f} | {r['daily_pct']:<8.3f}% | {r['trades']:<8} | {r['win_rate']:<6.1f}% | {r['max_dd_pct']:<9.2f}%")
        print("="*80)
        
        # Portfolio aggregate
        total_pnl = sum(r['net_pnl'] for r in results)
        total_daily = sum(r['daily_pnl'] for r in results)
        total_trades = sum(r['trades'] for r in results)
        print(f"{'TOTAL':<10} | ${total_pnl:<9.2f} | ${total_daily:<9.2f} | {(total_daily/args.deposit)*100:<8.3f}% | {total_trades:<8} |")
        print("="*80)

if __name__ == "__main__":
    main()
