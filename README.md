# cua-unlimit

Remove the Cua Spaces premium limits, then remove the client-side display of them.

Two steps:

1. **Remove the real restriction.** Run a self-hosted relay with plan enforcement off. The relay still meters usage but does not refuse a session past the Free plan limits. The hosted relay still enforces them.
2. **Remove the display.** Apply the five UI patches. They hide the cap, the upsell and the plan label so the client matches the unenforced state.

The patches alone do not change server-side enforcement. See [docs/relay.md](docs/relay.md) for the relay step.

## Relay

```sh
CUA_RELAY_PUBLIC_URL=https://relay.example.com bash scripts/relay-run.sh
CUA_RELAY_URL=https://relay.example.com bash scripts/relay-join.sh
```

`relay-run.sh` starts `cua-relay` with `CUA_RELAY_ENFORCE_PLANS` empty and `--device-enrollment off`. `relay-join.sh` registers this computer on that relay and points the host config at it. Repeat the join on every computer.

## Patches

Requires Python 3.10 or later. Quit Cua Spaces before changing its files.

```sh
python3 bin/cua-unlimit patches
python3 bin/cua-unlimit apply
python3 bin/cua-unlimit doctor
```

| Patch | Change |
|---|---|
| `0001-ui-hours-uncap` | Hide the 20-hour remote-hours cap |
| `0002-ui-sessions-uncap` | Hide the 2-computers-in-use cap |
| `0003-ui-plan-pro` | Show the account as Pro |
| `0004-ui-plan-teams` | Show the account as Teams |
| `0005-ui-allowance-banner` | Hide the Home allowance card |

Patches 0003 and 0004 change the displayed plan only. They do not grant Pro or Teams, change billing, create seats, or remove server-side enforcement. The relay still meters.

Reopen the app after `apply`. To restore every recorded change, quit the app and run:

```sh
python3 bin/cua-unlimit watch uninstall   # if the optional watcher is installed
python3 bin/cua-unlimit undo --all
```

A passing `doctor` checks files, not whether the app launches or the account has a paid entitlement. Diagnostic reports are local and not redacted.

## Layout

```
bin/cua-unlimit        CLI
lib/patchlib.py        patch engine
patches/cua-spaces/    UI patch catalog
scripts/relay-run.sh   self-hosted relay with enforcement off
scripts/relay-join.sh  register a computer on that relay
tests/                 engine tests
```

Run the tests with `python3 -m unittest discover -s tests`.

MIT. Cua Spaces itself is Cua AI, Inc.'s, distributed under FSL-1.1-MIT. This repository patches an installed copy; it does not redistribute the app.
