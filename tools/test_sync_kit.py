#!/usr/bin/env python3
"""Runnable check for sync-kit's staging rules added in kit v3 (T2): explicitly named entries, kept data files,
allowlisted links copied from their first-party target, new exclusions, venv console scripts in the wiring check,
and no exported file dropped by .gitignore.
Fake files only.  Run: python3 tools/test_sync_kit.py"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent


def write(path, text, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode:
        path.chmod(mode)
    return path


class Staging(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kit-sync-test-"))
        rules = write(self.tmp / "rules.json", json.dumps({"replacements": [{"find": "x", "replace": "y"}]}))
        self._old = {k: os.environ.get(k) for k in ("SCRUB_RULES", "PATH")}
        os.environ["SCRUB_RULES"] = str(rules)
        os.environ["PATH"] = "/usr/bin:/bin"
        spec = importlib.util.spec_from_file_location("sync_kit", TOOLS / "sync-kit.py")
        self.sk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sk)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_named_entries_and_exclusions(self):
        src, out = self.tmp / "src", self.tmp / "out"
        write(src / "corpus.jsonl", "{}\n")                    # KEEP_FILES beats the *.jsonl log exclusion
        write(src / "run.log.jsonl", "{}\n")
        write(src / "tests" / "test_a.py", "pass\n")             # a dir named in the allowlist ships
        write(src / "tests" / "test_a.py.bak-1", "old\n")        # ...but file exclusions still apply inside it
        write(src / "units" / "tests" / "t.py", "pass\n")         # a walked (not named) tests dir is still dropped
        write(src / "units" / "a.service", "[Service]\n")
        write(src / "units" / "a.service.proposed", "[Service]\n")
        write(src / "units" / "deployed" / "copy", "x\n")         # per-host deploy snapshots never ship
        write(src / "settings.json.hooks.original", "{}\n")
        names = ["corpus.jsonl", "run.log.jsonl", "tests", "units", "settings.json.hooks.original"]
        self.sk.stage_source(src, out, names)
        got = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
        self.assertEqual(got, ["corpus.jsonl", "tests/test_a.py", "units/a.service"])

    def test_router_gemini_wrench_is_exported(self):
        aip, home, out = self.tmp / "AI_Projects", self.tmp / "home", self.tmp / "out"
        relative = Path("router/wrenches/gemini.md")
        published = TOOLS.parent / "framework/_system/skills" / relative
        source = write(aip / "_system/skills" / relative, published.read_text())
        rows = [(src, dest, names) for src, dest, names in self.sk.sources(aip, home / ".claude", home)
                if dest == "_system/skills"]
        self.assertEqual(len(rows), 1)
        src, dest, names = rows[0]
        self.sk.stage_source(src, out / dest, names)
        self.assertEqual((out / dest / relative).read_bytes(), source.read_bytes())

    def test_allowlisted_link_ships_its_first_party_target(self):
        root, bindir, out = self.tmp / "AI_Projects", self.tmp / "bin", self.tmp / "out"
        write(root / "_tools" / "t" / "route.py", "#!/usr/bin/env python3\n", 0o755)
        bindir.mkdir()
        (bindir / "route").symlink_to(root / "_tools" / "t" / "route.py")
        (bindir / "outside").symlink_to("/usr/bin/env")
        self.sk.LINK_ROOT = root.resolve()
        self.sk.stage_source(bindir, out, ["route"])
        self.assertEqual((out / "route").read_text(), "#!/usr/bin/env python3\n")
        self.assertFalse((out / "route").is_symlink())
        with self.assertRaisesRegex(RuntimeError, "missing allowlisted file"):   # a link outside AI_Projects never ships
            self.sk.stage_source(bindir, self.tmp / "out2", ["outside"])

    def test_wiring_accepts_venv_console_script_of_an_exported_project(self):
        home, fw = self.tmp / "home", self.tmp / "fw"
        project = home / "AI_Projects" / "_tools" / "lanes"
        write(project / "pyproject.toml", "[project]\nname='lanes'\n")
        write(project / ".venv" / "bin" / "lane", "#!/usr/bin/env python3\n", 0o755)
        (home / ".local" / "bin").mkdir(parents=True)
        (home / ".local" / "bin" / "lane").symlink_to(project / ".venv" / "bin" / "lane")
        write(fw / "harness" / "bin" / "cx", "#!/usr/bin/env bash\nlane run x\n", 0o755)
        with self.assertRaisesRegex(RuntimeError, "calls 'lane'"):
            self.sk.assert_wiring(fw, [(project, "_tools/lanes", ["README.md"])], home, {"tools": []})
        write(fw / "_tools" / "lanes" / "pyproject.toml", "[project]\nname='lanes'\n")
        self.sk.assert_wiring(fw, [(project, "_tools/lanes", ["pyproject.toml"])], home, {"tools": []})


    def test_wiring_uses_manifest_for_installed_copies_and_third_party_shims(self):
        home, fw = self.tmp / "home", self.tmp / "fw"
        write(home / ".local/bin/hermes", "#!/bin/sh\nexit 0\n", 0o755)
        write(home / ".local/bin/thor-wake-proxy", "#!/bin/sh\nexit 0\n", 0o755)
        write(fw / "runner.sh", '#!/bin/sh\nhermes\nexec "$HOME/.local/bin/thor-wake-proxy"\n', 0o755)
        manifest = {"tools": [{"id": "hermes", "bundled": False},
                               {"id": "thor-wake-proxy", "bundled": True,
                                "entrypoints": {"thor-wake-proxy": "framework/_tools/thor-wake-proxy/thor-wake-proxy"}}]}
        with self.assertRaisesRegex(RuntimeError, "thor-wake-proxy"):
            self.sk.assert_wiring(fw, [], home, manifest)
        write(fw / "_tools/thor-wake-proxy/thor-wake-proxy", "#!/bin/sh\n", 0o755)
        self.sk.assert_wiring(fw, [], home, manifest)
        with self.assertRaisesRegex(RuntimeError, "calls 'hermes'"):
            self.sk.assert_wiring(fw, [], home, {"tools": []})

    def test_exported_files_must_not_be_git_ignored(self):
        repo, fw = self.tmp / "repo", self.tmp / "stage" / "framework"
        repo.mkdir()
        self.assertEqual(subprocess.run(["git", "init", "-q", str(repo)]).returncode, 0)
        write(repo / ".gitignore", "*.jsonl\n")
        write(fw / "_tools" / "c" / "corpus.jsonl", "{}\n")
        write(fw / "_tools" / "c" / "run.py", "pass\n")
        with self.assertRaisesRegex(RuntimeError, "framework/_tools/c/corpus.jsonl"):   # the r1 clone gap: ignored = lost
            self.sk.assert_not_ignored(repo, fw)
        write(repo / ".gitignore", "*.jsonl\n!framework/_tools/c/corpus.jsonl\n")
        self.sk.assert_not_ignored(repo, fw)

if __name__ == "__main__":
    unittest.main(verbosity=1)
