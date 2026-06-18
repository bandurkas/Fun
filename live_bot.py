#!/usr/bin/env python3
import argparse
import json
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

STATE_DIR = Path(__file__).resolve().parent


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


def parse_fill(res) -> tuple:
    """Parses a Hyperliquid order response.

    Returns (ok, filled_sz, avg_px, detail). `ok` is True only if at least part
    of the order actually filled. The SDK wraps per-order results inside
    response.data.statuses[]; an outer status=="ok" can still contain an
    inner {"error": ...} or a resting (unfilled) order, so we must inspect it.
    """
    try:
        if not isinstance(res, dict) or res.get("status") != "ok":
            return (False, 0.0, 0.0, str(res))
        statuses = res["response"]["data"]["statuses"]
        total_sz = 0.0
        avg_px = 0.0
        errs = []
        for st in statuses:
            if "filled" in st:
                f = st["filled"]
                total_sz += float(f["totalSz"])
                avg_px = float(f["avgPx"])
            elif "error" in st:
                errs.append(st["error"])
            else:
                errs.append(f"unfilled: {st}")
        if total_sz > 0:
            return (True, total_sz, avg_px, errs or None)
        return (False, 0.0, 0.0, errs or "no fill")
    except Exception as e:
        return (False, 0.0, 0.0, f"parse error: {e} / {res}")


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
    spot_coin_id = spot_pair.get("name")  # e.g. "PURR/USDC" or "@107"

    logger.info(f"Resolved Spot: Symbol='{spot_coin_id}', Decimals={spot_decimals}")
    logger.info(f"Resolved Perp: Symbol='{perp_coin_id}', Decimals={perp_decimals}")

    return spot_coin_id, perp_coin_id, spot_decimals, perp_decimals


def get_spot_balance(info_client: Info, account_address: str, token_name: str) -> float:
    """Returns the actual spot balance held for `token_name` on the account."""
    try:
        st = info_client.spot_user_state(account_address)
        for b in st.get("balances", []):
            if b.get("coin", "").upper() == token_name.upper():
                return float(b.get("total", 0.0))
    except Exception as e:
        logger.error(f"Failed to query spot balance: {e}")
    return 0.0


def get_perp_position(info_client: Info, account_address: str, coin: str) -> float:
    """Returns signed perp position size for `coin` (negative = short)."""
    try:
        st = info_client.user_state(account_address)
        for ap in st.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin", "").upper() == coin.upper():
                return float(pos.get("szi", 0.0))
    except Exception as e:
        logger.error(f"Failed to query perp position: {e}")
    return 0.0


class PositionState:
    """In-memory + on-disk trade state so a restart never loses or double-opens a position."""

    def __init__(self, coin: str):
        self.coin = coin
        self.path = STATE_DIR / f"state_{coin.lower()}.json"
        self.in_position = False
        self.spot_size = 0.0
        self.perp_size = 0.0
        self.entry_t = 0.0
        self.entry_basis = 0.0

    def to_dict(self):
        return {
            "coin": self.coin,
            "in_position": self.in_position,
            "spot_size": self.spot_size,
            "perp_size": self.perp_size,
            "entry_t": self.entry_t,
            "entry_basis": self.entry_basis,
        }

    def save(self):
        try:
            self.path.write_text(json.dumps(self.to_dict(), indent=2))
        except Exception as e:
            logger.error(f"Failed to persist state: {e}")

    def load(self):
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text())
            self.in_position = d.get("in_position", False)
            self.spot_size = d.get("spot_size", 0.0)
            self.perp_size = d.get("perp_size", 0.0)
            self.entry_t = d.get("entry_t", 0.0)
            self.entry_basis = d.get("entry_basis", 0.0)
            logger.info(f"Loaded persisted state: {self.to_dict()}")
        except Exception as e:
            logger.error(f"Failed to load state file: {e}")


