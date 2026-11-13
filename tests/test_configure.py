import importlib.util
import json
from pathlib import Path
import shlex
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("configure_under_test", SOURCE / "scripts/configure.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "linux user"
        self.windows = self.root / "Windows User"
        (self.windows / "Documents").mkdir(parents=True)
        self.home.mkdir()
        self.base = self.home / "bridge's environment"
        self.mcp = self.home / ".gemini/config/mcp_config.json"
        self.mcp.parent.mkdir(parents=True)
        self.original = {"mcpServers": {"unrelated": {"command": "keep-me"}}, "preferences": {"color": "blue"}}
        self.mcp.write_text(json.dumps(self.original))

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, **kwargs):
        return configure.build_plan(self.base, self.home, self.windows, "Ubuntu-24.04", "tester", **kwargs)

    def test_install_merges_configs_and_generates_quoted_paths(self):
        plan = self.plan()
        backup = configure.apply_plan(self.base, plan)
        self.assertTrue((backup / "manifest.json").exists())
        current = json.loads(self.mcp.read_text())
        self.assertEqual(current["preferences"], self.original["preferences"])
        self.assertEqual(current["mcpServers"]["unrelated"], self.original["mcpServers"]["unrelated"])
        windows_config = json.loads((self.windows / ".gemini/config/mcp_config.json").read_text())
        server = windows_config["mcpServers"]["codex-worker"]
        self.assertEqual(server["args"][:5], ["-d", "Ubuntu-24.04", "-u", "tester", "--"])
        self.assertEqual(server["args"][-1], str(self.base / "bin/start-mcp.sh"))
        command = shlex.split((self.base / "bin/start-mcp.sh").read_text().splitlines()[1])
        self.assertEqual(command[1], str(self.base / "venv/bin/python"))
        self.assertEqual(command[2], str(self.base / "app/bridge.py"))
        import tomllib
        config = tomllib.loads((self.base / "codex-home/config.toml").read_text())
        self.assertFalse(config["allow_login_shell"])
        self.assertTrue(config["shell_environment_policy"]["set"]["PATH"].startswith(str(self.base)))

    def test_rerun_is_idempotent_and_preserves_custom_settings_and_auth(self):
        configure.apply_plan(self.base, self.plan())
        settings_file = self.base / "app/settings.json"
        settings = json.loads(settings_file.read_text())
        settings["execution_path"] += ":/opt/project-tools/bin"
        settings["workspace_roots"].append("/mnt/d/Projects")
        settings_file.write_text(configure.as_json(settings))
        worker_config = self.base / "codex-home/config.toml"
        worker_config.write_text('model = "test-custom-model"\n')
        auth = self.base / "codex-home/auth.json"
        auth.write_text("test fixture -- never copy this")
        plan = self.plan()
        self.assertNotIn(auth, plan)
        self.assertNotIn(worker_config, plan)
        self.assertIsNone(configure.apply_plan(self.base, plan))
        self.assertEqual(auth.read_text(), "test fixture -- never copy this")
        self.assertIn("/opt/project-tools/bin", settings_file.read_text())
        self.assertIn("/mnt/d/Projects", settings_file.read_text())

    def test_invalid_existing_json_is_rejected_before_writes(self):
        self.mcp.write_text("invalid JSON")
        with self.assertRaises(ValueError):
            self.plan()
        self.assertFalse(self.base.exists())
        self.assertEqual(self.mcp.read_text(), "invalid JSON")

    def test_conflicting_entry_requires_explicit_replace_and_keeps_backup(self):
        content = {"mcpServers": {"codex-worker": {"command": "someone-elses-bridge"}}}
        self.mcp.write_text(json.dumps(content))
        with self.assertRaisesRegex(ValueError, "existing codex-worker"):
            self.plan()
        backup = configure.apply_plan(self.base, self.plan(replace_existing=True))
        manifest = json.loads((backup / "manifest.json").read_text())
        entry = next(r for r in manifest["files"] if r["path"] == str(self.mcp))
        self.assertEqual(json.loads(Path(entry["previous"]).read_text()), content)

    def test_failed_write_restores_already_applied_files(self):
        plan = self.plan()
        original_write = configure.atomic_write
        def injected_failure(path, content, mode=0o600):
            if path == self.windows / ".gemini/config/mcp_config.json":
                raise OSError("Simulated disk error")
            return original_write(path, content, mode)
        with patch.object(configure, "atomic_write", side_effect=injected_failure):
            with self.assertRaisesRegex(OSError, "Simulated disk error"):
                configure.apply_plan(self.base, plan)
        self.assertEqual(json.loads(self.mcp.read_text()), self.original)
        self.assertFalse((self.base / "app/settings.json").exists())

    def test_active_jobs_prevent_installation(self):
        (self.base / "state").mkdir(parents=True)
        with sqlite3.connect(self.base / "state/jobs.sqlite3") as db:
            db.execute("CREATE TABLE jobs (state TEXT)")
            db.execute("INSERT INTO jobs VALUES ('running')")
        with self.assertRaisesRegex(ValueError, "jobs are active"):
            self.plan()
        self.assertFalse((self.base / "app").exists())

    def test_wsl_only_and_custom_configuration_directory(self):
        custom = self.root / "testing-config"
        plan = configure.build_plan(self.base, self.home, None, "Debian", "tester", wsl_config_dir=custom)
        configure.apply_plan(self.base, plan)
        self.assertIn("codex-worker", json.loads((custom / "mcp_config.json").read_text())["mcpServers"])
        self.assertEqual(json.loads(self.mcp.read_text()), self.original)
        self.assertFalse((self.windows / ".gemini").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
