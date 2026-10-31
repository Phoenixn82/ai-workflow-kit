#!/usr/bin/env python3
"""Hermes pre_tool_call guard (terminal): every Claude/Codex dispatch goes through a route, same rule as Claude Code's
~/.claude/hooks/route-guard.py. Blocks `claude -p` with no --model, a Sonnet model (lanes.json not_routed), and bare
`codex exec` (use `cx <class> ...`, which validates the class itself). Exit 2 = block; anything else lets the call run.
The user, 2026-10-19: Hermes must know the routing system and not be talked out of it.
"""
import json
import re
import shlex
import sys

HOW = "Run `route list`, pick the task class, then run exactly the command `route <class>` prints."
# Wrappers that run the command after them, with the options of each that take the next word as their value.
WRAPPERS = {"env": "uCSa", "nohup": "", "nice": "n", "timeout": "ks", "stdbuf": "ioe", "exec": "a", "command": "",
            "time": "fo", "ionice": "cnp", "setsid": "", "chrt": "", "sudo": "ugCDhpUrRT"}
LONG_VALUE = {"--unset", "--chdir", "--split-string", "--argv0", "--adjustment", "--signal", "--kill-after", "--input", "--output",
              "--error", "--format", "--class", "--classdata", "--user", "--group", "--host", "--prompt", "--other-user",
              "--role", "--type", "--command-timeout"}


def split_string(value):
    """env -S: its escapes \\_ \\t \\n \\v \\f \\r separate words like a space does."""
    value = re.sub(r"\\[_tnvfr]", " ", value.split("\\c", 1)[0])  # \c ends the string
    try:
        return shlex.split(value)
    except ValueError:
        return value.split()


def unwrap(words):
    """The command after leading VAR=value assignments and wrappers such as env -i, timeout 60, nice -n 10, sudo -n."""
    while words:
        if re.fullmatch(r"\w+=.*", words[0]):
            words = words[1:]
            continue
        exe = words[0].rsplit("/", 1)[-1]
        if exe not in WRAPPERS:
            return words
        takes, words = WRAPPERS[exe], words[1:]
        while words and words[0].startswith("-") and words[0] != "--":
            opt = words.pop(0)
            if opt.startswith("--"):
                name, eq, value = opt.partition("=")
                if name in LONG_VALUE and not eq and words:
                    value = words.pop(0)
                if exe == "env" and name == "--split-string":
                    words = split_string(value) + words
                continue
            for i, letter in enumerate(opt[1:], 1):  # getopt cluster: -nu root, -n10, -i
                if letter in takes:
                    value = opt[i + 1:] or (words.pop(0) if words else "")
                    if exe == "env" and letter == "S":
                        words = split_string(value) + words
                    break
        if words and words[0] == "--":
            words = words[1:]
        if exe in ("timeout", "chrt") and words and re.fullmatch(r"[\d.]+[smhd]?", words[0]):
            words = words[1:]
    return words


def problem(command):
    # ponytail: splits chained commands on ; & | and newlines; a quoted separator inside a prompt can mis-split
    for seg in re.split(r"[;&|\n]+", command or ""):
        try:
            words = shlex.split(seg)
        except ValueError:
            words = seg.split()
        words = unwrap(words)
        if not words:
            continue
        exe = words[0].rsplit("/", 1)[-1]
        if exe == "claude" and any(w in ("-p", "--print") or w.startswith("--print=") for w in words):
            model = next((words[i + 1] for i, w in enumerate(words[:-1]) if w == "--model"), None) or next(
                (w.split("=", 1)[1] for w in words if w.startswith("--model=")), None)
            if not model:
                return f"route-guard: `claude -p` names no --model. {HOW}"
            if "sonnet" in model.lower():
                return f"route-guard: no route uses {model} (lanes.json not_routed). {HOW}"
        if exe == "codex" and any(w in ("exec", "e") for w in words[1:]):  # also after global options (-c k=v exec)
            return f"route-guard: dispatch Codex through `cx <class> <sandbox> <outfile> <prompt>`, not bare codex exec. {HOW}"
    return None


def main():
    data = json.load(sys.stdin)
    why = problem((data.get("tool_input") or {}).get("command", ""))
    if why:
        print(json.dumps({"decision": "block", "reason": why}))
        sys.exit(2)


def self_test():
    assert problem("claude -p 'hi'")
    assert problem("cd x && CLAUDE_SPAWN=1 claude --print hi")
    assert problem("claude -p --model claude-sonnet-5-5 hi")
    assert problem("codex exec 'do it'")
    assert problem("claude -p --model claude-haiku-5-5 --effort medium hi") is None
    assert problem("claude -p --model=claude-opus-5-5 hi") is None
    assert problem("cx feature-build workspace-write /tmp/o 'x'") is None
    assert problem("route list; lane doctor") is None
    assert problem("claude --version") is None and problem("ls -la") is None
    for wrapped in ("env claude -p hi", "env -i FOO=1 claude -p hi", "timeout 60 codex exec hi", "nice -n 10 claude -p hi",
                    "timeout -k 5 10m claude -p hi", "codex -c model=x exec hi", "nohup /usr/bin/claude --print hi",
                    "env -i codex exec hi", "sudo -n claude -p hi", "sudo -nu root codex exec hi", "nice -n10 claude -p hi",
                    "env -S 'codex exec' hi", "env -S 'codex\\_exec hi'", "env -S 'codex exec\\c ignored'","env -a x codex exec hi",
                    "env --argv0 x claude -p hi", "sudo -u root -- claude -p hi", "stdbuf -o0 claude -p hi"):
        assert problem(wrapped), wrapped
    assert problem("timeout 5m claude -p --model claude-haiku-5-5 hi") is None
    print("route-guard self-test passed")


if __name__ == "__main__":
    self_test() if sys.argv[1:] == ["--self-test"] else main()