def close_everything(exchange_client, info_client, account_address, spot_coin, perp_coin,
                     coin, spot_size, slippage, reason: str) -> bool:
    """Flattens both legs: market_close the perp, sell the actual spot balance.

    Returns True if both legs ended flat. Uses reduce-only/full-position close so
    a size mismatch between legs can never flip us into a new directional position.
    """
    logger.warning(f"FLATTENING POSITION ({reason})...")
    ok = True

    # Perp: market_close closes the entire perp position (reduce-only by design).
    perp_pos = get_perp_position(info_client, account_address, perp_coin)
    if abs(perp_pos) > 0:
        perp_res = exchange_client.market_close(perp_coin, None, None, slippage)
        p_ok, p_sz, p_px, p_detail = parse_fill(perp_res)
        logger.info(f"Perp close: ok={p_ok} sz={p_sz} px={p_px} detail={p_detail}")
        ok = ok and p_ok
    else:
        logger.info("No open perp position to close.")

    # Spot: sell the actual on-chain balance (handles fee dust / partial fills).
    bal = get_spot_balance(info_client, account_address, coin)
    if bal <= 0 and spot_size > 0:
        bal = spot_size  # fall back to recorded size if balance query failed
    if bal > 0:
        spot_res = exchange_client.market_open(spot_coin, False, bal, None, slippage)
        s_ok, s_sz, s_px, s_detail = parse_fill(spot_res)
        logger.info(f"Spot sell: ok={s_ok} sz={s_sz} px={s_px} detail={s_detail}")
        ok = ok and s_ok
    else:
        logger.info("No spot balance to sell.")

    return ok


