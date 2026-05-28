#!/usr/bin/env bash
set -e

start_xvfb() {
  rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
  Xvfb :99 -screen 0 1366x768x24 -nolisten tcp &
  XVFB_PID=$!
}

start_xvfb

# Если Xvfb упадёт — перезапускаем, чтобы Chromium всегда мог приконнектиться.
(
  while true; do
    sleep 5
    if ! kill -0 "$XVFB_PID" 2>/dev/null; then
      echo "[entrypoint] Xvfb died, restarting" >&2
      start_xvfb
    fi
  done
) &

export DISPLAY=:99
sleep 1
exec python -u bot.py
