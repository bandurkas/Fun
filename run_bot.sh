#!/bin/bash

SESSION_NAME="hl-bot"
BOT_DIR="/root/Fun"
VENV_PYTHON="$BOT_DIR/.venv/bin/python"
BOT_SCRIPT="$BOT_DIR/live_bot.py"
LOG_FILE="$BOT_DIR/bot.log"

case "$1" in
  start)
    if screen -list | grep -q "\.${SESSION_NAME}"; then
      echo "Bot is already running in screen session '${SESSION_NAME}'."
      exit 1
    fi
    shift
    echo "Starting bot in screen session '${SESSION_NAME}'..."
    # Start bot in screen detached mode and write to log file
    screen -dmS "${SESSION_NAME}" bash -c "cd ${BOT_DIR} && ${VENV_PYTHON} ${BOT_SCRIPT} $@ 2>&1 | tee -a ${LOG_FILE}"
    echo "Bot started. You can view logs with: ./run_bot.sh logs"
    echo "Or attach to the screen session with: screen -r ${SESSION_NAME}"
    ;;
  stop)
    if ! screen -list | grep -q "\.${SESSION_NAME}"; then
      echo "Bot is not running."
      exit 1
    fi
    echo "Stopping screen session '${SESSION_NAME}'..."
    screen -S "${SESSION_NAME}" -X quit
    echo "Bot stopped."
    ;;
  status)
    if screen -list | grep -q "\.${SESSION_NAME}"; then
      echo "Bot status: RUNNING"
      screen -list | grep "${SESSION_NAME}"
    else
      echo "Bot status: STOPPED"
    fi
    ;;
  logs)
    if [ -f "${LOG_FILE}" ]; then
      tail -n 50 "${LOG_FILE}"
    else
      echo "No log file found at ${LOG_FILE}"
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|status|logs} [bot arguments...]"
    echo "Example: $0 start --coin PURR --dry-run"
    exit 1
    ;;
esac