def main():
    parser = argparse.ArgumentParser(description="Hyperliquid Spot-Perp Live Arbitrage Bot")
    parser.add_argument("--coin", type=str, default="PURR", help="Coin to trade (e.g. PURR, HYPE)")
    parser.add_argument("--dry-run", action="store_true", help="Run in dry-run simulation mode (no real orders)")
    parser.add_argument("--entry", type=float, default=0.80, help="Entry basis threshold in %")
    parser.add_argument("--exit", type=float, default=0.05, help="Exit basis threshold in %")
    parser.add_argument("--size-usd", type=float, default=35.0, help="Order size per leg in USD (default $35, total position $70)")
    parser.add_argument("--slippage", type=float, default=0.005, help="Slippage tolerance for market orders as a fraction (0.005 = 0.5%)")
    parser.add_argument("--timeout-min", type=float, default=720.0, help="Force-exit a position after this many minutes")
    parser.add_argument("--min-notional", type=float, default=10.0, help="Minimum order notional in USD enforced by the exchange")
    args = parser.parse_args()

    # Load environment variables
    load_dotenv()

    is_testnet = os.getenv("HL_IS_TESTNET", "True").lower() == "true"
    base_url = constants.TESTNET_API_URL if is_testnet else constants.MAINNET_API_URL

    logger.info("=" * 60)
    logger.info("HYPERLIQUID SPOT-PERP LIVE ARBITRAGE BOT")
    logger.info("=" * 60)
    logger.info(f"Mode: {'TESTNET' if is_testnet else 'MAINNET'}")
    logger.info(f"Target: {args.coin}")
    logger.info(f"Dry-run: {args.dry_run}")
    logger.info(f"Parameters: Entry={args.entry}%, Exit={args.exit}%, Size per leg=${args.size_usd}, Slippage={args.slippage*100:.2f}%")
    logger.info("=" * 60)

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
        if account_address.upper().startswith("0XYOUR") or len(account_address) != 42:
            logger.error("Error: HL_ACCOUNT_ADDRESS looks like a placeholder/invalid. Set your MAIN wallet address.")
            sys.exit(1)

        wallet = eth_account.Account.from_key(private_key)
        exchange_client = Exchange(wallet, base_url, account_address=account_address)
        logger.info(f"Initialized live Exchange client for account: {account_address}")
        logger.info(f"API wallet (signer) address: {wallet.address}")

    # 1b. Restore + reconcile position state
    state = PositionState(args.coin)
    state.load()
    if not args.dry_run:
        actual_perp = get_perp_position(info_client, account_address, perp_coin)
        actual_spot = get_spot_balance(info_client, account_address, args.coin)
        logger.info(f"Exchange snapshot at startup: perp_pos={actual_perp}, spot_balance={actual_spot}")
        exchange_flat = (abs(actual_perp) < 1e-9 and actual_spot < 1e-9)
        if state.in_position and exchange_flat:
            logger.warning("State file says IN POSITION but exchange is flat. Resetting to flat.")
            state.in_position = False
            state.save()
        elif not state.in_position and not exchange_flat:
            logger.error("Exchange shows an OPEN position but state file is flat. "
                         "Refusing to start to avoid double-opening. Flatten manually or fix state file.")
            sys.exit(1)

    # 2. Start WebSocket Manager
    logger.info("Starting WebSocket connection...")
    ws_manager = WebsocketManager(base_url)
    ws_manager.start()

    spot_sub = {"type": "l2Book", "coin": spot_coin}
    perp_sub = {"type": "l2Book", "coin": perp_coin}
    ws_manager.subscribe(spot_sub, handle_spot_book)
    ws_manager.subscribe(perp_sub, handle_perp_book)
    logger.info(f"Subscribed to Spot L2 ({spot_coin}) and Perp L2 ({perp_coin})")

    logger.info("Bot is running and listening for market data. Press Ctrl+C to exit.")

    try:
        while True:
            time.sleep(2.0)
            now = time.time()

            with state_lock:
                spot_age = now - spot_book["last_update_t"]
                perp_age = now - perp_book["last_update_t"]
                spot_bids_snap = list(spot_book["bids"])
                spot_asks_snap = list(spot_book["asks"])
                perp_bids_snap = list(perp_book["bids"])
                perp_asks_snap = list(perp_book["asks"])

            data_fresh = (spot_age <= 10.0 and perp_age <= 10.0
                          and spot_asks_snap and perp_bids_snap
                          and spot_bids_snap and perp_asks_snap)

            if state.in_position:
                elapsed_m = (now - state.entry_t) / 60.0

                # Force-exit on timeout regardless of book freshness (market_close
                # does not need the book), so a stale WS can never trap a position.
                if elapsed_m >= args.timeout_min:
                    logger.warning(f"!!! EXIT (TIMEOUT {args.timeout_min:.0f}m) at {elapsed_m:.1f}m !!!")
                    if args.dry_run:
                        state.in_position = False
                        state.save()
                    else:
                        if close_everything(exchange_client, info_client, account_address,
                                            spot_coin, perp_coin, args.coin, state.spot_size,
                                            args.slippage, "timeout"):
                            state.in_position = False
                            state.spot_size = state.perp_size = 0.0
                            state.save()
                            logger.info("Position flattened on timeout.")
                        else:
                            logger.error("Timeout flatten did not fully close. Will retry next loop.")
                    continue

                if not data_fresh:
                    logger.warning(f"Stale book while holding (spot {spot_age:.1f}s / perp {perp_age:.1f}s). Waiting...")
                    continue

                spot_bid = get_weighted_average_price(spot_bids_snap, args.size_usd)
                perp_ask = get_weighted_average_price(perp_asks_snap, args.size_usd)
                if spot_bid == 0.0 or perp_ask == 0.0:
                    logger.warning("Orderbook depth not sufficient for target size.")
                    continue

                basis = (perp_ask - spot_bid) / spot_bid * 100.0
                logger.info(f"HOLDING - Spot Bid: {spot_bid:.6f} | Perp Ask: {perp_ask:.6f} | "
                            f"Basis: {basis:.3f}% (Exit <= {args.exit}%) | Duration: {elapsed_m:.1f}m")

                if basis <= args.exit:
                    logger.info("!!! EXIT SIGNAL (CONVERGENCE) !!!")
                    if args.dry_run:
                        logger.info(f"[DRY-RUN] Simulated exit at basis {basis:.3f}%")
                        state.in_position = False
                        state.save()
                    else:
                        if close_everything(exchange_client, info_client, account_address,
                                            spot_coin, perp_coin, args.coin, state.spot_size,
                                            args.slippage, "convergence"):
                            state.in_position = False
                            state.spot_size = state.perp_size = 0.0
                            state.save()
                            logger.info("Position flattened on convergence.")
                        else:
                            logger.error("Convergence flatten did not fully close. Will retry next loop.")
                continue

            # --- Not in position: look for entry ---
            if not data_fresh:
                logger.warning(f"Waiting for fresh WebSocket data (spot {spot_age:.1f}s / perp {perp_age:.1f}s)...")
                continue

            spot_ask = get_weighted_average_price(spot_asks_snap, args.size_usd)
            perp_bid = get_weighted_average_price(perp_bids_snap, args.size_usd)
            if spot_ask == 0.0 or perp_bid == 0.0:
                logger.warning("Orderbook depth not sufficient for target size.")
                continue

            basis = (perp_bid - spot_ask) / spot_ask * 100.0
            logger.info(f"MONITORING - Spot Ask: {spot_ask:.6f} | Perp Bid: {perp_bid:.6f} | "
                        f"Basis Premium: {basis:.3f}% (Entry >= {args.entry}%)")

            if basis < args.entry:
                continue

            coin_size = (args.size_usd * 2.0) / (spot_ask + perp_bid)
            coin_size = round(coin_size, common_decimals)
            if coin_size <= 0.0:
                logger.error(f"Calculated size is 0 after rounding (common_decimals={common_decimals}).")
                continue
            if coin_size * spot_ask < args.min_notional or coin_size * perp_bid < args.min_notional:
                logger.warning(f"Order notional below exchange minimum ${args.min_notional}. Skipping.")
                continue

            logger.info("!!! ENTRY SIGNAL TRIGGERED !!!")
            logger.info(f"Target size: {coin_size} {args.coin} "
                        f"(Spot ${coin_size * spot_ask:.2f} / Perp ${coin_size * perp_bid:.2f})")

            if args.dry_run:
                logger.info(f"[DRY-RUN] Simulated entry of {coin_size} tokens at basis {basis:.3f}%")
                state.in_position = True
                state.spot_size = coin_size
                state.perp_size = coin_size
                state.entry_t = now
                state.entry_basis = basis
                state.save()
                continue

            # --- LIVE entry with atomic-hedge rollback ---
            logger.info("[LIVE] Entering: buy spot, then short perp...")
            spot_res = exchange_client.market_open(spot_coin, True, coin_size, None, args.slippage)
            s_ok, s_sz, s_px, s_detail = parse_fill(spot_res)
            logger.info(f"Spot buy: ok={s_ok} sz={s_sz} px={s_px} detail={s_detail}")

            perp_res = exchange_client.market_open(perp_coin, False, coin_size, None, args.slippage)
            p_ok, p_sz, p_px, p_detail = parse_fill(perp_res)
            logger.info(f"Perp short: ok={p_ok} sz={p_sz} px={p_px} detail={p_detail}")

            if s_ok and p_ok:
                state.in_position = True
                state.spot_size = s_sz
                state.perp_size = p_sz
                state.entry_t = now
                state.entry_basis = basis
                state.save()
                if abs(s_sz - p_sz) > (10 ** -common_decimals):
                    logger.warning(f"Leg size mismatch (spot {s_sz} vs perp {p_sz}). "
                                   f"Exit will flatten each leg independently.")
                logger.info("Live entry executed. Now holding delta-neutral position.")
            else:
                # One or both legs failed -> immediately flatten whatever filled.
                logger.error("Entry leg failure detected. Rolling back filled leg(s)...")
                flat = close_everything(exchange_client, info_client, account_address,
                                        spot_coin, perp_coin, args.coin, s_sz if s_ok else 0.0,
                                        args.slippage, "entry-rollback")
                state.in_position = False
                state.spot_size = state.perp_size = 0.0
                state.save()
                if flat:
                    logger.info("Rollback complete; account is flat. Continuing to monitor.")
                else:
                    logger.error("ROLLBACK FAILED — possible naked exposure. Halting for manual check.")
                    break

    except KeyboardInterrupt:
        logger.info("Shutting down bot...")
    finally:
        logger.info("Stopping WebSocket client...")
        ws_manager.stop()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
