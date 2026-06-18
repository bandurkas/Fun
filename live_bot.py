#!/usr/bin/env python3
import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path
from dotenv import load_dotenv
import eth_account

# Import Hyperliquid SDK
from hyperliquid.utils import constants
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.websocket_manager import WebsocketManager

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("LiveBot")

# Global state for WebSocket L2 Books
state_lock = threading.Lock()
spot_book = {"bids": [], "asks": [], "last_update_t": 0.0}
perp_book = {"bids": [], "asks": [], "last_update_t": 0.0}

def handle_spot_book(msg):
    global spot_book
    data = msg.get("data")
    if data and "levels" in data:
        with state_lock:
            spot_book["bids"] = data["levels"][0]
            spot_book["asks"] = data["levels"][1]
            spot_book["last_update_t"] = time.time()

def handle_perp_book(msg):
    global perp_book
    data = msg.get("data")
    if data and "levels" in data:
        with state_lock:
            perp_book["bids"] = data["levels"][0]
            perp_book["asks"] = data["levels"][1]
            perp_book["last_update_t"] = time.time()

def get_weighted_average_price(levels: list, target_usd: float) -> float:
    """Calculates weighted average price to buy/sell target_usd from L2 book levels."""
    accumulated_usd = 0.0
    accumulated_qty = 0.0
    for level in levels:
        px = float(level["px"])
        sz = float(level["sz"])
        usd = px * sz
        if accumulated_usd + usd >= target_usd:
            needed_usd = target_usd - accumulated_usd
            needed_qty = needed_usd / px
            accumulated_qty += needed_qty
            accumulated_usd = target_usd
            break
        else:
            accumulated_qty += sz
            accumulated_usd += usd
            
    if accumulated_usd < target_usd:
        # Not enough depth in the book
        return 0.0
    return accumulated_usd / accumulated_qty

def resolve_decimals_and_symbols(coin: str, info_client: Info):
    """Resolves correct symbol strings and decimals for Spot and Perp."""
    logger.info(f"Querying exchange metadata for {coin}...")
    
    # 1. Resolve Perp Metadata
    meta = info_client.meta()
    perp_asset = next((u for u in meta.get("universe", []) if u.get("name") == coin.upper()), None)
    if not perp_asset:
        raise ValueError(f"Could not find perpetual asset '{coin}' in meta.")
    perp_decimals = perp_asset["szDecimals"]
    perp_coin_id = coin.upper()
    
    # 2. Resolve Spot Metadata
    spot_meta = info_client.spot_meta()
    tokens = spot_meta.get("tokens", [])
    universe = spot_meta.get("universe", [])
    
    spot_token = next((t for t in tokens if t.get("name", "").upper() == coin.upper()), None)
    if not spot_token:
        raise ValueError(f"Could not find token '{coin}' in spotMeta tokens.")
    token_idx = spot_token["index"]
    spot_decimals = spot_token["szDecimals"]
    
    spot_pair = next((p for p in universe if p.get("tokens", [])[0] == token_idx), None)
    if not spot_pair:
        raise ValueError(f"Could not find spot pair in universe for token '{coin}'")
    spot_coin_id = spot_pair.get("name") # e.g. "PURR/USDC" or "@107"
    
    logger.info(f"Resolved Spot: Symbol='{spot_coin_id}', Decimals={spot_decimals}")
    logger.info(f"Resolved Perp: Symbol='{perp_coin_id}', Decimals={perp_decimals}")
    
    return spot_coin_id, perp_coin_id, spot_decimals, perp_decimals

