# Self-hosted relay

The hosted relay enforces the Free plan. A self-hosted relay removes the real restriction when it runs with plan enforcement off.

## Requirements

- A `cua-relay` binary, a `state.json`, and a `signing-key.pem` in `~/.cua-relay` (or `CUA_RELAY_DIR`).
- A public URL for the relay, reachable by every computer. Tailscale Serve, a reverse proxy, or a LAN address all work.
- A Cua account token for registration. `relay-join.sh` reads `~/.cua/credentials.json` or `CUA_ACCOUNT_TOKEN`.

## Run

```sh
CUA_RELAY_PUBLIC_URL=https://relay.example.com bash scripts/relay-run.sh
```

The script sets `CUA_RELAY_ENFORCE_PLANS=` (empty) and `--device-enrollment off`. The relay meters usage but does not refuse a session past the Free limits. Keep it running under launchd, systemd, or a process supervisor.

## Join each computer

```sh
CUA_RELAY_URL=https://relay.example.com bash scripts/relay-join.sh
```

The script registers the computer, writes `~/.cua/host/machine-token` and `~/.cua/host/relay-jwks.json`, sets `relay_url` in `~/.cua/host/config.json`, and installs the `com.cua-unlimit.host` LaunchAgent. Grant Screen Recording permission to the helper through System Settings if the host uses it.

Repeat on every computer. The relay state and signing key are the authority; back them up.

## Apply the UI patches

After every computer points at the self-hosted relay, apply the five patches so the client stops drawing the old limits:

```sh
python3 bin/cua-unlimit apply
```

The patches remove display only. The real removal is the relay running without plan enforcement.
