#!/usr/bin/env python3
"""PreToolUse(Bash) guard: no local model servers/runners on odin.

odin is the cockpit laptop; a thin laptop overheats under a local model server. Local model work runs on thor (Ollama on thor:11434, or `ssh thor ...`).
Denies only when the model command is what actually runs (segment start, after wrappers like
nohup/setsid/env/sudo), so `pgrep -af llama-server` or `ssh thor 'ollama serve'` pass.
Override for one command: put ODIN_MODEL_OK=1 in it.
"""
import json
import re
import socket
import sys

MODEL = re.compile(r"^(?:\S*/)?(?:ollama\s+(?:serve|run)\b|llama-(?:server|cli)\b|vllm\s+serve\b)")
WRAPPERS = re.compile(r"^(?:(?:nohup|setsid|exec|sudo|env|nice|ionice|time)\s+|timeout\s+\S+\s+|-\S+\s+|\w+=\S*\s+)+")


def offending(cmd):
    if "ODIN_MODEL_OK=1" in cmd:
        return None
    for seg in re.split(r"[;&|\n()`]|\$\(", cmd):
        seg = seg.strip()
        if re.match(r"(?:\S*/)?ssh\b", seg):
            continue  # the rest of this segment runs on another host
        rest = WRAPPERS.sub("", seg)
        if MODEL.match(rest) and not re.search(r"OLLAMA_HOST=\S*(thor)", seg):
            return rest.split()[0] + " " + (rest.split()[1] if len(rest.split()) > 1 else "")
    return None


def main():
    if socket.gethostname().split(".")[0].lower() != "odin":
        return
    data = json.load(sys.stdin)
    hit = offending((data.get("tool_input") or {}).get("command", ""))
    if hit:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"`{hit.strip()}` would run a local model on odin, a thin laptop that overheats "
                "under that load. Run it on thor instead: `ssh thor '...'`, or point the client "
                "at OLLAMA_HOST=thor:11434. Override for one command only if the user asked: "
                "add ODIN_MODEL_OK=1."),
        }}))


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        bad = ["ollama serve", "nohup ollama/bin/ollama serve > x 2>&1 &", "cd x && OLLAMA_HOST=127.0.0.1:11435 ./ollama serve",
               "setsid ~/.cache/p/ollama/lib/ollama/llama-server --model m", "env A=1 vllm serve m", "echo hi; ollama run qwen3:8b"]
        good = ["pgrep -af llama-server", "ssh thor 'ollama serve'", "ollama pull qwen3:8b", "ollama list",
                "OLLAMA_HOST=thor:11434 ollama run qwen3:8b", "grep -rn 'ollama serve' .",
                "ODIN_MODEL_OK=1 ollama run x", "journalctl -u ollama"]
        assert all(offending(c) for c in bad), [c for c in bad if not offending(c)]
        assert not any(offending(c) for c in good), [c for c in good if offending(c)]
        print("self-test passed")
    else:
        main()
