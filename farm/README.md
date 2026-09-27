# ☁️ Cloud phone farm

Conference apps have no API. The farm gives every user a real Android phone in the browser: they install the
conference app, log in, open the attendee list — and the agent drives it from there over ADB, reading the
accessibility tree (names/titles as text, no OCR) and tapping by element.

**Free-tier design.** Runs anywhere the Android emulator runs: your Mac, a Linux box with KVM, or an Oracle
Always-Free ARM VM. No per-device cloud fees. Expose it with a free Cloudflare quick tunnel.

## One-time setup (macOS, Apple Silicon)

```bash
brew install openjdk@17 android-commandlinetools
export JAVA_HOME=/opt/homebrew/opt/openjdk@17 ANDROID_HOME=/opt/homebrew/share/android-commandlinetools
export PATH=$JAVA_HOME/bin:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/emulator:$ANDROID_HOME/platform-tools:$PATH
yes | sdkmanager --licenses
sdkmanager platform-tools emulator "system-images;android-34;google_apis_playstore;arm64-v8a"
echo no | avdmanager create avd -n farm0 -k "system-images;android-34;google_apis_playstore;arm64-v8a" -d pixel_6
```

Linux (x86_64, needs `/dev/kvm`): same, with `x86_64` in place of `arm64-v8a`.

## Run

```bash
FARM_TOKEN=some-secret TUNNEL=1 ./farm/start_farm.sh
# prints FARM_URL=https://xxxx.trycloudflare.com and FARM_TOKEN
```

Put those two values in the web app sidebar (or as `FARM_URL` / `FARM_TOKEN` secrets on Streamlit Cloud).
Each "Get a cloud phone" click clones `farm0`, boots it headless (~15–30 s), and shows it in the browser.
`FARM_MAX_SESSIONS` (default 3) caps concurrent phones; idle sessions are reclaimed after 2 h.

## How the agent drives an unknown app

`farm/workflow.py` is app-agnostic:

1. **Capture** — scroll the visible list, read `uiautomator dump`, group text nodes into rows, guess
   name / title / company, dedupe, stop when nothing new appears. An LLM pass removes UI chrome.
2. **Send** — find the search box (heuristics on labels/ids → LLM picks from visible labels if needed),
   type the name, open the matching result, **verify the name is on the profile**, find the message /
   request-meeting action, type, tap send, go back. Every element choice is saved to
   `~/.conference-agent/android_recipes/<package>.json` so the next contact is deterministic.

Dry-run mode composes and screenshots without tapping Send.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | SDK + capacity check |
| POST | `/sessions` | boot a phone → `{id, serial}` |
| GET | `/sessions/{id}` | status: booting / ready / error |
| GET | `/sessions/{id}/view?token=` | browser remote control (iframe-able) |
| GET | `/sessions/{id}/screen.png` | live frame (JPEG, half-res) |
| POST | `/sessions/{id}/tap|swipe|type|key/{k}` | manual control |
| POST | `/sessions/{id}/install` | `{apk_url}` or `{package}` (opens Play Store) |
| POST | `/sessions/{id}/capture` | start attendee capture → job id |
| POST | `/sessions/{id}/send` | send approved messages → job id |
| GET | `/jobs/{id}` | progress / results |
| DELETE | `/sessions/{id}` | release |

All except `/health` and `/view` need `X-Farm-Token`.

## Notes

- Play Store image requires the user to sign into a Google account on the phone once per session
  (or use `install` with a direct APK URL for apps that publish one).
- Streaming is screenshot polling (~1 fps over a tunnel). `farm/ws-scrcpy` is checked out for a
  higher-fps H.264 stream if you want it — run `node dist/index.js` and embed port 8000 instead.
- Memory: ~2 GB per emulator. 3 sessions is comfortable on a 16 GB Mac.
