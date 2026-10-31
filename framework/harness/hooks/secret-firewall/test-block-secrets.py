#!/usr/bin/env python3
"""Regression coverage for the secret-firewall Bash screening path."""
import json
import os
import subprocess
import sys
import tempfile


HOOK = __import__('os').path.expanduser("~/.claude/hooks/secret-firewall/block-secrets.py")
# Test runs must not land in the live hook event log, where this suite and the dispatcher's fail-closed check
# (session_id "unknown") would otherwise make up most of the denials.
ENV = {**os.environ, "HOOK_EVENT_LOG": os.devnull}

MANDATORY_CASES = [
    ("MUST DENY: cat Claude credentials", "cat ~/.claude/.credentials.json", True),
    (
        "MUST DENY: copy service-account JSON",
        "cp ./service-account-prod.json /tmp/service-account-backup.json",
        True,
    ),
    ("MUST DENY: read ~/.secrets value", "cat ~/.secrets/provider.json", True),
    ("MUST DENY: read a .env value", "grep '^API_KEY=' .env", True),
    ("MUST DENY: cat CLIProxy client key", "cat ~/.cliproxy-client.key", True),
    ("MUST ALLOW: cat SSH public key", "cat ~/.ssh/id_ed25519.pub", False),
    (
        "MUST ALLOW: heredoc documents a key path",
        "cat > /tmp/key-paths.md <<'EOF'\n"
        "The client key path is ~/.cliproxy-client.key; do not paste its value.\n"
        "EOF",
        False,
    ),
    (
        "MUST ALLOW: generic recursive JSON grep",
        "grep -r --include=*.json 'display_name' /tmp/public-docs",
        False,
    ),
    (
        "MUST ALLOW: comment mentions .env",
        "true # .env is loaded by the application at runtime",
        False,
    ),
]

GENERIC_CREDENTIAL_CASES = [
    # Plaintext password/token files with no fixed extension.
    ("MUST DENY: cat media-server password file", "cat ~/.config/app/media-password", True),
    ("MUST DENY: cat bare credentials file", "cat ~/.config/windows/credentials", True),
    ("MUST DENY: cat bare -token file", "cat ~/.config/se/telegram-bot-token", True),
    ("MUST DENY: cat Google client-secret JSON", "cat ~/Downloads/client_secret_123.apps.googleusercontent.com.json", True),
    ("MUST DENY: cat .secret extension", "cat ~/.config/side-app/pairing.secret", True),
    (
        "MUST ALLOW: cat the firewall's own script (no self-block)",
        "cat ~/.claude/hooks/secret-firewall/block-secrets.py",
        False,
    ),
    (
        "MUST ALLOW: grep mentioning secret-firewall by name",
        "grep -r 'secret-firewall' /tmp/notes.md",
        False,
    ),
    (
        "MUST ALLOW: unrelated word containing 'token' with no delimiter",
        "cat ~/.config/chromium/TrustTokenKeyCommitments",
        False,
    ),
]

LEGACY_CASES = [
    ("real access: cat secrets file", "cat ~/.secrets/foo.json", True),
    ("real access: list secrets directory", "ls -la ~/.secrets", True),
    ("real access: cat project .env", "cat .env", True),
    ("real access: append into secrets directory", "echo 'x' >> ~/.secrets/notes", True),
    ("real access: cat Codex auth", "cat ~/.codex/auth.json", True),
    ("real access: dump environment pipeline", "printenv | grep KEY", True),
    (
        "doc-only heredoc mention",
        "cat > /tmp/x.md <<'EOF'\n"
        "The secrets-directory path is ~/.secrets/ and the Codex auth-file path is "
        "~/.codex/auth.json.\n"
        "EOF",
        False,
    ),
    ("doc-only .gitignore pattern", "printf '.env*' > .gitignore", False),
    # 2026-09-04: deny-list retirement (permissions.deny -> []) moved secret
    # protection to hook-only. These four patterns existed only in the old
    # Read()/Edit() deny rules, not in block-secrets.py, until backfilled
    # into is_protected_path + SECRET_FILE_HINT alongside the retirement.
    ("real access: cat .credentials.json", "cat ~/.credentials.json", True),
    ("real access: cat token.json", "cat token.json", True),
    ("real access: cat an apikey file", "cat myapikey.json", True),
    ("real access: cat a service-account file", "cat service-account-1.json", True),
]

