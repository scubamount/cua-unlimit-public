# Command and recovery reference

Use `python3 bin/cua-unlimit --help` for the command list. On Windows, replace `python3` with `py -3`.

| Command | What it does |
|---|---|
| `version`, `patches`, `status` | Identify the installation, list the bundled patches, or compare files with the patch catalog. |
| `apply` | Apply the defaults for a matching version in dependency order. Back up changed files with their original SHA-256 hashes. |
| `undo`, `undo --all` | Restore the last operation or unwind all recorded operations. Refuse restoration if a file no longer matches the recorded version. |
| `auto` | Recheck the installation after an update. A version or marker mismatch can produce a local diagnostic report. |
| `doctor` | Check patch state, hashes and backups. Warnings can coexist with an overall `ok`. |
| `collect` | Write a local diagnostic report and archive. This does not upload them. |
| `watch install`, `watch status`, `watch uninstall` | Manage the optional per-user update watcher. It is not installed by default. |

## Catalog

Patches 0001 and 0002 change the display of hours and concurrent computers. Patches 0003 and 0004 change the displayed plan, not the account. Patch 0005 hides the Home allowance card separately. The JSON definitions specify the exact versions, dependencies and expected occurrences. A version match alone does not prove the UI works.

## Local data and recovery

The tool uses `.cua-unlimit` in the current user's home directory unless `CUA_UNLIMIT_HOME` is set. Use a separate state directory for each installation if you manage more than one. Backups contain original application assets. Reports can include local paths, package metadata, usage details, and excerpts from application files. Keep them private; do not attach a raw report or backup to a public issue.

If an update changed a file, do not force an old backup over it. If a patch mismatches, stop and check the app version. For a blocked launch or missing backup, restore a clean, signed copy from the vendor. If the watcher is enabled, uninstall it before reinstalling so it does not reapply changes. Use the operating system's normal launch path. Report file checks, launch state and visible behavior separately.
