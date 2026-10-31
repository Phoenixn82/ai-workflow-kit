import hashlib, json, re, sys, tomllib

EVENT_KEYS = {"PreToolUse":"pre_tool_use","PermissionRequest":"permission_request","PostToolUse":"post_tool_use",
 "PreCompact":"pre_compact","PostCompact":"post_compact","SessionStart":"session_start","SessionEnd":"session_end",
 "UserPromptSubmit":"user_prompt_submit","SubagentStart":"subagent_start","SubagentStop":"subagent_stop",
 "Stop":"stop","Interrupt":"interrupt"}
NO_MATCHER = {"UserPromptSubmit","Stop","Interrupt"}   # matcher_pattern_for_event -> None
DEFAULT_ACL = 2500                                     # DEFAULT_HOOK_OUTPUT_TOKEN_LIMIT (verify)

def codex_hook_hash(event, matcher, h):
    # normalized command handler (discovery.rs append_matcher_groups)
    timeout = h.get("timeout")
    if event in ("SessionEnd", "Interrupt"):
        timeout = min(max(timeout if timeout is not None else 1, 1), 3)
    else:
        timeout = max(timeout if timeout is not None else 600, 1)
    norm = {"type": "command", "command": h["command"], "timeout": timeout, "async": bool(h.get("async", False))}
    if h.get("statusMessage") is not None:
        norm["statusMessage"] = h["statusMessage"]
    acl = h.get("additionalContextLimit")
    if event in ("PreToolUse","PostToolUse","SessionStart","UserPromptSubmit","SubagentStart") and acl is not None and acl != DEFAULT_ACL:
        norm["additionalContextLimit"] = acl
    ident = {"event_name": EVENT_KEYS[event], "hooks": [norm]}
    m = None if event in NO_MATCHER else matcher
    if m is not None:                       # toml serializer drops None fields
        ident["matcher"] = m
    blob = json.dumps(ident, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()

hooks_path = sys.argv[1]; cfg_path = sys.argv[2]
hooks = json.load(open(hooks_path))["hooks"]
state = tomllib.load(open(cfg_path, "rb")).get("hooks", {}).get("state", {})
for event, groups in hooks.items():
    for gi, g in enumerate(groups):
        for hi, h in enumerate(g.get("hooks", [])):
            key = f"{hooks_path}:{EVENT_KEYS[event]}:{gi}:{hi}"
            got = codex_hook_hash(event, g.get("matcher"), h)
            stored = state.get(key, {}).get("trusted_hash")
            print(("MATCH " if got == stored else "DIFF  ") + key, got, stored or "-")
