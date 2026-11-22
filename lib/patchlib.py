"""cua-unlimit patch engine.

A small, dependency-free engine for applying text and byte patches to a
macOS .app bundle or a Windows Electron install, with per-operation backups,
verification, rollback, and post-update report collection.

Patches are JSON files under
patches/<app>/<id>.json; see docs/reference.md.
"""

from __future__ import annotations

import csv
import fnmatch
import hashlib
import io
import json
import os
import platform
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import winreg
except ImportError:  # not on Windows
    winreg = None

SCHEMA = 1
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
DEFAULT_APP = Path("/Applications/Cua Spaces.app")
REPO_ROOT = Path(__file__).resolve().parents[1]
PATCH_ROOT = REPO_ROOT / "patches"
STATE_DIR = Path(os.environ.get("CUA_UNLIMIT_HOME", Path.home() / ".cua-unlimit"))
BACKUP_DIR = STATE_DIR / "backups"
REPORT_DIR = STATE_DIR / "reports"
LOG_DIR = STATE_DIR / "logs"
STATE_FILE = STATE_DIR / "state.json"
WATCH_LABEL = "com.scubamount.cua-unlimit.watch"
WATCH_TASK = "cua-unlimit-watch"


class PatchError(RuntimeError):
    pass


# utilities


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]} timed out after {timeout}s"


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cua-unlimit.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise PatchError(f"cannot write {path}; close Cua Spaces and retry")
                time.sleep(0.5)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data) -> None:
    atomic_write(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode())


def context_around(text: str, needle: str, radius: int = 300) -> list[str]:
    out = []
    start = 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            break
        out.append(text[max(0, i - radius): i + len(needle) + radius])
        start = i + len(needle)
        if len(out) >= 5:
            break
    return out


# app model


def read_asar_package_json(asar: Path) -> dict | None:
    """Read package.json from an Electron asar without unpacking it."""
    if not asar.exists():
        return None
    try:
        with asar.open("rb") as fh:
            prefix = fh.read(16)
            if len(prefix) < 16:
                return None
            _, header_size, _json_size, json_len = struct.unpack("<IIII", prefix)
            header = json.loads(fh.read(json_len).decode("utf-8"))
            entry = header.get("files", {}).get("package.json")
            if not entry or "offset" not in entry or "size" not in entry:
                return None
            fh.seek(8 + header_size + int(entry["offset"]))
            data = fh.read(int(entry["size"]))
            return json.loads(data.decode("utf-8"))
    except (OSError, ValueError, struct.error, json.JSONDecodeError):
        return None


def split_version(value: str) -> tuple[str, str]:
    parts = value.split(".")
    if len(parts) >= 3:
        return ".".join(parts[:3]), value
    return value, value


def windows_file_version(exe: Path | None) -> str | None:
    if not exe or not exe.exists() or not IS_WINDOWS:
        return None
    quoted = str(exe).replace("'", "''")
    ps = f"(Get-Item -LiteralPath '{quoted}').VersionInfo.FileVersion"
    rc, out, _ = run(["powershell", "-NoProfile", "-Command", ps], timeout=20)
    return out.strip() if rc == 0 and out.strip() else None


def windows_registry_app() -> Path | None:
    if winreg is None:
        return None
    roots = [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]
    for root in roots:
        for view in (winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0), winreg.KEY_READ):
            try:
                key = winreg.OpenKey(
                    root, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", 0, view
                )
            except OSError:
                continue
            try:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        sub = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, sub) as sk:
                            name = str(winreg.QueryValueEx(sk, "DisplayName")[0])
                            if "cua spaces" not in name.lower():
                                continue
                            for field in ("DisplayIcon", "InstallLocation"):
                                try:
                                    value = str(winreg.QueryValueEx(sk, field)[0])
                                except OSError:
                                    continue
                                candidate = value.split(",")[0].strip().strip('"')
                                p = Path(candidate)
                                if field == "DisplayIcon" and p.suffix.lower() == ".exe":
                                    p = p.parent
                                if p.exists() and is_app_dir(p):
                                    return p
                    except OSError:
                        continue
            finally:
                key.Close()
    return None


def app_candidates() -> list[Path]:
    if IS_WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        pf = Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
        pf86 = Path(os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)")
        return [
            local / "Programs" / "cua-spaces",
            local / "Programs" / "Cua Spaces",
            local / "Cua Spaces",
            pf / "Cua Spaces",
            pf / "cua-spaces",
            pf86 / "Cua Spaces",
        ]
    if IS_MACOS:
        return [DEFAULT_APP, Path.home() / "Applications" / "Cua Spaces.app"]
    return [
        Path.home() / ".local" / "share" / "cua-spaces",
        Path("/opt/Cua Spaces"),
        Path("/usr/lib/cua-spaces"),
    ]


