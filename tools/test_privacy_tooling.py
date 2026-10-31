#!/usr/bin/env python3
"""Runnable check for the kit's privacy tooling: scrub.py --check, tools/githooks, and sync-kit's staging strips and
export assertions. Uses only fake rules and fake names.  Run: python3 tools/test_privacy_tooling.py"""
import getpass
import importlib.util
import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
GATE_DIR = REPO / "framework" / "_system" / "skills" / "guard" / "scripts"
FAKE_RULES = {
    "replacements": [{"find": "Acme Client", "replace": "a client", "isRegex": False, "caseSensitive": False}],
    "allowlist": [],
    "verifyDenylist": [{"pattern": "(?i)zorblax-secret-word", "label": "fake residue"}],
    "excludedTools": ["zorblax-tool"],
    "dropSectionTerms": ["Acme Client"],
}


def write(path, text, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode:
        path.chmod(mode)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kit-privacy-test-"))
        self.rules = write(self.tmp / "rules.json", json.dumps(FAKE_RULES))
        self.env = dict(os.environ, SCRUB_RULES=str(self.rules), GUARD_GATE_DIR=str(GATE_DIR))
        self.env.pop("GUARD_DENYLIST_FILE", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def check(self, *args, scrub=TOOLS / "scrub.py", env=None):
        return subprocess.run([sys.executable, str(scrub), "--check", *map(str, args)], env=env or self.env,
                              capture_output=True, text=True)


class ScrubCheck(Base):
    def test_clean_and_hits(self):
        clean = write(self.tmp / "d" / "clean.md", "Nothing private here.\n")
        self.assertEqual(self.check(clean).returncode, 0)
        bad = write(self.tmp / "d" / "sub" / "bad.svg", "<svg><text>line one</text>\n<text>Acme Client</text></svg>\n")
        out = self.check(self.tmp / "d")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("sub/bad.svg:2", out.stdout)
        self.assertIn("replacement rule #1", out.stdout)
        self.assertNotIn("Acme", out.stdout)                       # matched text only with --show
        self.assertIn("Acme", self.check(bad, "--show").stdout)

    def test_never_writes(self):
        f = write(self.tmp / "x.txt", "Acme Client and zorblax-secret-word\n")
        before = (f.read_bytes(), f.stat().st_mtime_ns)
        out = self.check(f)
        self.assertEqual(out.returncode, 1)
        self.assertIn("verify denylist [fake residue]", out.stdout)
        self.assertEqual(before, (f.read_bytes(), f.stat().st_mtime_ns))

    def test_username_binary_forbidden_path(self):
        write(self.tmp / "d" / "u.sh", f"cd /home/{getpass.getuser()}/x\n")
        (self.tmp / "d" / "b.dat").write_bytes(b"\x00\x01\x02")
        write(self.tmp / "d" / ".env", "A=1\n")
        out = self.check(self.tmp / "d").stdout
        self.assertIn("u.sh:1   real username", out)
        self.assertIn("b.dat   binary file", out)
        self.assertIn(".env   forbidden private path", out)

    def test_skips_git_dir_and_active_rules_file(self):
        write(self.tmp / "d" / ".git" / "x", "Acme Client\n")
        rules = write(self.tmp / "d" / "my-rules.json", json.dumps(FAKE_RULES))
        write(self.tmp / "d" / "ok.md", "fine\n")
        out = self.check(self.tmp / "d", env=dict(self.env, SCRUB_RULES=str(rules)))
        self.assertEqual(out.returncode, 0, out.stdout)

    def test_refuses_template_rules(self):
        lone = self.tmp / "lone"
        lone.mkdir()
        shutil.copy(TOOLS / "scrub.py", lone / "scrub.py")
        shutil.copy(TOOLS / "scrub-rules.json", lone / "scrub-rules.json")
        f = write(self.tmp / "f.md", "fine\n")
        env = {k: v for k, v in self.env.items() if k != "SCRUB_RULES"}
        out = self.check(f, scrub=lone / "scrub.py", env=env)
        self.assertEqual(out.returncode, 2)
        self.assertIn("template", out.stdout)
        self.assertEqual(self.check(f, "--template-ok", scrub=lone / "scrub.py", env=env).returncode, 0)
        self.assertEqual(self.check(f, env=dict(self.env, SCRUB_RULES=str(self.tmp / "missing.json"))).returncode, 2)

    def test_guard_denylist(self):
        deny = write(self.tmp / "deny.json", json.dumps({"denylist": [
            {"label": "fake person", "regex": r"\bQuuxperson\b"},
            {"label": "public handle", "regex": r"\bQuuxhandle\b"}]}))
        f = write(self.tmp / "g.md", "Quuxperson wrote this. Repo: github.com/Quuxhandle/kit\n")
        out = self.check(f, "--guard-denylist", deny, "--owner-repo", "Quuxhandle/kit")
        self.assertEqual(out.returncode, 1)
        self.assertIn("guard denylist [fake person]", out.stdout)
        self.assertNotIn("public handle", out.stdout)              # the repo owner's own handle is exempt
        self.assertEqual(self.check(f, "--guard-denylist", self.tmp / "nope.json").returncode, 2)
        gate = self.tmp / "gate"                                       # --check writes nothing, not even bytecode
        shutil.copytree(GATE_DIR, gate, ignore=shutil.ignore_patterns("__pycache__"))
        env = {k: v for k, v in self.env.items() if k != "PYTHONDONTWRITEBYTECODE"}
        out = self.check(f, "--guard-denylist", deny, env=dict(env, GUARD_GATE_DIR=str(gate)))
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertFalse((gate / "__pycache__").exists())

    def test_word_right_after_an_escape(self):
        # "\nName" in a string literal: \b sees no word boundary after the n, so the scrub misses the name there
        rules = dict(FAKE_RULES,
                     replacements=FAKE_RULES["replacements"] + [
                         {"find": r"\bQuuxcorp\b", "replace": "a company", "isRegex": True, "caseSensitive": True}],
                     verifyDenylist=FAKE_RULES["verifyDenylist"] + [{"pattern": r"\bZorbname\b", "label": "fake name"}])
        write(self.rules, json.dumps(rules))
        f = write(self.tmp / "e.py", 'MSG = "first line\\nQuuxcorp and\\tZorbname"\nOK = "a\\nb"\n')
        out = self.check(f)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("e.py:1   a scrub rule matches right after a \\n, \\t or \\r escape", out.stdout)
        self.assertIn("e.py:1   verify denylist [fake name]", out.stdout)
        self.assertNotIn("e.py:2", out.stdout)
        self.assertEqual(self.check(write(self.tmp / "ok.py", 'MSG = "a\\nb\\tc Quuxcorpus"\n')).returncode, 0)

    def test_login_name_checked_under_another_home(self):
        f = write(self.tmp / "h.sh", f"cd /home/{getpass.getuser()}/x\n")
        out = self.check(f, env=dict(self.env, HOME=str(self.tmp / "elsewhere")))
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("h.sh:1   real username", out.stdout)


class Hooks(Base):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        shutil.copytree(TOOLS / "githooks", self.repo / "tools" / "githooks")
        shutil.copy(TOOLS / "scrub.py", self.repo / "tools" / "scrub.py")
        shutil.copy(TOOLS / "check-commits.sh", self.repo / "tools" / "check-commits.sh")
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        for key in [k for k in self.env if k.startswith("GIT_") and k.endswith(("_NAME", "_EMAIL"))]:
            self.env.pop(key)
        self.git("init", "-q", "-b", "main")
        self.git("config", "core.hooksPath", "tools/githooks")
        self.git("config", "user.name", "Kit Test")
        self.git("config", "user.email", "1+kit-test@users.noreply.github.com")
        self.git("add", "tools")
        self.assertEqual(self.commit("init").returncode, 0)

    def git(self, *args, env=None):
        return subprocess.run(["git", *args], cwd=self.repo, env=env or self.env, capture_output=True, text=True)

    def commit(self, message, env=None):
        return self.git("commit", "-q", "-m", message, env=env)

    def head_count(self):
        return int(self.git("rev-list", "--count", "HEAD").stdout)

    def test_clean_commit_passes(self):
        write(self.repo / "a.md", "hello\n")
        self.git("add", "a.md")
        out = self.commit("add a")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_blocks_non_noreply_identity(self):
        write(self.repo / "a.md", "hello\n")
        self.git("add", "a.md")
        out = self.commit("add a", env=dict(self.env, GIT_AUTHOR_EMAIL="someone@example.com"))
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("AUTHOR email is not a GitHub noreply", out.stderr)
        out = self.commit("add a", env=dict(self.env, GIT_COMMITTER_EMAIL="someone@example.com"))
        self.assertIn("COMMITTER email", out.stderr)
        self.assertEqual(self.head_count(), 1)

    def test_scans_the_index_not_the_working_tree(self):
        f = write(self.repo / "a.md", "Acme Client\n")
        self.git("add", "a.md")
        write(f, "hello\n")                                         # working tree clean, index dirty
        out = self.commit("add a")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("a.md:1", out.stderr)
        self.git("add", "a.md")
        write(f, "Acme Client\n")                                   # index clean, working tree dirty
        self.assertEqual(self.commit("add a").returncode, 0)

    def test_blocks_private_rules_file_and_message(self):
        write(self.repo / "tools" / "scrub-rules.local.json", "{}\n")
        self.git("add", "-f", "tools/scrub-rules.local.json")
        out = self.commit("rules")
        self.assertIn("private rules file staged", out.stderr)
        self.git("rm", "-q", "--cached", "tools/scrub-rules.local.json")
        write(self.repo / "b.md", "fine\n")
        self.git("add", "b.md")
        out = self.commit("work for Acme Client")
        self.assertIn("[kit commit-msg] BLOCKED", out.stderr)
        self.assertEqual(self.head_count(), 1)

    def test_gitleaks_runs_on_staged_content(self):
        token = "sk_live_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
        write(self.repo / "c.py", f'STRIPE = "{token}"\n')
        self.git("add", "c.py")
        out = self.commit("add c")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("gitleaks found a secret", out.stderr)

    def test_guard_denylist_from_git_config(self):
        deny = write(self.tmp / "deny.json", json.dumps({"denylist": [{"label": "fake person", "regex": r"\bQuuxperson\b"}]}))
        self.git("config", "kit.guardDenylist", str(deny))
        write(self.repo / "d.md", "Quuxperson\n")
        self.git("add", "d.md")
        out = self.commit("add d")
        self.assertIn("guard denylist [fake person]", out.stderr)
        self.git("config", "kit.guardDenylist", str(self.tmp / "missing.json"))
        write(self.repo / "d.md", "fine\n")
        self.git("add", "d.md")
        self.assertNotEqual(self.commit("add d").returncode, 0)    # configured but missing: fail closed

    def test_pre_push_chains_to_global_hook(self):
        hooks = self.tmp / "global-hooks"
        marker = self.tmp / "chained"
        write(hooks / "pre-push", f'#!/bin/sh\necho "$1" > "{marker}"\nexit 3\n', 0o755)
        cfg = write(self.tmp / "gitconfig", f"[core]\n\thooksPath = {hooks}\n")
        hook = self.repo / "tools" / "githooks" / "pre-push"
        out = subprocess.run([str(hook), "origin", "url"], cwd=self.repo, input="", text=True,
                             env=dict(self.env, GIT_CONFIG_GLOBAL=str(cfg)), capture_output=True)
        self.assertEqual(out.returncode, 3)
        self.assertEqual(marker.read_text().strip(), "origin")
        out = subprocess.run([str(hook), "origin", "url"], cwd=self.repo, input="", text=True, env=self.env)
        self.assertEqual(out.returncode, 0)

    def unhooked_commit(self, message, env=None):
        """A commit the kit hooks never saw (another clone, a rebase, or rules that grew afterwards)."""
        return self.git("-c", f"core.hooksPath={self.tmp / 'no-hooks'}", "commit", "-q", "-m", message, env=env)

    def test_check_commits_rescans_metadata_messages_and_files(self):
        script = self.repo / "tools" / "check-commits.sh"
        base = self.git("rev-parse", "HEAD").stdout.strip()
        run = lambda *a: subprocess.run([str(script), *a], cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(run(f"{base}..HEAD").returncode, 0)        # nothing new
        write(self.repo / "a.md", "Acme Client\n")
        self.git("add", "a.md")
        self.unhooked_commit("fine", env=dict(self.env, GIT_AUTHOR_EMAIL="someone@example.com"))
        write(self.repo / "a.md", "hello\n")                          # a follow-up commit does not hide the old blob
        self.git("add", "a.md")
        self.unhooked_commit("about zorblax-secret-word")
        out = run(f"{base}..HEAD")
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn("author email is not a GitHub noreply address", out.stderr)
        self.assertNotIn("someone@example.com", out.stderr)            # the bad email is not echoed
        self.assertRegex(out.stderr, r"files/[0-9a-f]{12}/a\.md:1   scrub would rewrite")
        self.assertRegex(out.stderr, r"messages/[0-9a-f]{12}:1   verify denylist \[fake residue\]")
        self.assertEqual(run().returncode, 2)                          # usage

    def test_pre_push_rescans_only_commits_the_remote_lacks(self):
        remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
        self.git("remote", "add", "origin", str(remote))
        out = self.git("push", "-q", "origin", "main")
        self.assertEqual(out.returncode, 0, out.stderr)
        token = "sk_live_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
        write(self.repo / "c.py", f'STRIPE = "{token}"\n')
        self.git("add", "c.py")
        self.unhooked_commit("add c")
        out = self.git("push", "-q", "origin", "main")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("[kit pre-push] BLOCKED", out.stderr)
        self.git("reset", "-q", "--hard", "origin/main")               # rewrite the unpublished commit away
        write(self.repo / "d.md", "fine\n")
        self.git("add", "d.md")
        self.assertEqual(self.commit("add d").returncode, 0)
        rules = json.loads(self.rules.read_text())                     # rules grow: old pushed commits are not rescanned
        rules["verifyDenylist"].append({"pattern": "githooks", "label": "late rule"})
        self.rules.write_text(json.dumps(rules))
        out = self.git("push", "-q", "origin", "main")
        self.assertEqual(out.returncode, 0, out.stderr)


def load_sync_kit():
    spec = importlib.util.spec_from_file_location("sync_kit", TOOLS / "sync-kit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SyncKit(Base):
    def setUp(self):
        super().setUp()
        self._old = {k: os.environ.get(k) for k in ("SCRUB_RULES", "PATH")}
        os.environ["SCRUB_RULES"] = str(self.rules)
        os.environ["PATH"] = "/usr/bin:/bin"
        self.sk = load_sync_kit()

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def test_names_regex(self):
        r = self.sk.excluded_tools_re()
        for text in ("run zorblax-tool now", "zorblax_tool.py", "/x/zorblax-tool-watcher.sh"):
            self.assertTrue(r.search(text), text)
        self.assertFalse(r.search("myzorblax-tool"))

    def test_strip_dispatcher_entries(self):
        sh = ('#!/usr/bin/env bash\ncase "$1" in\n  A)\n    hooks=("/h/keep.sh"\n           "/h/zorblax-tool-watcher.sh"\n'
              '           "/h/also-keep.sh") ;;\n  B)\n    hooks=("/h/keep.sh"\n           "/h/zorblax-tool.sh") ;;\nesac\n'
              '# zorblax-tool note\n')
        out = self.sk.strip_shell_entries(sh, self.sk.excluded_tools_re())
        self.assertNotIn("zorblax", out)
        self.assertIn('"/h/also-keep.sh") ;;', out)
        self.assertIn(") ;;\nesac", out)
        f = write(self.tmp / "d.sh", out)
        self.assertEqual(subprocess.run(["bash", "-n", str(f)]).returncode, 0)

    def test_strip_markdown(self):
        md = ("# Rules\n\n## Talking\n- keep me (except for Acme Client work) please\n- drop zorblax-tool usage\n"
              "  continued line\n- keep two\n\n## Anything Named Acme Client\nsecret instructions\n### sub\nmore\n"
              "## Code\n```bash\n# zorblax-tool comment in code\necho hi\n```\n| a | zorblax-tool |\n| b | ok |\n")
        out = self.sk.strip_markdown(md, self.sk.drop_terms_re())
        self.assertIn("- keep me please\n", out)
        self.assertNotIn("continued line", out)
        self.assertIn("- keep two", out)
        self.assertNotIn("secret instructions", out)
        self.assertNotIn("more", out)
        self.assertIn("## Code\n```bash\n", out)                   # a '#' line in a fence is not a heading
        self.assertIn("| b | ok |", out)
        self.assertNotIn("| a |", out)

    def test_assert_no_excluded_tools(self):
        fw = self.tmp / "fw"
        write(fw / "ok.md", "fine\n")
        self.sk.assert_no_excluded_tools(fw)
        write(fw / "bin" / "x", "#!/bin/sh\nzorblax_tool --go\n")
        with self.assertRaisesRegex(RuntimeError, "bin/x:2"):
            self.sk.assert_no_excluded_tools(fw)

    def test_assert_wiring(self):
        home, fw = self.tmp / "home", self.tmp / "fw"
        tool = home / "AI_Projects" / "_tools" / "a"
        write(tool / "run.sh", "#!/bin/sh\n", 0o755)
        write(home / ".local" / "bin" / "wake-up", "#!/bin/sh\n", 0o755)
        write(home / ".local" / "bin" / "vendor-bin", "\x7fELF", 0o755)
        table = [(tool, "_tools/a", None)]
        write(fw / "_tools" / "b" / "wrapper", "#!/bin/sh\nexec $HOME/AI_Projects/_tools/a/run.sh\n"
              "$HOME/.local/bin/vendor-bin\n", 0o755)
        with self.assertRaisesRegex(RuntimeError, "run.sh, which is not in the export"):
            self.sk.assert_wiring(fw, table, home)
        write(fw / "_tools" / "a" / "run.sh", "#!/bin/sh\n", 0o755)
        self.sk.assert_wiring(fw, table, home)
        write(fw / "units" / "job.service", "[Service]\nSlice=agents.slice\nExecStart=%h/AI_Projects/_tools/a/run.sh\n")
        with self.assertRaisesRegex(RuntimeError, "agents.slice"):
            self.sk.assert_wiring(fw, table, home)
        write(fw / "units" / "agents.slice", "[Slice]\n")
        write(fw / "_tools" / "b" / "go.sh", "#!/usr/bin/env bash\necho run wake-up first\nwake-up || exit 1\n", 0o755)
        with self.assertRaisesRegex(RuntimeError, "calls 'wake-up'"):
            self.sk.assert_wiring(fw, table, home)
        write(fw / "harness" / "bin" / "wake-up", "#!/bin/sh\n", 0o755)
        self.sk.assert_wiring(fw, table, home)
        write(home / ".claude" / "hooks" / "plain.sh", "echo hi\n", 0o644)  # run as `bash plain.sh`: no +x needed
        write(fw / "harness" / "settings.reference.json", '{"command": "bash $HOME/.claude/hooks/plain.sh"}\n')
        with self.assertRaisesRegex(RuntimeError, "plain.sh, which is not in the export"):
            self.sk.assert_wiring(fw, table, home)


if __name__ == "__main__":
    unittest.main(verbosity=1)
