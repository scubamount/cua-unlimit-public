from __future__ import annotations

import json
import os
import plistlib
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import patchlib as P


ORIGINAL = "const store={set(e){this.usage=e?.metered?e:null;}};\n"
PATCHED = "const store={set(e){this.usage=e?.metered?{...e,hoursLimit:null,softLimitReached:!1,warnings:[]}:null;}};\n"
ORIGINAL2 = "/* v2 header */\n" + ORIGINAL


def build_asar(path: Path, package: dict) -> None:
    pkg = json.dumps(package).encode()
    header_json = json.dumps(
        {"files": {"package.json": {"size": len(pkg), "offset": "0"}}}
    ).encode()
    json_len = len(header_json)
    pad = (-json_len) % 4
    header_size = 8 + json_len + pad
    prefix = struct.pack("<IIII", 4, header_size, header_size - 4, json_len)
    blob = prefix + header_json + b"\0" * pad
    assert len(blob) == 8 + header_size
    path.write_bytes(blob + pkg)


class PatchlibTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app_path = self.root / "Fake.app"
        (self.app_path / "Contents" / "Resources" / "WebUI" / "assets").mkdir(parents=True)
        with (self.app_path / "Contents" / "Info.plist").open("wb") as fh:
            plistlib.dump(
                {
                    "CFBundleIdentifier": "test.fake",
                    "CFBundleName": "Fake",
                    "CFBundleExecutable": "Fake",
                    "CFBundleShortVersionString": "1.0",
                    "CFBundleVersion": "1.0.1",
                },
                fh,
            )
        self.asset = self.app_path / "Contents" / "Resources" / "WebUI" / "assets" / "app.js"
        self.asset.write_text(ORIGINAL)

        self.patch_root = self.root / "patches" / "fake"
        self.patch_root.mkdir(parents=True)
        self.state = self.root / "state"

        self.saved = (P.PATCH_ROOT, P.STATE_DIR, P.BACKUP_DIR, P.STATE_FILE, P.REPORT_DIR, P.LOG_DIR)
        P.PATCH_ROOT = self.root / "patches"
        P.STATE_DIR = self.state
        P.BACKUP_DIR = self.state / "backups"
        P.STATE_FILE = self.state / "state.json"
        P.REPORT_DIR = self.state / "reports"
        P.LOG_DIR = self.state / "logs"

    def tearDown(self):
        (P.PATCH_ROOT, P.STATE_DIR, P.BACKUP_DIR, P.STATE_FILE, P.REPORT_DIR, P.LOG_DIR) = self.saved
        self.tmp.cleanup()

    def write_patch(self, name, **over):
        patch = {
            "schema": 1,
            "id": f"fake/{name}",
            "app": "fake",
            "title": name,
            "app_versions": ["1.0"],
            "builds": ["1.0.1"],
            "try_on_unknown": False,
            "default": True,
            "depends_on": [],
            "targets": [
                {
                    "kind": "text",
                    "glob": "Contents/Resources/WebUI/assets/*.js",
                    "find": "set(e){this.usage=e?.metered?e:null;",
                    "replace": "set(e){this.usage=e?.metered?{...e,hoursLimit:null,softLimitReached:!1,warnings:[]}:null;",
                    "expect": 1,
                }
            ],
            "verify": [{"kind": "js_syntax", "glob": "Contents/Resources/WebUI/assets/*.js"}],
        }
        patch.update(over)
        (self.patch_root / f"{name}.json").write_text(json.dumps(patch))
        return patch

    def test_apply_and_undo(self):
        self.write_patch("0001")
        app = P.App(self.app_path)
        ops = P.apply_many(app, P.load_patches())
        self.assertEqual(len(ops), 1)
        self.assertEqual(self.asset.read_text(), PATCHED)
        self.assertEqual(P.patch_state(app, P.load_patches()[0]), "applied")
        restored = P.undo(app)
        self.assertEqual(len(restored), 1)
        self.assertEqual(self.asset.read_text(), ORIGINAL)
        self.assertEqual(P.load_state()["operations"], [])

    def test_second_apply_is_noop(self):
        self.write_patch("0001")
        app = P.App(self.app_path)
        P.apply_many(app, P.load_patches())
        ops = P.apply_many(app, P.load_patches())
        self.assertEqual(ops, [])
        self.assertEqual(self.asset.read_text(), PATCHED)

    def test_mixed_bundle_reapplies_to_new_asset(self):
        self.write_patch("0001")
        app = P.App(self.app_path)
        P.apply_many(app, P.load_patches())
        new_asset = self.asset.with_name("app2.js")
        new_asset.write_text(ORIGINAL)
        self.assertEqual(P.patch_state(app, P.load_patches()[0]), "stale")
        ops = P.apply_many(app, P.load_patches())
        self.assertEqual(len(ops), 1)
        self.assertEqual(self.asset.read_text(), PATCHED)
        self.assertEqual(new_asset.read_text(), PATCHED)
        self.assertEqual(P.patch_state(app, P.load_patches()[0]), "applied")

    def test_verify_failure_is_undoable(self):
        self.write_patch(
            "0001",
            verify=[{"kind": "text_present", "glob": "Contents/Resources/WebUI/assets/*.js", "text": "not-there"}],
        )
        app = P.App(self.app_path)
        with self.assertRaises(P.PatchError):
            P.apply_many(app, P.load_patches())
        self.assertEqual(self.asset.read_text(), PATCHED)
        restored = P.undo(app)
        self.assertEqual(len(restored), 1)
        self.assertEqual(self.asset.read_text(), ORIGINAL)

    def test_dry_run_does_not_write(self):
        self.write_patch("0001")
        app = P.App(self.app_path)
        ops = P.apply_many(app, P.load_patches(), dry_run=True)
        self.assertEqual(len(ops), 1)
        self.assertEqual(self.asset.read_text(), ORIGINAL)
        self.assertFalse(P.BACKUP_DIR.exists())
        self.assertEqual(P.load_state()["operations"], [])

    def test_stale_state_reapplies_after_update(self):
        self.write_patch("0001")
        app = P.App(self.app_path)
        P.apply_many(app, P.load_patches())
        self.asset.write_text(ORIGINAL2)  # simulate Cua replacing the asset
        self.assertEqual(P.patch_state(app, P.load_patches()[0]), "stale")
        ops = P.apply_many(app, P.load_patches())
        self.assertEqual(len(ops), 1)
        self.assertIn(PATCHED, self.asset.read_text())
        restored = P.undo(app)
        self.assertEqual(len(restored), 1)
        self.assertEqual(self.asset.read_text(), ORIGINAL2)
        self.assertEqual(P.load_state()["operations"], [])

    def test_undo_refuses_changed_file(self):
        self.write_patch("0001")
        app = P.App(self.app_path)
        P.apply_many(app, P.load_patches())
        self.asset.write_text(ORIGINAL2)
        with self.assertRaises(P.PatchError):
            P.undo(app)
        self.assertEqual(self.asset.read_text(), ORIGINAL2)
        self.assertEqual(len(P.load_state()["operations"]), 1)

    def test_mismatch_raises_and_collects(self):
        self.write_patch("0001", targets=[{"kind": "text", "glob": "Contents/Resources/WebUI/assets/*.js", "find": "missing-marker", "replace": "x", "expect": 1}])
        app = P.App(self.app_path)
        with self.assertRaises(P.PatchError):
            P.apply_many(app, P.load_patches())
        report_path = P.collect(app)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["patches"][0]["targets"][0]["find_count"], 0)
        self.assertTrue(report["asset_inventory"][0]["counts"]["this.usage"])

    def test_try_on_unknown_version(self):
        self.write_patch("0001", app_versions=["9.9"], builds=["9.9"], try_on_unknown=True)
        app = P.App(self.app_path)
        ops = P.apply_many(app, P.load_patches())
        self.assertEqual(len(ops), 1)
        self.assertEqual(self.asset.read_text(), PATCHED)

    def test_auto_unknown_version_fails_closed(self):
        self.write_patch("0001", app_versions=["9.9"], builds=["9.9"], try_on_unknown=False)
        app = P.App(self.app_path)
        with redirect_stderr(StringIO()):
            rc = P.auto(app, quiet=True)
        self.assertEqual(rc, 1)
        reports = sorted(P.REPORT_DIR.glob("*.json"))
        self.assertEqual(len(reports), 1)

    def test_dependency_pulls_parent(self):
        self.write_patch("0001")
        self.write_patch(
            "0002",
            depends_on=["fake/0001"],
            default=False,
            targets=[
                {
                    "kind": "text",
                    "glob": "Contents/Resources/WebUI/assets/*.js",
                    "find": "set(e){this.usage=e?.metered?{...e,hoursLimit:null,softLimitReached:!1,warnings:[]}:null;",
                    "replace": "set(e){this.usage=e?.metered?{...e,hoursLimit:null,liveSessionsLimit:null,softLimitReached:!1,warnings:[]}:null;",
                    "expect": 1,
                }
            ],
        )
        app = P.App(self.app_path)
        patches = P.load_patches()
        selected = [P.patch_by_id(patches, "fake/0002")]
        ops = P.apply_many(app, selected)
        self.assertEqual([o["patch"] for o in ops], ["fake/0001", "fake/0002"])
        self.assertIn("liveSessionsLimit:null", self.asset.read_text())

    def test_parent_state_survives_dependent_patch(self):
        marker = "this.usage=e?.metered?{...e,hoursLimit:null,"
        self.write_patch("0001", applied_if=[marker])
        self.write_patch(
            "0002",
            depends_on=["fake/0001"],
            default=True,
            targets=[
                {
                    "kind": "text",
                    "glob": "Contents/Resources/WebUI/assets/*.js",
                    "find": "set(e){this.usage=e?.metered?{...e,hoursLimit:null,softLimitReached:!1,warnings:[]}:null;",
                    "replace": "set(e){this.usage=e?.metered?{...e,hoursLimit:null,softLimitReached:!1,warnings:[],liveSessionsLimit:null}:null;",
                    "expect": 1,
                }
            ],
        )
        app = P.App(self.app_path)
        P.apply_many(app, P.load_patches())
        self.assertIn("liveSessionsLimit:null", self.asset.read_text())
        states = {row["id"]: row["state"] for row in P.status(app)["patches"]}
        self.assertEqual(states["fake/0001"], "applied")
        self.assertEqual(states["fake/0002"], "applied")

    def test_four_patch_chain_keeps_parent_markers(self):
        self.write_patch("0001", applied_if=["this.usage=e?.metered?{...e,hoursLimit:null,"])
        self.write_patch(
            "0002",
            depends_on=["fake/0001"],
            applied_if=["this.usage=e?.metered?{...e,hoursLimit:null,softLimitReached:!1,warnings:[],liveSessionsLimit:null"],
            targets=[
                {
                    "kind": "text",
                    "glob": "Contents/Resources/WebUI/assets/*.js",
                    "find": "set(e){this.usage=e?.metered?{...e,hoursLimit:null,softLimitReached:!1,warnings:[]}:null;",
                    "replace": "set(e){this.usage=e?.metered?{...e,hoursLimit:null,softLimitReached:!1,warnings:[],liveSessionsLimit:null}:null;",
                    "expect": 1,
                }
            ],
        )
        self.write_patch(
            "0003",
            depends_on=["fake/0002"],
            applied_if=["plan:\"pro\"", "plan:\"teams\""],
            targets=[
                {
                    "kind": "text",
                    "glob": "Contents/Resources/WebUI/assets/*.js",
                    "find": "set(e){this.usage=e?.metered?{...e,hoursLimit:null,softLimitReached:!1,warnings:[],liveSessionsLimit:null}:null;",
                    "replace": "set(e){this.usage=e?.metered?{...e,hoursLimit:null,softLimitReached:!1,warnings:[],liveSessionsLimit:null,plan:\"pro\"}:null;",
                    "expect": 1,
                }
            ],
        )
        self.write_patch(
            "0004",
            depends_on=["fake/0003"],
            targets=[
                {
                    "kind": "text",
                    "glob": "Contents/Resources/WebUI/assets/*.js",
                    "find": "set(e){this.usage=e?.metered?{...e,hoursLimit:null,softLimitReached:!1,warnings:[],liveSessionsLimit:null,plan:\"pro\"}:null;",
                    "replace": "set(e){this.usage=e?.metered?{...e,hoursLimit:null,softLimitReached:!1,warnings:[],liveSessionsLimit:null,plan:\"teams\"}:null;",
                    "expect": 1,
                }
            ],
        )
        app = P.App(self.app_path)
        P.apply_many(app, P.load_patches())
        text = self.asset.read_text()
        self.assertIn('plan:"teams"', text)
        states = {row["id"]: row["state"] for row in P.status(app)["patches"]}
        self.assertEqual(states, {"fake/0001": "applied", "fake/0002": "applied", "fake/0003": "applied", "fake/0004": "applied"})
        self.assertTrue(P.doctor(app)["ok"])

    def test_doctor_accepts_parent_marker_after_dependent(self):
        marker = "this.usage=e?.metered?{...e,hoursLimit:null,"
        self.write_patch("0001", applied_if=[marker])
        self.write_patch(
            "0002",
            depends_on=["fake/0001"],
            targets=[
                {
                    "kind": "text",
                    "glob": "Contents/Resources/WebUI/assets/*.js",
                    "find": "set(e){this.usage=e?.metered?{...e,hoursLimit:null,softLimitReached:!1,warnings:[]}:null;",
                    "replace": "set(e){this.usage=e?.metered?{...e,hoursLimit:null,softLimitReached:!1,warnings:[],liveSessionsLimit:null}:null;",
                    "expect": 1,
                }
            ],
        )
        app = P.App(self.app_path)
        P.apply_many(app, P.load_patches())
        report = P.doctor(app)
        file_checks = [c for c in report["checks"] if c["name"].startswith("file ")]
        self.assertTrue(file_checks)
        self.assertTrue(all(c["ok"] for c in file_checks))
        self.assertTrue(any("dependent patch" in c["detail"] for c in file_checks))

    def test_status_version_match(self):
        self.write_patch("0001")
        app = P.App(self.app_path)
        st = P.status(app)
        self.assertEqual(st["version"], "1.0")
        self.assertEqual(st["patches"][0]["state"], "available")
        self.assertTrue(st["patches"][0]["version_match"])

    def test_asar_roundtrip(self):
        asar = self.root / "test.asar"
        package = {"version": "1.2.3", "name": "x"}
        build_asar(asar, package)
        self.assertEqual(P.read_asar_package_json(asar), package)

    def test_electron_layout_and_asar_version(self):
        app_path = self.root / "Cua Spaces"
        (app_path / "resources" / "web" / "assets").mkdir(parents=True)
        (app_path / "Cua Spaces.exe").write_bytes(b"MZ")
        asset = app_path / "resources" / "web" / "assets" / "app.js"
        asset.write_text(ORIGINAL)
        build_asar(app_path / "resources" / "app.asar", {"version": "0.33.2", "name": "cua-spaces"})
        self.write_patch(
            "0003",
            app_versions=["0.33.*"],
            builds=[],
            targets=[
                {
                    "kind": "text",
                    "glob": "**/assets/*.js",
                    "find": "set(e){this.usage=e?.metered?e:null;",
                    "replace": "set(e){this.usage=e?.metered?{...e,hoursLimit:null,softLimitReached:!1,warnings:[]}:null;",
                    "expect": 1,
                }
            ],
            verify=[{"kind": "js_syntax", "glob": "**/assets/*.js"}],
        )
        app = P.App(app_path)
        self.assertEqual(app.version, "0.33.2")
        self.assertEqual(app.layout, "electron")
        self.assertEqual(
            [p.relative_to(app.path).as_posix() for p in app.web_asset_dirs()],
            ["resources/web/assets"],
        )
        ops = P.apply_many(app, P.load_patches())
        self.assertEqual(len(ops), 1)
        self.assertEqual(asset.read_text(), PATCHED)
        report = json.loads(P.collect(app).read_text())
        self.assertEqual(report["app"]["asar_package"]["version"], "0.33.2")
        self.assertEqual(report["app"]["layout"], "electron")
        self.assertIn(
            "resources/web/assets/app.js",
            [a["path"] for a in report["asset_inventory"]],
        )
        P.undo(app)
        self.assertEqual(asset.read_text(), ORIGINAL)

    def test_watch_windows_task(self):
        app = P.App(self.app_path)
        old_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = str(self.root / "AppData" / "Roaming")
        try:
            path = P.watch_install_windows(app, start=False)
        finally:
            if old_appdata is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old_appdata
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-16")
        self.assertIn("<LogonTrigger>", text)
        self.assertIn("<StateChange>SessionUnlock</StateChange>", text)
        self.assertIn("--app", text)
        self.assertIn("auto --quiet", text)
        self.assertIn(str(P.REPO_ROOT / "bin" / "cua-unlimit"), text)
        self.assertEqual(P.WATCH_TASK_NAME, "cua-unlimit-watch")
        query = P.watch_process_query()
        self.assertIn(str(P.watch_cmd_path()).lower(), query)

if __name__ == "__main__":
    unittest.main()
