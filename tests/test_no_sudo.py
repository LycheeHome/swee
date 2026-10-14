"""swee runs no sudo: host control goes through lychee-admin's host-operations
socket (swee/host_ops.py). These guard against it creeping back."""
import ast
import os
import pathlib
import unittest

os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("ADMIN_ROLE_ID", "1")
os.environ.setdefault("RELAY_CHANNEL_ID", "1")
os.environ.setdefault("STATS_CHANNEL_ID", "1")
os.environ.setdefault("ACTIVITY_CHANNEL_ID", "1")
os.environ.setdefault("ALERTS_CHANNEL_ID", "1")
os.environ.setdefault("ADMIN_CHANNEL_ID", "1")
os.environ.setdefault("COMMANDS_CHANNEL_ID", "1")
os.environ.setdefault("BOT_UPDATES_CHANNEL_ID", "1")
os.environ.setdefault("REST_HOST", "x")
os.environ.setdefault("REST_PORT", "1")
os.environ.setdefault("REST_USER", "x")
os.environ.setdefault("REST_PASSWORD", "x")
os.environ.setdefault("PALWORLD_SETTINGS_INI_PATH", "/tmp/x")

ROOT = pathlib.Path(__file__).resolve().parent.parent


class NoSudoTests(unittest.TestCase):
    def test_no_module_runs_sudo(self):
        sources = sorted((ROOT / "swee").glob("*.py")) + [ROOT / "main.py"]
        self.assertGreater(len(sources), 2)
        offenders = []
        for path in sources:
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "sudo":
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [], "a module still runs sudo")

    def test_wrapper_setting_is_gone(self):
        import swee.config as config
        self.assertFalse(hasattr(config, "SWEE_UPDATE_WRAPPER"))


if __name__ == "__main__":
    unittest.main()
