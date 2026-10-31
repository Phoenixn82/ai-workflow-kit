#!/usr/bin/env python3
"""Check the offline runner's fixtures and PASS/SKIP/FAIL contract with fake tools."""
from pathlib import Path
import subprocess
import tempfile
import unittest

RUNNER = Path(__file__).with_name("run-selftests.sh").resolve()


class Selftests(unittest.TestCase):
    def test_skip_does_not_hide_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            fw = Path(directory)
            for name, body in {
                "pass.sh": '[ -d "$XDG_RUNTIME_DIR" ] && [ "$HOME/runtime" = "$XDG_RUNTIME_DIR" ]',
                "skip.sh": 'echo "missing optional dependency"; exit 77',
                "fail.sh": 'echo "SKIP text must not hide a failing exit status"; exit 1',
            }.items():
                (fw / name).write_text(body + "\n")
            env = {"PATH": "/usr/bin:/bin"}
            run = lambda *names: subprocess.run(["bash", str(RUNNER), str(fw), *names],
                                                env=env, capture_output=True, text=True, timeout=15)
            result = run("pass.sh", "skip.sh")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS pass.sh", result.stdout)
            self.assertIn("SKIP skip.sh: missing optional dependency", result.stdout)
            result = run("skip.sh", "fail.sh")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("FAIL fail.sh (rc 1)", result.stdout)

    def test_weekly_review_gets_its_installed_files_in_an_empty_home(self):
        with tempfile.TemporaryDirectory() as directory:
            fw = Path(directory)
            for name in ("lanes.json", "automations/preflight.py", "automations/record_spend.py"):
                path = fw / "_system" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n")
            tool = fw / "_tools/weekly-review/run.sh"
            tool.parent.mkdir(parents=True)
            tool.write_text('for f in lanes.json automations/preflight.py automations/record_spend.py; do\n'
                            '  test -f "$HOME/AI_Projects/_system/$f" || exit 1\ndone\n')
            result = subprocess.run(["bash", str(RUNNER), str(fw), "_tools/weekly-review/run.sh"],
                                    env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS _tools/weekly-review/run.sh", result.stdout)

    def test_vault_rag_py_survives_the_empty_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            fw = Path(directory)
            tool = fw / "_tools/vault-rag/vault_rag.py"
            tool.parent.mkdir(parents=True)
            tool.write_text("raise SystemExit(3)\n")   # fails if plain python3 runs it instead of VAULT_RAG_PY
            py = fw / "py"
            py.write_text(f'#!/bin/sh\n[ "$1" = "{tool}" ] && [ "$2" = --self-test ]\n')
            recall = tool.with_name("vault-recall")
            recall.write_text(f'#!/bin/sh\n[ "$VAULT_RAG_PY" = "{py}" ]\n')
            py.chmod(0o755); recall.chmod(0o755)
            result = subprocess.run(["bash", str(RUNNER), str(fw), "_tools/vault-rag/vault_rag.py", "_tools/vault-rag/vault-recall"],
                                    env={"PATH": "/usr/bin:/bin", "VAULT_RAG_PY": str(py)}, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS _tools/vault-rag/vault_rag.py", result.stdout)
            self.assertIn("PASS _tools/vault-rag/vault-recall", result.stdout)


if __name__ == "__main__":
    unittest.main()
