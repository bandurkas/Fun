# Project Handoff - Hyperliquid Spot-Perp Arbitrage Bot

This document outlines the current state, architecture, and deployment status of the Hyperliquid Spot-Perp Arbitrage Bot to enable a seamless handoff for the next AI agent or developer.

---

## 1. Project Overview & Strategy
* **Goal:** Delta-neutral spot-perp convergence arbitrage on Hyperliquid, targeted at $PURR$ (and optionally $HYPE$).
* **Pilot Balance:** $70 USDC ($35 per leg) targeting $0.50 - $1.00 daily profit.
* **Key Strategy details:**
  * **Delta-Neutral Sizing:** Matching size in coins ($N = \text{Balance} / (S + P)$) to maintain neutrality regardless of token price fluctuations.
  * **Liquidity Filtering:** High-resolution backtests filtered out empty minutes (volume = 0) to avoid phantom basis profits from illiquid coins. PURR remains highly liquid.
  * **Simultaneous Market Execution:** Spot and Perp legs are opened and closed simultaneously using taker market orders (`exchange.market_open`) to eliminate execution leg risk on small balances.

---

## 2. Codebase Structure (Local: `/Users/sabar/Desktop/Fun`)
* **`live_bot.py`**: The real-time bot client.
  * Subscribes to Hyperliquid L2 orderbook WebSockets (`l2Book`) for Spot and Perp.
  * Dynamically computes the weighted average entry/exit price for the target USD volume ($35 leg) by walking the L2 bids/asks book.
  * Calculates real-time spread premiums and executes Taker/Taker orders when signals trigger.
* **`fetch_hl_data.py`**: CLI script to download historical candle data from Hyperliquid.
* **`spot_perp_arbitrage_backtest.py`**: Backtest engine simulating the spot-perp basis strategy.
* **`run_bot.sh`**: Management wrapper script to run, stop, monitor, and tail logs of the bot in a `screen` session.
* **`requirements.txt`**: Project dependencies (`hyperliquid-python-sdk`, `python-dotenv`, `requests`, `eth-account`).
* **`.env.example`**: Template for credentials.

---

## 3. Deployment Status (VPS 2: `168.231.118.173`, root)
* **Code Location:** Cloned at `/root/Fun`.
* **Python Environment:** Virtualenv configured at `/root/Fun/.venv` with Python 3.12.3. All dependencies installed.
* **Runner Utility:** `/root/Fun/run_bot.sh` is configured and executable.
* **Verification:** Run in `--dry-run` mode on Testnet. Connection established, book subscriptions completed, and basis calculations logged successfully.
* **Current Config:** `/root/Fun/.env` is copied from the example template but **credentials are not yet entered**.

---

## 4. Next Steps for the Incoming Agent
1. **API Keys Setup:** Confirm if the user has updated `/root/Fun/.env` with:
   * `HL_PRIVATE_KEY` (API wallet private key with trade-only permissions, NOT main wallet key).
   * `HL_ACCOUNT_ADDRESS` (Main wallet EVM address).
   * `HL_IS_TESTNET` (set to `False` for Mainnet, `True` for Testnet).
2. **Testnet / Dry-run Validation:**
   * Start the bot in background dry-run mode with credentials loaded to check EVM signature authentication:
     ```bash
     /root/Fun/run_bot.sh start --coin PURR --dry-run
     ```
   * Tail logs to check for any authentication issues:
     ```bash
     /root/Fun/run_bot.sh logs
     ```
   * Stop the dry-run:
     ```bash
     /root/Fun/run_bot.sh stop
     ```
3. **Go Live:**
   * Ensure `HL_IS_TESTNET=False` is set in `.env`.
   * Start the live bot:
     ```bash
     /root/Fun/run_bot.sh start --coin PURR
     ```
   * Monitor live positions, order fills, and spread convergence via the logs.
