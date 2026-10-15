"""swee runs in a container (python:3.14-slim, read-only root): there is no
systemctl and no journalctl in it, and neither must creep back in as
something swee runs. Text that merely names them for a human to type
(`journalctl -u palworld` in an embed) is fine; an exact "systemctl" or
"journalctl" string constant is what an argv would contain."""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


class NoHostToolsTests(unittest.TestCase):
    def offenders(self, word):
        sources = sorted((ROOT / "swee").glob("*.py")) + [ROOT / "main.py"]
        self.assertGreater(len(sources), 2)
        found = []
        for path in sources:
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == word:
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        return found

    def test_no_systemctl_anywhere(self):
        self.assertEqual(self.offenders("systemctl"), [], "a module still runs systemctl")

    def test_no_journalctl_anywhere(self):
        self.assertEqual(self.offenders("journalctl"), [], "a module still runs journalctl")


class ImageFilesTests(unittest.TestCase):
    def test_dockerfile_pins_the_contract(self):
        text = (ROOT / "Dockerfile").read_text()
        self.assertIn("FROM python:3.14-slim", text)
        self.assertIn("USER 993:980", text)
        self.assertIn("WORKDIR /state", text)
        self.assertIn("HEALTHCHECK", text)
        self.assertIn("/state/heartbeat", text)
        self.assertIn('org.opencontainers.image.source="https://github.com/LycheeHome/swee"', text)

    def test_dockerignore_keeps_secrets_and_state_out(self):
        lines = (ROOT / ".dockerignore").read_text().split()
        for entry in (".env", ".git", ".venv", "*.json", "tests"):
            self.assertIn(entry, lines)


if __name__ == "__main__":
    unittest.main()
