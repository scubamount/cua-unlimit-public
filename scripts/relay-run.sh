#!/bin/bash
# Run the Cua relay with plan enforcement off.
# Usage: CUA_RELAY_PUBLIC_URL=https://relay.example.com bash scripts/relay-run.sh
set -euo pipefail

DIR="${CUA_RELAY_DIR:-$HOME/.cua-relay}"
PUBLIC_URL="${CUA_RELAY_PUBLIC_URL:?set CUA_RELAY_PUBLIC_URL}"
LISTEN="${CUA_RELAY_LISTEN:-0.0.0.0:18081}"

[ -x "$DIR/cua-relay" ] || { echo "missing $DIR/cua-relay" >&2; exit 1; }
[ -f "$DIR/state.json" ] || { echo "missing $DIR/state.json" >&2; exit 1; }
[ -f "$DIR/signing-key.pem" ] || { echo "missing $DIR/signing-key.pem" >&2; exit 1; }

exec env CUA_RELAY_ENFORCE_PLANS= "$DIR/cua-relay" \
  --listen "$LISTEN" \
  --oidc-issuer https://auth.cua.ai/realms/cyclops-cs \
  --oidc-jwks-url https://auth.cua.ai/realms/cyclops-cs/protocol/openid-connect/certs \
  --state-file "$DIR/state.json" \
  --signing-key-file "$DIR/signing-key.pem" \
  --public-url "$PUBLIC_URL" \
  --device-enrollment off
