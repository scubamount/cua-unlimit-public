# cua-unlimit-public

Reversible display patches for the Cua Spaces desktop client on macOS and Windows. The five bundled patches change how the client displays usage limits, upgrade prompts and the plan label. **They do not grant Pro or Teams, change billing, create seats, or override server-side limits.** The app itself is not included.

## Inspect before applying

Requires Python 3.10 or later. Node.js is optional for checking JavaScript syntax. From the repository root:

```sh
python3 bin/cua-unlimit patches
python3 bin/cua-unlimit status
```

On Windows use `py -3 bin/cua-unlimit status`. For a nonstandard install, put `--app PATH` before the subcommand. The JSON files in [`patches/cua-spaces`](patches/cua-spaces/) define the supported versions and expected matches.

## Apply and restore

Quit the app before changing its resources. Use only a copy you own or administer. Modifying a signed macOS app invalidates its resource seal. If macOS or your administrator blocks launch, restore the original files or reinstall the signed vendor build. Do not weaken launch protections.

```sh
python3 bin/cua-unlimit apply
python3 bin/cua-unlimit doctor
```

Reopen the app normally to see the changes. To restore all recorded changes, quit the app and run `python3 bin/cua-unlimit undo --all`. Keep the backup directory until restoration succeeds. A passing `doctor` check is not proof that the app can launch or that an account has a paid entitlement.

See the [command and recovery reference](docs/reference.md) for updates, backups, and diagnostic privacy. Run the fixture tests with `python3 -m unittest discover -s tests`. This independent tool is distributed under the [MIT license](LICENSE).
