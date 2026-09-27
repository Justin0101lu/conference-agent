#!/bin/bash
# Start the Conference Agent phone farm on this machine (macOS or Linux with Android SDK).
# Usage: ./farm/start_farm.sh            -> API on :8765, prints FARM_TOKEN
#        TUNNEL=1 ./farm/start_farm.sh   -> also opens a free Cloudflare quick tunnel and prints the public URL
set -e
cd "$(dirname "$0")/.."
export JAVA_HOME=${JAVA_HOME:-/opt/homebrew/opt/openjdk@17}
export ANDROID_HOME=${ANDROID_HOME:-/opt/homebrew/share/android-commandlinetools}
export ANDROID_SDK_ROOT=$ANDROID_HOME
export PATH=$JAVA_HOME/bin:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH
export FARM_TOKEN=${FARM_TOKEN:-$(python3 -c 'import secrets;print(secrets.token_urlsafe(16))')}
export FARM_PORT=${FARM_PORT:-8765}
export FARM_MAX_SESSIONS=${FARM_MAX_SESSIONS:-3}
export FARM_BASE_AVD=${FARM_BASE_AVD:-farm_play}
mkdir -p ~/.conference-agent/farm
echo "FARM_TOKEN=$FARM_TOKEN" | tee ~/.conference-agent/farm/token.env
PY=${PY:-.venv/bin/python}
nohup $PY -m uvicorn conference_agent.farm.server:app --host 0.0.0.0 --port "$FARM_PORT" > ~/.conference-agent/farm/server.log 2>&1 &
disown
echo "farm api pid $! on http://localhost:$FARM_PORT"
if [ "${TUNNEL:-0}" = "1" ]; then
  command -v cloudflared >/dev/null || brew install cloudflared
  nohup cloudflared tunnel --url "http://localhost:$FARM_PORT" > ~/.conference-agent/farm/tunnel.log 2>&1 &
  disown
  sleep 10
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' ~/.conference-agent/farm/tunnel.log | head -1)
  echo "FARM_URL=$URL" | tee -a ~/.conference-agent/farm/token.env
fi