def main():
    parser = argparse.ArgumentParser(description="Hyperliquid Spot-Perp Live Arbitrage Bot")
    parser.add_argument("--coin", type=str, default="PURR", help="Coin to trade (e.g. PURR, HYPE)")
    parser.add_argument("--dry-run", action="store_true", help="Run in dry-run simulation mode (no real orders)")
    parser.add_argument("--entry", type=float, default=0.80, help="Entry basis threshold in %")
    parser.add_argument("--exit", type=float, default=0.05, help="Exit basis threshold in %")
    parser.add_argument("--size-usd", type=float, default=35.0, help="Order size per leg in USD (default $35, total position $70)")
    parser.add_argument("--slippage", type=float, default=0.01, help="Slippage tolerance for market orders (default 1%)")
    args = parser.parse_args()

    # Load environment variables
    load_dotenv()
    
    is_testnet = os.getenv("HL_IS_TESTNET", "True").lower() == "true"
    base_url = constants.TESTNET_API_URL if is_testnet else constants.MAINNET_API_URL
    
    logger.info("="*60)
    logger.info("HYPERLIQUID SPOT-PERP LIVE ARBITRAGE BOT")
    logger.info("="*60)
    logger.info(f"Mode: {'TESTNET' if is_testnet else 'MAINNET'}")
    logger.info(f"Target: {args.coin}")
    logger.info(f"Dry-run: {args.dry_run}")
    logger.info(f"Parameters: Entry={args.entry}%, Exit={args.exit}%, Size per leg=${args.size_usd}")
    logger.info("="*60)
    
    # 1. Initialize Clients
    info_client = Info(base_url, skip_ws=True)
    
    try:
        spot_coin, perp_coin, spot_decimals, perp_decimals = resolve_decimals_and_symbols(args.coin, info_client)
    except Exception as e:
        logger.error(f"Failed to resolve metadata: {e}")
        sys.exit(1)
        
    # Calculate common precision to keep sizes perfectly matched
    common_decimals = min(spot_decimals, perp_decimals)
    
    exchange_client = None
    account_address = None
    if not args.dry_run:
        private_key = os.getenv("HL_PRIVATE_KEY")
        account_address = os.getenv("HL_ACCOUNT_ADDRESS")
        if not private_key or not account_address:
            logger.error("Error: HL_PRIVATE_KEY and HL_ACCOUNT_ADDRESS must be set in .env for live trading.")
            sys.exit(1)
            
        wallet = eth_account.Account.from_key(private_key)
        exchange_client = Exchange(wallet, base_url, account_address=account_address)
        logger.info(f"Initialized live Exchange client for account: {account_address}")

    # 2. Start WebSocket Manager
    logger.info("Starting WebSocket connection...")
    ws_manager = WebsocketManager(base_url)
    ws_manager.start()
    
    # Subscribe to L2 Books
    spot_sub = {"type": "l2Book", "coin": spot_coin}
    perp_sub = {"type": "l2Book", "coin": perp_coin}
    
    ws_manager.subscribe(spot_sub, handle_spot_book)
    ws_manager.subscribe(perp_sub, handle_perp_book)
    logger.info(f"Subscribed to Spot L2 ({spot_coin}) and Perp L2 ({perp_coin})")
    
    # 3. Main Trading Loop
    in_position = False
    pos_size = 0.0
    entry_t = 0.0
    
    logger.info("Bot is running and listening for market data. Press Ctrl+C to exit.")
    
    try:
        while True:
            time.sleep(2.0) # Check every 2 seconds
            
            with state_lock:
                spot_age = time.time() - spot_book["last_update_t"]
                perp_age = time.time() - perp_book["last_update_t"]
                
                # Check for WebSocket stale data
                if spot_age > 10.0 or perp_age > 10.0 or not spot_book["asks"] or not perp_book["bids"]:
                    logger.warning(f"Waiting for WebSocket data... (Spot age: {spot_age:.1f}s, Perp age: {perp_age:.1f}s)")
                    continue
                    
                # Take snapshots under lock
                spot_bids_snap = list(spot_book["bids"])
                spot_asks_snap = list(spot_book["asks"])
                perp_bids_snap = list(perp_book["bids"])
                perp_asks_snap = list(perp_book["asks"])
                
            # Calculate execution prices
            if not in_position:
                # To Enter: Buy Spot (asks) and Short Perp (bids)
                spot_ask = get_weighted_average_price(spot_asks_snap, args.size_usd)
                perp_bid = get_weighted_average_price(perp_bids_snap, args.size_usd)
                
                if spot_ask == 0.0 or perp_bid == 0.0:
                    logger.warning("Orderbook depth not sufficient for target size.")
                    continue
                    
                basis = (perp_bid - spot_ask) / spot_ask * 100.0
                logger.info(f"MONITORING - Spot Ask: {spot_ask:.6f} | Perp Bid: {perp_bid:.6f} | Basis Premium: {basis:.3f}% (Entry target >= {args.entry}%)")
                
                # Check entry trigger
                if basis >= args.entry:
                    # Sizing: equal coin size
                    coin_size = (args.size_usd * 2.0) / (spot_ask + perp_bid)
                    coin_size = round(coin_size, common_decimals)
                    
                    if coin_size == 0.0:
                        logger.error(f"Calculated size is 0.0 due to decimal rounding (common_decimals={common_decimals}).")
                        continue
                        
                    logger.info(f"!!! ENTRY SIGNAL TRIGGERED !!!")
                    logger.info(f"Target size: {coin_size} {args.coin} (Spot value: ${coin_size * spot_ask:.2f}, Perp value: ${coin_size * perp_bid:.2f})")
                    
                    if args.dry_run:
                        logger.info(f"[DRY-RUN] Simulated entry of {coin_size} tokens at basis {basis:.3f}%")
                        pos_size = coin_size
                        in_position = True
                        entry_t = time.time()
                    else:
                        logger.info("[LIVE] Submitting simultaneous market orders...")
                        # Spot Buy (Long)
                        spot_res = exchange_client.market_open(spot_coin, True, coin_size, None, args.slippage)
                        # Perp Sell (Short)
                        perp_res = exchange_client.market_open(perp_coin, False, coin_size, None, args.slippage)
                        
                        logger.info(f"Spot order result: {spot_res}")
                        logger.info(f"Perp order result: {perp_res}")
                        
                        # Verify execution status
                        if spot_res.get("status") == "ok" and perp_res.get("status") == "ok":
                            logger.info("Live entry executed successfully!")
                            pos_size = coin_size
                            in_position = True
                            entry_t = time.time()
                        else:
                            logger.error("Failed to execute one or both legs. Manual check required!")
                            # For safety, if one leg failed, we should theoretically close the other, 
                            # but in live bot we halt and print error to prevent further issues.
                            break
                            
            else:
                # To Exit: Sell Spot (bids) and Buy Perp (asks)
                spot_bid = get_weighted_average_price(spot_bids_snap, args.size_usd)
                perp_ask = get_weighted_average_price(perp_asks_snap, args.size_usd)
                
                if spot_bid == 0.0 or perp_ask == 0.0:
                    logger.warning("Orderbook depth not sufficient for target size.")
                    continue
                    
                basis = (perp_ask - spot_bid) / spot_bid * 100.0
                elapsed_m = (time.time() - entry_t) / 60.0
                logger.info(f"HOLDING - Spot Bid: {spot_bid:.6f} | Perp Ask: {perp_ask:.6f} | Basis: {basis:.3f}% (Exit target <= {args.exit}%) | Duration: {elapsed_m:.1f}m")
                
                # Check exit triggers (convergence or 12-hour timeout)
                is_converged = basis <= args.exit
                is_timeout = elapsed_m >= 720.0 # 12 hours
                
                if is_converged or is_timeout:
                    if is_converged:
                        logger.info(f"!!! EXIT SIGNAL TRIGGERED (CONVERGENCE) !!!")
                    else:
                        logger.warning(f"!!! EXIT SIGNAL TRIGGERED (TIMEOUT 12H) !!!")
                        
                    if args.dry_run:
                        logger.info(f"[DRY-RUN] Simulated exit of {pos_size} tokens at basis {basis:.3f}%")
                        in_position = False
                        pos_size = 0.0
                    else:
                        logger.info("[LIVE] Submitting simultaneous exit market orders...")
                        # Spot Sell (Close Long)
                        spot_res = exchange_client.market_open(spot_coin, False, pos_size, None, args.slippage)
                        # Perp Buy (Close Short)
                        perp_res = exchange_client.market_open(perp_coin, True, pos_size, None, args.slippage)
                        
                        logger.info(f"Spot exit result: {spot_res}")
                        logger.info(f"Perp exit result: {perp_res}")
                        
                        if spot_res.get("status") == "ok" and perp_res.get("status") == "ok":
                            logger.info("Live exit executed successfully!")
                            in_position = False
                            pos_size = 0.0
                        else:
                            logger.error("Failed to execute one or both exit legs. Manual action required!")
                            break
                            
    except KeyboardInterrupt:
        logger.info("Shutting down bot...")
    finally:
        logger.info("Stopping WebSocket client...")
        ws_manager.stop()
        logger.info("Shutdown complete.")

if __name__ == "__main__":
    main()