CASES = MANDATORY_CASES + GENERIC_CREDENTIAL_CASES + LEGACY_CASES


def is_blocked(stdout):
    if not stdout.strip():
        return False
    response = json.loads(stdout)
    return (
        response.get("decision") == "block"
        and response.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    )


def run_case(label, command, expected_blocked):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    result = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=ENV,
    )
    try:
        assert result.returncode == 0, f"exit {result.returncode}: {result.stderr.strip()}"
        actual_blocked = is_blocked(result.stdout)
        assert actual_blocked == expected_blocked, (
            f"expected blocked={expected_blocked}, got blocked={actual_blocked}; "
            f"stdout={result.stdout!r}; stderr={result.stderr!r}"
        )
    except (AssertionError, json.JSONDecodeError) as exc:
        print(f"FAIL: {label} -- {exc}")
        return False
    print(f"PASS: {label}")
    return True


def fail_closed_case(label, stdin, hook=HOOK, error=None):
    """A payload the hook cannot parse, or a bug inside it, must deny (exit 0 + deny JSON), never allow."""
    with tempfile.TemporaryDirectory() as td:
        log = os.path.join(td, "events.jsonl")
        result = subprocess.run([sys.executable, hook], input=stdin, capture_output=True, check=False,
                                env={**os.environ, "HOOK_EVENT_LOG": log})
        try:
            assert result.returncode == 0, f"exit {result.returncode}: {result.stderr[-300:]!r}"
            assert is_blocked(result.stdout.decode()), f"not denied: stdout={result.stdout!r}"
            reason = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
            assert "could not be screened" in reason and (error is None or error in reason), reason
            assert "deny:could not be screened" in open(log).read(), "deny not logged to HOOK_EVENT_LOG"
        except (AssertionError, ValueError, KeyError, OSError) as exc:
            print(f"FAIL: {label} -- {exc}")
            return False
    print(f"PASS: {label}")
    return True


def fail_closed_cases():
    """Regression shapes from a weekly review of hook errors."""
    passed = [
        # JSONDecodeError: dispatch tests/run.sh feeds 'not json' to the live dispatcher on purpose
        fail_closed_case("fail-closed: non-JSON stdin (JSONDecodeError)", b"not json", error="JSONDecodeError"),
        fail_closed_case("fail-closed: empty stdin", b""),
        fail_closed_case("fail-closed: truncated JSON", b'{"tool_name": "Bash", "tool_input": {"command": "cat ~/.e'),
        fail_closed_case("fail-closed: JSON that is not an object", b'["Bash"]', error="ValueError"),
        fail_closed_case("fail-closed: invalid UTF-8 bytes", b"\xff\xfe{"),
    ]
    # Regression: an in-place edit once called a function before defining it, so every call
    # from every session hit a NameError. Same path covers the old hook's fail-open-exception: any bug must deny.
    src = open(HOOK, encoding="utf-8").read()
    assert "fail-open" not in src.lower(), "block-secrets.py has a fail-open path"
    marker = '\nif __name__ == "__main__":'
    assert src.count(marker) == 1
    with tempfile.TemporaryDirectory() as td:
        os.symlink(os.path.join(os.path.dirname(os.path.dirname(HOOK)), "hook_event.py"), os.path.join(td, "hook_event.py"))
        os.mkdir(os.path.join(td, "secret-firewall"))  # same layout as the live hook, so its event log import works
        broken = os.path.join(td, "secret-firewall", "block-secrets.py")
        with open(broken, "w", encoding="utf-8") as f:
            f.write(src.replace(marker, "\ndef screen_bash(cmd, cwd=''):\n    return resolve_vars_undefined(cmd)\n" + marker))
        passed.append(fail_closed_case("fail-closed: hook bug (NameError)", json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "echo hello"}}).encode(), hook=broken, error="NameError"))
    return passed


def main():
    passed = [run_case(*case) for case in CASES] + fail_closed_cases()
    if all(passed):
        print(f"PASS: {len(passed)}/{len(passed)} regression cases")
        return 0
    print(f"FAIL: {sum(passed)}/{len(passed)} regression cases")
    return 1


if __name__ == "__main__":
    sys.exit(main())
