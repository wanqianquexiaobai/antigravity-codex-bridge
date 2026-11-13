#!/usr/bin/env python3
"""Generate machine-specific configuration without copying account credentials."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import tempfile
import uuid

SOURCE = Path(__file__).resolve().parents[1]


def as_json(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def linux_path(value):
    value = str(value)
    if re.match(r"^[A-Za-z]:[\\/]", value):
        value = "/mnt/" + value[0].lower() + "/" + value[3:].replace("\\", "/")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("Use absolute paths: " + value)
    if any(char in str(path) for char in "\r\n\x00"):
        raise ValueError("Paths cannot contain control characters.")
    return path.resolve()


def windows_profile():
    shell = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    try:
        result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command",
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
            "[Environment]::GetFolderPath('UserProfile')"],
            check=True, capture_output=True, encoding="utf-8", timeout=15)
        value = result.stdout.strip().lstrip("\ufeff")
        if not re.match(r"^[A-Za-z]:[\\/]", value):
            raise ValueError("Unexpected Windows profile path")
        path = linux_path(value)
        if not path.is_dir():
            raise ValueError("Windows profile is not accessible")
        return path
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ValueError("Could not detect the Windows user profile. Pass "
                         "--windows-home /mnt/c/Users/YOUR_NAME or --wsl-only.") from exc


def read_json(path):
    if not path.exists():
        return {}
    result = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object: " + str(path))
    return result


def check_idle(base):
    database = base / "state/jobs.sqlite3"
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            count = db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
        if count:
            raise ValueError("Codex jobs are active. Finish/cancel them before reinstalling. "
                             "Use codex_list_jobs to refresh interrupted jobs first.")


def build_plan(base, user_home, windows_home, distribution, username,
               workspace_roots=(), replace_existing=False, source=SOURCE, wsl_config_dir=None):
    """Validate every input before returning files to write; no mutations here."""
    base, user_home = linux_path(base), linux_path(user_home)
    if base == user_home or base == Path("/") or base.is_relative_to(user_home / ".codex"):
        raise ValueError("Use a separate installation directory.")
    if not distribution or not username or any(c in distribution + username for c in "\r\n\x00"):
        raise ValueError("WSL distribution and Linux username are required.")
    if windows_home is not None:
        windows_home = linux_path(windows_home)
        if not windows_home.is_dir():
            raise ValueError("Windows user profile directory does not exist.")
    check_idle(base)
    settings_path = base / "app/settings.json"
    previous = read_json(settings_path)
    roots = list(previous.get("workspace_roots", [])) or [str(user_home)]
    if windows_home and (windows_home / "Documents").is_dir():
        roots.append(str(windows_home / "Documents"))
    for value in workspace_roots:
        root = linux_path(value)
        if not root.is_dir():
            raise ValueError("Workspace root must already exist: " + str(root))
        roots.append(str(root))
    roots = list(dict.fromkeys(roots))
    execution_path = previous.get("execution_path") or ":".join(map(str, [
        base / "bin", base / "venv/bin", user_home / ".local/bin",
        "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
    protected = [base, user_home / ".codex", user_home / ".gemini", user_home / ".ssh"]
    if windows_home:
        protected += [windows_home / name for name in (".codex", ".gemini", ".ssh")]
    settings = {**previous,
        "codex_binary": str(base / "bin/codex"), "codex_home": str(base / "codex-home"),
        "state_directory": str(base / "state"), "launcher": str(base / "bin/codex-worker"),
        "wsl_distribution": distribution, "workspace_roots": roots,
        "protected_directories": list(dict.fromkeys([
            *previous.get("protected_directories", []), *map(str, protected)])),
        "execution_path": execution_path, "max_concurrent_jobs": 1}
    # target -> (UTF-8 bytes, Unix mode)
    plan = {settings_path: (as_json(settings).encode(), 0o600)}
    for name in ("bridge.py", "launch_cli.py"):
        plan[base / "app" / name] = ((source / name).read_bytes(), 0o600)
    python = shlex.quote(str(base / "venv/bin/python"))
    app = base / "app"
    plan[base / "bin/start-mcp.sh"] = (("#!/bin/sh\nexec " + python + " " +
        shlex.quote(str(app / "bridge.py")) + " --config " +
        shlex.quote(str(settings_path)) + " serve\n").encode(), 0o700)
    plan[base / "bin/codex-worker"] = (("#!/bin/sh\nexec " + python + " " +
        shlex.quote(str(app / "launch_cli.py")) + ' "$@"\n').encode(), 0o700)
    config_path = base / "codex-home/config.toml"
    if not config_path.exists():
        config = '''cli_auth_credentials_store = "file"
approval_policy = "never"
sandbox_mode = "workspace-write"
allow_login_shell = false

[shell_environment_policy]
inherit = "core"
[shell_environment_policy.set]
PATH = PATH_VALUE

[features]
shell_snapshot = false
[sandbox_workspace_write]
network_access = true
[agents]
enabled = false
'''.replace("PATH_VALUE", json.dumps(execution_path, ensure_ascii=False))
        plan[config_path] = (config.encode(), 0o600)
    plan[base / "codex-home/AGENTS.md"] = ((source / "worker-AGENTS.md").read_bytes(), 0o600)
    configs = [(linux_path(wsl_config_dir) if wsl_config_dir else user_home / ".gemini/config", {
        "command": str(base / "bin/start-mcp.sh"), "args": [], "timeoutSeconds": 90})]
    if windows_home:
        configs.append((windows_home / ".gemini/config", {
            "command": "wsl.exe", "args": ["-d", distribution, "-u", username, "--",
                str(base / "bin/start-mcp.sh")], "timeoutSeconds": 90}))
    for root, server in configs:
        path = root / "mcp_config.json"
        content = read_json(path)
        servers = content.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcpServers must be an object: " + str(path))
        existing = servers.get("codex-worker")
        if existing and existing != server and not replace_existing:
            raise ValueError("An existing codex-worker entry differs in " + str(path) +
                             ". Inspect it first; use --replace-existing to back up and replace it.")
        servers["codex-worker"] = server
        plan[path] = (as_json(content).encode(), 0o600)
        plan[root / "skills/codex-delegate/SKILL.md"] = ((source / "codex-delegate/SKILL.md").read_bytes(), 0o600)
        plan[root / "rules/codex-delegation.md"] = ((source / "gemini-codex-rule.md").read_bytes(), 0o600)
    return plan


def atomic_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".bridge-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def apply_plan(base, plan):
    base = linux_path(base)
    for path in plan:
        if path.is_symlink():
            raise ValueError("Refusing to replace a symlink: " + str(path))
    before = {path: (path.read_bytes(), path.stat().st_mode & 0o777)
              if path.exists() else None for path in plan}
    changed = {p: value for p, value in plan.items() if before[p] is None or before[p][0] != value[0]}
    if not changed:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    backup = base / "backups" / stamp
    backup.mkdir(parents=True, mode=0o700)
    records = []
    applied = []
    try:
        for index, (path, (content, mode)) in enumerate(changed.items()):
            old = before[path]
            if (path.read_bytes() if path.exists() else None) != (old[0] if old else None):
                raise ValueError("File changed during installation: " + str(path))
            saved = backup / f"{index:03d}.bak" if old else None
            if saved:
                atomic_write(saved, old[0])
            records.append({"path": str(path), "previous": str(saved) if saved else None,
                            "previous_mode": old[1] if old else None,
                            "installed_sha256": hashlib.sha256(content).hexdigest()})
            atomic_write(path, content, mode)
            applied.append(path)
        manifest = {"format": 1, "backup_directory": str(backup), "files": records}
        atomic_write(backup / "manifest.json", as_json(manifest).encode())
        atomic_write(base / "installation.json", as_json(manifest).encode())
    except BaseException:
        for path in reversed(applied):
            if path.read_bytes() != changed[path][0]:
                continue
            if before[path] is None:
                path.unlink()
            else:
                atomic_write(path, *before[path])
        raise
    return backup


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-dir", default=str(Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "antigravity-codex"))
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--windows-home")
    location.add_argument("--wsl-only", action="store_true")
    parser.add_argument("--workspace-root", action="append", default=[])
    parser.add_argument("--wsl-config-dir", help="Override the WSL Antigravity config directory")
    parser.add_argument("--replace-existing", action="store_true")
    parser.add_argument("--check", action="store_true", help="Validate and list targets without writing configuration")
    args = parser.parse_args()
    try:
        windows_home = None if args.wsl_only else (linux_path(args.windows_home) if args.windows_home else windows_profile())
        base = linux_path(args.install_dir)
        plan = build_plan(base, Path.home(), windows_home, os.environ.get("WSL_DISTRO_NAME", ""),
                          getpass.getuser(), args.workspace_root, args.replace_existing,
                          wsl_config_dir=args.wsl_config_dir)
        if args.check:
            print("Configuration validated; target files: " + str(len(plan)))
            return
        backup = apply_plan(base, plan)
        print("Configuration is already up to date." if backup is None else "Configuration installed. Backup: " + str(backup))
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, "Configuration error: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
