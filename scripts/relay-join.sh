#!/bin/bash
# Register this computer on a self-hosted relay and point the host config at it.
# Usage: CUA_RELAY_URL=https://relay.example.com bash scripts/relay-join.sh
set -euo pipefail

RELAY="${CUA_RELAY_URL:?set CUA_RELAY_URL}"
APP="${CUA_SPACES_APP:-/Applications/Cua Spaces.app}"
LABEL="com.cua-unlimit.host"
HOST_DIR="$HOME/.cua/host"
HELPER_DIR="$HOST_DIR/bin"
HELPER="$HELPER_DIR/cua-spacesd"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

fail() { echo "error: $*" >&2; exit 1; }

[ -d "$APP" ] || fail "Cua Spaces not found at $APP"
[ -f "$HOST_DIR/config.json" ] || fail "missing $HOST_DIR/config.json (open Cua Spaces once first)"
[ -f "$HOME/.cua/spacesd/id" ] || fail "missing ~/.cua/spacesd/id (open Cua Spaces once first)"

if [ -n "${CUA_ACCOUNT_TOKEN:-}" ]; then
  TOKEN="$CUA_ACCOUNT_TOKEN"
else
  [ -f "$HOME/.cua/credentials.json" ] || fail "missing ~/.cua/credentials.json (sign in to Cua Spaces, or set CUA_ACCOUNT_TOKEN)"
  TOKEN=$(python3 -c "import json;print(json.load(open('$HOME/.cua/credentials.json'))['access_token'])")
fi
MID=$(tr -d '[:space:]' < "$HOME/.cua/spacesd/id")
NAME=$(scutil --get ComputerName 2>/dev/null || hostname)

echo "relay:   $RELAY"
echo "machine: $MID ($NAME)"

# Register the machine. A 200 or 201 returns a fresh machine token and JWKS.
BODY=$(mktemp)
trap 'rm -f "$BODY"' EXIT
CODE=$(curl -sS -o "$BODY" -w '%{http_code}' -X POST "$RELAY/v1/machines" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "$(python3 -c "import json,sys;print(json.dumps({'id':sys.argv[1],'name':sys.argv[2]}))" "$MID" "$NAME")")
if [ "$CODE" = 200 ] || [ "$CODE" = 201 ]; then
  python3 - "$BODY" "$HOST_DIR" <<'PY'
import json, sys
body, host_dir = sys.argv[1], sys.argv[2]
d = json.load(open(body))
tok = d.get("machine_token")
if not tok:
    raise SystemExit("no machine_token in registration response")
open(host_dir + "/machine-token", "w").write(tok)
if d.get("jwks"):
    open(host_dir + "/relay-jwks.json", "w").write(json.dumps(d["jwks"]))
print("registered; token %d chars" % len(tok))
PY
else
  echo "register returned $CODE; keeping the existing token"
fi

# Point the host config at the relay.
python3 - "$HOST_DIR/config.json" "$RELAY" <<'PY'
import json, sys
path, url = sys.argv[1], sys.argv[2]
c = json.load(open(path))
c["relay_url"] = url
with open(path, "w") as f:
    json.dump(c, f, indent=2)
    f.write("\n")
PY

# Refresh the helper binary from the installed app.
mkdir -p "$HELPER_DIR"
cp "$APP/Contents/Helpers/cua-spacesd" "$HELPER"
chmod +x "$HELPER"

# Install a per-user LaunchAgent.
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>AssociatedBundleIdentifiers</key>
	<array><string>com.trycua.spaces.macos</string></array>
	<key>KeepAlive</key><true/>
	<key>Label</key><string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>$HELPER</string>
		<string>join</string>
		<string>--relay</string><string>$RELAY</string>
		<string>--relay-token-file</string><string>$HOST_DIR/machine-token</string>
		<string>--host-policy</string><string>$HOST_DIR/host.json</string>
		<string>--machine-id-file</string><string>$HOME/.cua/spacesd/id</string>
		<string>--relay-jwks</string><string>$HOST_DIR/relay-jwks.json</string>
		<string>--token-file</string><string>$HOST_DIR/env-token</string>
		<string>--data-dir</string><string>$HOME/.cua/spacesd</string>
	</array>
	<key>RunAtLoad</key><true/>
	<key>StandardErrorPath</key><string>$HOST_DIR/launchd.log</string>
	<key>StandardOutPath</key><string>$HOST_DIR/launchd.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true
echo "done: $LABEL -> $RELAY"
