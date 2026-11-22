# cua-unlimit-public

Reversible display patches for the Cua Spaces desktop client on macOS and Windows. The defaults hide usage-limit notices and upgrade prompts and make Settings display **Teams**, even for a Free account. They do not grant Pro or Teams, change billing, create seats, or override server-side limits. The app itself is not included.

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

Reopen the app normally to see the changes. The optional watcher can reapply patches after you restore the files. If you installed it, run `python3 bin/cua-unlimit watch uninstall` first. Then quit the app and run `python3 bin/cua-unlimit undo --all`. Keep the backup directory until restoration succeeds.

`doctor` checks files, not whether the app launches or the account has a paid entitlement. Diagnostic reports are local and not redacted. Do not post them or backups in public issues.

See the [command and recovery reference](docs/reference.md) for updates, backups, and diagnostic privacy. Run the fixture tests with `python3 -m unittest discover -s tests`. This independent tool is distributed under the [MIT license](LICENSE).
