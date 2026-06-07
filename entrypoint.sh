#!/usr/bin/env bash
# Start a virtual display, then run the bot HEADED inside it.
# ShipCSX's Angular form renders reliably in a real (headed) browser; pure
# headless Chromium sometimes comes up blank. We manage Xvfb ourselves instead
# of using `xvfb-run` (which could hang on this image).
set -e

# Launch Xvfb in the background on display :99.
Xvfb :99 -screen 0 1366x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
export DISPLAY=:99

# Give Xvfb a moment to come up.
sleep 1.5

echo "entrypoint: Xvfb started on :99, launching bot..."
exec python bot.py