def is_app_dir(path: Path) -> bool:
    return any(
        (path / rel).exists()
        for rel in (
            "Contents/Info.plist",
            "Contents/Resources/app.asar",
            "Contents/Resources/web",
            "Contents/Resources/WebUI",
            "resources/app.asar",
            "resources/web",
            "Cua Spaces.exe",
            "cua-spaces.exe",
        )
    )


def find_app(explicit: str | Path | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise SystemExit(f"app not found: {path}")
        return path
    env = os.environ.get("CUA_UNLIMIT_APP")
    if env:
        return find_app(env)
    searched = []
    for candidate in app_candidates():
        searched.append(str(candidate))
        if candidate.exists() and is_app_dir(candidate):
            return candidate
    registry = windows_registry_app()
    if registry:
        return registry
    raise SystemExit("Cua Spaces not found; pass --app. Searched: " + ", ".join(searched))


class App:
    def __init__(self, path: Path | str | None = None):
        self.path = find_app(path)
        self.info: dict = {}
        info_path = self.path / "Contents" / "Info.plist"
        if info_path.exists():
            with info_path.open("rb") as fh:
                self.info = plistlib.load(fh)
        self.bundle_id = str(
            self.info.get("CFBundleIdentifier")
            or ("com.trycua.spaces.macos" if IS_MACOS else "ai.cua.spaces.desktop")
        )
        self.name = str(self.info.get("CFBundleName") or "Cua Spaces")
        self.executable = self._detect_executable()
        self.version, self.build = self._detect_version()
        self.layout = "electron" if self._is_electron() else "swift"

    def _is_electron(self) -> bool:
        return any(
            (self.path / rel).exists()
            for rel in ("resources/app.asar", "Contents/Resources/app.asar")
        )

    def _detect_executable(self) -> Path | None:
        if IS_MACOS:
            name = self.info.get("CFBundleExecutable")
            if name:
                candidate = self.path / "Contents" / "MacOS" / str(name)
                if candidate.exists():
                    return candidate
        for name in ("Cua Spaces.exe", "cua-spaces.exe", "Cua Spaces", "cua-spaces"):
            candidate = self.path / name
            if candidate.exists():
                return candidate
        if IS_WINDOWS:
            for candidate in sorted(self.path.glob("*.exe")):
                if not candidate.name.lower().startswith("uninstall"):
                    return candidate
        return None

    def _detect_version(self) -> tuple[str, str]:
        short = self.info.get("CFBundleShortVersionString")
        if short:
            return str(short), str(self.info.get("CFBundleVersion", "unknown"))
        for rel in ("resources/app.asar", "Contents/Resources/app.asar"):
            package = read_asar_package_json(self.path / rel)
            if package and package.get("version"):
                version = str(package["version"])
                file_version = windows_file_version(self.executable)
                return version, (file_version or version)
        file_version = windows_file_version(self.executable)
        if file_version:
            return split_version(file_version)
        return "unknown", "unknown"

    def file(self, rel: str) -> Path:
        return self.path / rel

    def resolve(self, pattern: str) -> list[Path]:
        """Resolve a glob relative to the app root."""
        return sorted(p for p in self.path.glob(pattern) if p.is_file())

    def web_asset_dirs(self) -> list[Path]:
        dirs = []
        for rel in (
            "Contents/Resources/web/assets",
            "resources/web/assets",
            "Contents/Resources/WebUI/assets",
            "web/assets",
        ):
            candidate = self.path / rel
            if candidate.is_dir():
                dirs.append(candidate)
        return dirs

    def cua_cli(self) -> Path | None:
        for rel in (
            "Contents/MacOS/cua",
            "Contents/Resources/native/cua",
            "resources/native/cua.exe",
            "resources/native/cua",
        ):
            candidate = self.path / rel
            if candidate.exists():
                return candidate
        found = shutil.which("cua")
        return Path(found) if found else None

    def running(self) -> list[int]:
        if not self.executable:
            return []
        if IS_WINDOWS:
            rc, out, _ = run(
                ["tasklist", "/FI", f"IMAGENAME eq {self.executable.name}", "/FO", "CSV", "/NH"]
            )
            if rc != 0 or not out:
                return []
            pids = []
            for row in csv.reader(out.splitlines()):
                if len(row) >= 2 and row[0].lower() == self.executable.name.lower():
                    try:
                        pids.append(int(row[1]))
                    except ValueError:
                        continue
            return pids
        rc, out, _ = run(["pgrep", "-f", str(self.executable)])
        if rc != 0 or not out:
            return []
        return [int(x) for x in out.split() if x.isdigit()]

    def signature(self) -> dict:
        if IS_MACOS:
            rc, out, err = run(["codesign", "--verify", "--strict", str(self.path)], timeout=60)
            detail = "valid" if rc == 0 else (err or out)[:300] + " (expected after a resource patch)"
            return {"ok": rc == 0, "kind": "codesign", "detail": detail}
        if IS_WINDOWS:
            if not self.executable:
                return {"ok": False, "kind": "authenticode", "detail": "no executable"}
            quoted = str(self.executable).replace("'", "''")
            ps = (
                f"$s=Get-AuthenticodeSignature -LiteralPath '{quoted}'; "
                "$s.Status.ToString() + '|' + $s.SignerCertificate.Subject"
            )
            rc, out, err = run(["powershell", "-NoProfile", "-Command", ps], timeout=30)
            if rc != 0:
                return {"ok": False, "kind": "authenticode", "detail": (err or out)[:300]}
            status, _, subject = out.partition("|")
            return {
                "ok": status.strip() == "Valid",
                "kind": "authenticode",
                "detail": (status.strip() + " " + subject.strip()).strip(),
            }
        return {"ok": True, "kind": "none", "detail": "not checked"}

# patches


def load_patches(app_name: str | None = None) -> list[dict]:
    patches = []
    if not PATCH_ROOT.exists():
        return patches
    for path in sorted(PATCH_ROOT.rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise SystemExit(f"bad patch JSON {path}: {exc}")
        if data.get("schema") != SCHEMA:
            raise SystemExit(f"unsupported patch schema in {path}: {data.get('schema')!r}")
        try:
            data["_file"] = str(path.relative_to(REPO_ROOT))
        except ValueError:
            data["_file"] = str(path)
        data.setdefault("app", path.parent.name)
        data.setdefault("default", True)
        data.setdefault("depends_on", [])
        data.setdefault("try_on_unknown", False)
        data.setdefault("targets", [])
        data.setdefault("verify", [])
        if app_name and data["app"] != app_name:
            continue
        patches.append(data)
    return patches


def patch_matches_version(patch: dict, app: App) -> bool:
    versions = [str(v) for v in patch.get("app_versions", [])]
    builds = [str(v) for v in patch.get("builds", [])]
    candidates = [app.version, app.build]
    for candidate in candidates:
        for pattern in versions + builds:
            if fnmatch.fnmatch(candidate, pattern):
                return True
    return False


def patch_applies(patch: dict, app: App) -> bool:
    if patch_matches_version(patch, app):
        return True
    return bool(patch.get("try_on_unknown"))


def patch_by_id(patches: list[dict], patch_id: str) -> dict:
    for p in patches:
        if p.get("id") == patch_id or p.get("id", "").endswith("/" + patch_id):
            return p
    raise SystemExit(f"unknown patch id: {patch_id}")


def target_files(app: App, target: dict) -> list[Path]:
    pattern = target.get("glob")
    if not pattern:
        raise SystemExit("target has no glob")
    return app.resolve(pattern)


def count_in_file(path: Path, needle: str) -> int:
    return path.read_bytes().count(needle.encode())


def applied_marker_present(app: App, patch: dict) -> bool:
    markers = patch.get("applied_if", [])
    if not markers:
        return False
    needles = [m.encode() for m in markers]
    for target in patch.get("targets", []):
        for path in target_files(app, target):
            data = path.read_bytes()
            if any(n in data for n in needles):
                return True
    return False


def patch_state(app: App, patch: dict) -> str:
    """applied | stale | partial | available | mismatch"""
    total_find = 0
    total_repl = 0
    files = 0
    for target in patch.get("targets", []):
        if target.get("kind", "text") == "bytes":
            find = bytes.fromhex(target["find"])
            repl = bytes.fromhex(target["replace"])
        else:
            find = target["find"].encode()
            repl = target["replace"].encode()
        for path in target_files(app, target):
            data = path.read_bytes()
            files += 1
            total_find += data.count(find)
            total_repl += data.count(repl)
    if not files:
        return "mismatch"
    expect = sum(int(t.get("expect", 1)) for t in patch.get("targets", []))
    state = load_state()
    ops = [
        op
        for op in state.get("operations", [])
        if op.get("patch") == patch["id"] and not op.get("noop")
    ]
    marker = applied_marker_present(app, patch)
    if ops:
        last = ops[-1]
        current = {
            f["path"]: sha256_file(Path(f["path"])) if Path(f["path"]).exists() else None
            for f in last.get("files", [])
        }
        if total_find and total_repl:
            return "stale"  # a second bundle copy still has the original marker
        if all(current.get(f["path"]) == f["after"] for f in last.get("files", [])):
            return "applied"
        if marker:
            return "applied"  # a later patch extended this target
        if total_find == 0 and total_repl >= expect:
            return "applied"  # patched but edited, or a later patch changed the hash
        return "stale"
    if total_find and total_repl:
        return "stale"  # mixed bundle: one copy patched, another not
    if marker:
        return "applied"
    if total_find == 0 and total_repl >= expect:
        return "applied"
    if total_repl:
        return "partial"
    if patch_applies(patch, app):
        return "available"
    return "mismatch"


def load_state() -> dict:
    return read_json(STATE_FILE, {"schema": SCHEMA, "operations": []}) or {"schema": SCHEMA, "operations": []}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    write_json(STATE_FILE, state)


# apply


def apply_patch(app: App, patch: dict, dry_run: bool = False) -> dict:
    """Apply one patch. Returns an operation record."""
    op = {
        "patch": patch["id"],
        "applied_at": now(),
        "app_version": app.version,
        "build": app.build,
        "files": [],
    }
    for target in patch.get("targets", []):
        kind = target.get("kind", "text")
        expect = int(target.get("expect", 1))
        if kind == "bytes":
            find = bytes.fromhex(target["find"])
            repl = bytes.fromhex(target["replace"])
        else:
            find = target["find"].encode()
            repl = target["replace"].encode()
        paths = target_files(app, target)
        if not paths:
            raise PatchError(f"{patch['id']}: no files match {target['glob']}")
        blobs = {p: p.read_bytes() for p in paths}
        total_find = sum(d.count(find) for d in blobs.values())
        total_repl = sum(d.count(repl) for d in blobs.values())
        if total_find == 0 and total_repl >= expect:
            continue  # already applied in this target
        if total_find != expect:
            counts = ", ".join(
                f"{p.name}:{d.count(find)}" for p, d in blobs.items() if d.count(find)
            ) or "none"
            raise PatchError(
                f"{patch['id']}: expected {expect} occurrence(s) of the marker under "
                f"{target['glob']}, found {total_find} ({counts})"
            )
        for path, data in blobs.items():
            n_find = data.count(find)
            if not n_find:
                continue
            rel = str(path.relative_to(app.path))
            before = sha256_bytes(data)
            new_data = data.replace(find, repl, n_find)
            backup = BACKUP_DIR / app.version / patch["id"].replace("/", "_") / before[:12] / rel
            if not dry_run:
                if not backup.exists():
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(backup, data)
                atomic_write(path, new_data)
            op["files"].append(
                {
                    "path": str(path),
                    "relative": rel,
                    "backup": str(backup),
                    "before": before,
                    "after": sha256_bytes(new_data),
                    "dry_run": dry_run,
                }
            )
    if not op["files"]:
        op["noop"] = True
        return op
    if not dry_run:
        state = load_state()
        state["schema"] = SCHEMA
        state["app"] = str(app.path)
        state["app_version"] = app.version
        state["build"] = app.build
        state["operations"] = [
            op for op in state.get("operations", []) if op.get("patch") != patch["id"]
        ]
        state.setdefault("operations", []).append(op)
        save_state(state)
        verify_patch(app, patch, [Path(f["path"]) for f in op["files"]])
    return op


def verify_patch(app: App, patch: dict, changed_paths: list[Path] | None = None) -> None:
    for check in patch.get("verify", []):
        kind = check.get("kind")
        paths = app.resolve(check["glob"]) if check.get("glob") else []
        if kind == "text_present":
            if not any(check["text"].encode() in p.read_bytes() for p in paths):
                raise PatchError(f"{patch['id']}: verify failed, text not present: {check['text'][:60]}")
        elif kind == "text_absent":
            if any(check["text"].encode() in p.read_bytes() for p in paths):
                raise PatchError(f"{patch['id']}: verify failed, text still present: {check['text'][:60]}")
        elif kind == "js_syntax":
            for p in (changed_paths or paths):
                if p.exists():
                    check_js_syntax(p)
        elif kind == "sha256":
            want = check["sha256"]
            if not any(sha256_file(p) == want for p in paths):
                raise PatchError(f"{patch['id']}: verify failed, no file has sha256 {want}")


def check_js_syntax(path: Path) -> None:
    node = shutil.which("node")
    if not node:
        return
    with tempfile.NamedTemporaryFile("wb", suffix=".mjs", delete=False) as fh:
        fh.write(path.read_bytes())
        tmp = fh.name
    try:
        rc, out, err = run([node, "--check", tmp], timeout=60)
        if rc != 0:
            raise PatchError(f"{path}: JS syntax check failed: {(err or out)[:300]}")
    finally:
        os.unlink(tmp)


def with_dependencies(patches: list[dict]) -> list[dict]:
    """Include a selected patch's dependencies, so applying a child alone works."""
    known = {p["id"]: p for p in load_patches()}
    selected = {p["id"]: p for p in patches}
    seen: set[str] = set()

    def visit(patch: dict) -> None:
        if patch["id"] in seen:
            return
        seen.add(patch["id"])
        for dep in patch.get("depends_on", []):
            if dep not in selected:
                if dep not in known:
                    raise SystemExit(f"{patch['id']} depends on missing patch {dep}")
                selected[dep] = known[dep]
            visit(selected[dep])

    for patch in list(selected.values()):
        visit(patch)
    return list(selected.values())


def apply_many(app: App, patches: list[dict], dry_run: bool = False) -> list[dict]:
    """Apply patches in dependency order, skipping ones already applied."""
    ordered = order_patches(with_dependencies(patches))
    ops = []
    for patch in ordered:
        state = patch_state(app, patch)
        if state == "applied":
            continue
        if state == "partial":
            raise PatchError(f"{patch['id']}: partially applied; run undo first")
        if not patch_applies(patch, app):
            continue
        ops.append(apply_patch(app, patch, dry_run=dry_run))
    return ops


def order_patches(patches: list[dict]) -> list[dict]:
    by_id = {p["id"]: p for p in patches}
    ordered: list[dict] = []
    seen: set[str] = set()

    def visit(patch: dict) -> None:
        if patch["id"] in seen:
            return
        for dep in patch.get("depends_on", []):
            if dep not in by_id:
                raise SystemExit(f"{patch['id']} depends on missing patch {dep}")
            visit(by_id[dep])
        seen.add(patch["id"])
        ordered.append(patch)

    for patch in patches:
        visit(patch)
    return ordered


# undo


def undo(app: App, all_ops: bool = False) -> list[dict]:
    state = load_state()
    ops = state.get("operations", [])
    if not ops:
        return []
    todo = list(reversed(ops)) if all_ops else [ops[-1]]
    restored = []
    for op in todo:
        for f in reversed(op.get("files", [])):
            if f.get("dry_run"):
                continue
            path = Path(f["path"])
            backup = Path(f["backup"])
            if not backup.exists():
                raise PatchError(f"backup missing: {backup}")
            data = backup.read_bytes()
            if sha256_bytes(data) != f["before"]:
                raise PatchError(f"backup hash mismatch: {backup}")
            current = sha256_file(path) if path.exists() else None
            if current == f["before"]:
                pass  # already original; drop the operation without writing
            elif current == f["after"]:
                atomic_write(path, data)
                restored.append({"path": str(path), "sha256": f["before"]})
            else:
                raise PatchError(
                    f"{path} changed since apply; refusing to restore. "
                    "Run collect or auto to re-apply against the current file."
                )
        state["operations"].remove(op)
    save_state(state)
    return restored


# status


def status(app: App) -> dict:
    patches = load_patches()
    rows = []
    for patch in patches:
        state = patch_state(app, patch)
        rows.append(
            {
                "id": patch["id"],
                "title": patch.get("title", ""),
                "default": patch.get("default", True),
                "version_match": patch_matches_version(patch, app),
                "try_on_unknown": patch.get("try_on_unknown", False),
                "state": state,
                "file": patch["_file"],
            }
        )
    return {
        "app": str(app.path),
        "version": app.version,
        "build": app.build,
        "bundle_id": app.bundle_id,
        "running": app.running(),
        "patches": rows,
        "operations": load_state().get("operations", []),
    }


# doctor


def relay_usage(app: App) -> dict | None:
    cua = app.cua_cli()
    if not cua:
        return None
    rc, out, err = run([str(cua), "usage", "--json"], timeout=45)
    if rc != 0:
        return {"error": (err or out)[:300]}
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"error": out[:300]}


def doctor(app: App) -> dict:
    report = {
        "schema": SCHEMA,
        "generated_at": now(),
        "app": {
            "path": str(app.path),
            "version": app.version,
            "build": app.build,
            "bundle_id": app.bundle_id,
            "layout": app.layout,
            "platform": sys.platform,
            "running": app.running(),
        },
        "checks": [],
        "status": status(app),
        "relay_usage": relay_usage(app),
    }
    checks = report["checks"]

    def add(name: str, ok: bool, detail: str, level: str = "error"):
        checks.append({"name": name, "ok": ok, "level": "ok" if ok else level, "detail": detail})

    add("app exists", app.path.exists(), str(app.path))
    sig = app.signature()
    add("code signature", sig["ok"], sig["detail"], level="warn")
    for row in report["status"]["patches"]:
        if row["state"] == "applied":
            add(f"patch {row['id']}", True, "applied")
        elif row["state"] == "available":
            add(f"patch {row['id']}", False, "available, not applied", level="warn")
        elif row["state"] == "stale":
            add(f"patch {row['id']}", False, "stale: the app changed; run auto", level="error")
        elif row["state"] == "partial":
            add(f"patch {row['id']}", False, "partially applied; run undo first", level="error")
        elif row["state"] == "mismatch":
            if not row["version_match"] and not row["try_on_unknown"]:
                add(f"patch {row['id']}", False, f"not for {app.version}", level="warn")
            else:
                add(f"patch {row['id']}", False, "marker missing or version mismatch")
    for op in report["status"]["operations"]:
        patch = next((p for p in load_patches() if p["id"] == op.get("patch")), None)
        for f in op.get("files", []):
            path = Path(f["path"])
            backup = Path(f["backup"])
            if not path.exists():
                add(f"file {f['relative']}", False, "patched file missing")
                continue
            got = sha256_file(path)
            if got == f["after"]:
                add(f"file {f['relative']}", True, "matches patched hash")
            elif patch and applied_marker_present(app, patch):
                add(f"file {f['relative']}", True, "matches applied marker after a dependent patch")
            else:
                add(f"file {f['relative']}", False, f"drifted: {got[:12]} != {f['after'][:12]}")
            add(
                f"backup {f['relative']}",
                backup.exists() and sha256_file(backup) == f["before"],
                str(backup),
            )
    usage = report["relay_usage"]
    if usage and usage.get("error"):
        add("relay usage", False, str(usage["error"]), level="warn")
    elif usage:
        used = float(usage.get("hours_used") or 0)
        limit = usage.get("hours_limit")
        if limit:
            pct = used / limit if limit else 0.0
            if used >= limit or usage.get("soft_limit_reached"):
                add(
                    "relay hours",
                    False,
                    f"{used:g} of {limit:g} used; the relay may refuse a new session if enforcement is on",
                    level="warn",
                )
            elif pct >= 0.8:
                add("relay hours", False, f"{used:g} of {limit:g} used (80% or more)", level="warn")
            else:
                add("relay hours", True, f"{used:g} of {limit:g} used")
    report["ok"] = all(c["ok"] for c in checks if c["level"] == "error")
    return report


# collect


def collect(app: App, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or REPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    patches = load_patches()
    assets: list[Path] = []
    for directory in app.web_asset_dirs():
        assets.extend(sorted(directory.glob("*.js")))
    assets = sorted(set(assets))
    inventory = []
    for p in assets:
        text = p.read_text(errors="replace")
        inventory.append(
            {
                "path": p.relative_to(app.path).as_posix(),
                "size": p.stat().st_size,
                "sha256": sha256_file(p),
                "counts": {
                    "this.usage": text.count("this.usage"),
                    "metered": text.count("metered"),
                    "hoursLimit": text.count("hoursLimit"),
                    "hours_limit": text.count("hours_limit"),
                    "softLimitReached": text.count("softLimitReached"),
                    "relay.usage": text.count("relay.usage"),
                },
                "contexts": {
                    "this.usage": context_around(text, "this.usage", 240)[:3],
                    "metered": context_around(text, "metered", 240)[:3],
                },
            }
        )
    asar_package = None
    for rel in ("resources/app.asar", "Contents/Resources/app.asar"):
        asar_package = read_asar_package_json(app.path / rel)
        if asar_package:
            break
    report = {
        "schema": SCHEMA,
        "generated_at": now(),
        "host": {
            "platform": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
        },
        "app": {
            "path": str(app.path),
            "version": app.version,
            "build": app.build,
            "bundle_id": app.bundle_id,
            "layout": app.layout,
            "running": app.running(),
            "info_plist": app.info,
            "asar_package": asar_package,
            "signature": app.signature(),
        },
        "status": status(app),
        "patches": [],
        "asset_inventory": inventory,
        "relay_usage": relay_usage(app),
    }
    for patch in patches:
        entry = {"id": patch["id"], "state": patch_state(app, patch), "targets": []}
        for target in patch.get("targets", []):
            for path in target_files(app, target):
                data = path.read_bytes()
                kind = target.get("kind", "text")
                find = bytes.fromhex(target["find"]) if kind == "bytes" else target["find"].encode()
                repl = bytes.fromhex(target["replace"]) if kind == "bytes" else target["replace"].encode()
                find_count = data.count(find)
                replace_count = data.count(repl)
                text = data.decode("utf-8", "replace")
                if not (find_count or replace_count or "this.usage" in text):
                    continue
                entry["targets"].append(
                    {
                        "path": path.relative_to(app.path).as_posix(),
                        "sha256": sha256_bytes(data),
                        "find_count": find_count,
                        "replace_count": replace_count,
                        "find_context": context_around(text, target["find"], 300)[:3] if kind == "text" else [],
                        "near_misses": [
                            c for c in context_around(text, "this.usage", 300)[:5] if target["find"] not in c
                        ],
                    }
                )
        report["patches"].append(entry)
    stamp_value = stamp()
    report_path = out_dir / f"{stamp_value}-{app.version}.json"
    write_json(report_path, report)
    archive = out_dir / f"{stamp_value}-{app.version}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(report_path, arcname=report_path.name)
        info_path = app.path / "Contents" / "Info.plist"
        if info_path.exists():
            tar.add(info_path, arcname="Info.plist")
        if asar_package:
            payload = (json.dumps(asar_package, indent=2) + "\n").encode()
            entry = tarfile.TarInfo("package.json")
            entry.size = len(payload)
            tar.addfile(entry, io.BytesIO(payload))
    return report_path


# auto


def auto(app: App, quiet: bool = False) -> int:
    patches = load_patches()
    defaults = [p for p in patches if p.get("default", True)]
    try:
        ops = apply_many(app, defaults)
    except PatchError as exc:
        path = collect(app)
        if quiet:
            print(f"patch failed: {exc}; report: {path}", file=sys.stderr)
        else:
            print(f"patch failed: {exc}")
            print(f"report: {path}")
        return 1
    changed = [op for op in ops if not op.get("noop")]
    if not changed:
        states = {p["id"]: patch_state(app, p) for p in defaults}
        if defaults and not any(s == "applied" for s in states.values()):
            path = collect(app)
            if quiet:
                print(f"no patch matches {app.version} ({app.build}); report: {path}", file=sys.stderr)
            else:
                print(f"no patch matches {app.version} ({app.build}); report: {path}")
            return 1
    if not quiet:
        if changed:
            print(f"applied {len(changed)} patch operation(s) to {app.version} ({app.build})")
            print("quit and reopen Cua Spaces to load the patched UI")
        else:
            print(f"nothing to do: {app.version} ({app.build}) is patched")
    return 0


# watch


def watch_paths(app: App) -> list[str]:
    paths: list[str] = []
    for candidate in (
        app.path / "Contents" / "Info.plist",
        app.path / "Contents" / "Resources" / "app.asar",
        app.path / "resources" / "app.asar",
    ):
        if candidate.exists():
            paths.append(str(candidate))
    paths.extend(str(d) for d in app.web_asset_dirs())
    return list(dict.fromkeys(paths))


def watch_plist(app: App) -> dict:
    return {
        "Label": WATCH_LABEL,
        "ProgramArguments": [
            sys.executable,
            str(REPO_ROOT / "bin" / "cua-unlimit"),
            "--app",
            str(app.path),
            "auto",
            "--quiet",
        ],
        "WatchPaths": watch_paths(app),
        "RunAtLoad": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(LOG_DIR / "watch.log"),
        "StandardErrorPath": str(LOG_DIR / "watch.log"),
    }


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


WATCH_TASK_NAME = "cua-unlimit-watch"


def watch_cmd_path() -> Path:
    return STATE_DIR / "watch.cmd"


def watch_vbs_path() -> Path:
    return startup_dir() / "cua-unlimit-watch.vbs"


def watch_task_xml_path() -> Path:
    return STATE_DIR / "watch-task.xml"


def watch_task_user() -> str:
    domain = os.environ.get("USERDOMAIN") or ""
    user = os.environ.get("USERNAME") or ""
    if not domain or domain.upper() == "WORKGROUP":
        domain = platform.node().split(".")[0]
    return f"{domain}\\{user}" if domain and user else user


def watch_task_xml(app: App) -> str:
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    if pythonw.exists():
        python = pythonw
    cli = REPO_ROOT / "bin" / "cua-unlimit"
    user = watch_task_user()
    args = f'"{cli}" --app "{app.path}" auto --quiet'
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Re-apply cua-unlimit patches after Cua Spaces updates</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
    <SessionStateChangeTrigger>
      <Enabled>true</Enabled>
      <StateChange>SessionUnlock</StateChange>
      <UserId>{user}</UserId>
    </SessionStateChangeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{python}</Command>
      <Arguments>{args}</Arguments>
    </Exec>
  </Actions>
</Task>
'''


def watch_install_windows(app: App, start: bool = True) -> Path:
    if start:
        watch_uninstall_windows()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = watch_task_xml_path()
    with path.open("w", encoding="utf-16", newline="") as fh:
        fh.write(watch_task_xml(app))
    if start:
        rc, out, err = run(
            ["schtasks", "/Create", "/TN", WATCH_TASK_NAME, "/XML", str(path), "/F"],
            timeout=30,
        )
        if rc != 0:
            raise SystemExit(f"schtasks create failed: {err or out}")
        run(["schtasks", "/Run", "/TN", WATCH_TASK_NAME], timeout=15)
    return path


def watch_process_query() -> str:
    """PowerShell that selects only legacy loop processes from this install."""
    cmd = str(watch_cmd_path()).replace("'", "''").lower()
    cli = str(REPO_ROOT / "bin" / "cua-unlimit").replace("'", "''").lower()
    return (
        "Get-CimInstance Win32_Process | Where-Object { "
        "($_.Name -eq 'cmd.exe' -and $_.CommandLine -and "
        f"$_.CommandLine.ToLower().Contains('{cmd}')) -or "
        "(($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') -and "
        f"$_.CommandLine -and $_.CommandLine.ToLower().Contains('{cli}') -and "
        "$_.CommandLine.ToLower().Contains('auto')) "
        "}"
    )


def watch_uninstall_windows() -> Path:
    run(["schtasks", "/Delete", "/TN", WATCH_TASK_NAME, "/F"], timeout=20)
    ps = watch_process_query() + " | Select-Object -ExpandProperty ProcessId"
    rc, out, _ = run(["powershell", "-NoProfile", "-Command", ps], timeout=20)
    if rc == 0:
        for pid in out.split():
            if pid.isdigit():
                run(["taskkill", "/F", "/PID", pid], timeout=10)
    for path in (watch_vbs_path(), watch_cmd_path()):
        if path.exists():
            path.unlink()
    return watch_task_xml_path()


def watch_status_windows() -> dict:
    rc, out, err = run(
        ["schtasks", "/Query", "/TN", WATCH_TASK_NAME, "/FO", "LIST"], timeout=20
    )
    installed = rc == 0
    loaded = installed and "Disabled" not in out
    return {
        "installed": installed,
        "loaded": loaded,
        "plist": f"task:{WATCH_TASK_NAME}",
        "detail": (out or err)[:500],
    }


def watch_install(app: App) -> Path:
    if IS_WINDOWS:
        return watch_install_windows(app)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = Path.home() / "Library" / "LaunchAgents" / f"{WATCH_LABEL}.plist"
    with path.open("wb") as fh:
        plistlib.dump(watch_plist(app), fh)
    domain = f"gui/{os.getuid()}"
    run(["launchctl", "bootout", domain, str(path)], timeout=30)
    rc, out, err = run(["launchctl", "bootstrap", domain, str(path)], timeout=30)
    if rc != 0:
        raise SystemExit(f"launchctl bootstrap failed: {err or out}")
    return path


def watch_uninstall() -> Path:
    if IS_WINDOWS:
        return watch_uninstall_windows()
    path = Path.home() / "Library" / "LaunchAgents" / f"{WATCH_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    run(["launchctl", "bootout", domain, str(path)], timeout=30)
    if path.exists():
        path.unlink()
    return path


def watch_status() -> dict:
    if IS_WINDOWS:
        return watch_status_windows()
    path = Path.home() / "Library" / "LaunchAgents" / f"{WATCH_LABEL}.plist"
    rc, out, err = run(["launchctl", "print", f"gui/{os.getuid()}/{WATCH_LABEL}"], timeout=30)
    return {"installed": path.exists(), "loaded": rc == 0, "plist": str(path), "detail": (out or err)[:500]}
