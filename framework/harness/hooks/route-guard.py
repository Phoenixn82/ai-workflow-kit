#!/usr/bin/env python3
"""PreToolUse(Agent|Workflow) guard: every dispatch names its model, so nothing silently falls back to Opus.

The user, 2026-10-13: routing is fluid (`route <class>` picks the cheapest lane that passes, and shifts to Codex when
Claude runs ahead of its weekly pace); there is no CLAUDE_CODE_SUBAGENT_MODEL default to catch a forgotten model.
Denies an Agent call with no `model` (forks are exempt: they always run on the parent's model) and a Workflow script
with an agent() call whose own arguments never mention `model`. An Agent call must also name its effort, and xhigh/max
only with an xhigh route class (hard-engineering, verify-judge, the Opus second reviewer) in its description. Also denies a model whose family no route uses
(Sonnet: lanes.json not_routed; 1,384 Sonnet subagent turns slipped through 2026-10-12 via superpowers' "cheap model"
advice). A cost guard, not a safety hook: on a deny, run `route <class>`, add the model, and retry. A model the user
named themselves passes with "you-pick" in the Agent description (or a comment in the Workflow script).
"""
import json
import os
import re
import sys

WORKFLOW_DIRS = (os.path.expanduser("~/AI_Projects/.claude/workflows"), os.path.expanduser("~/.claude/workflows"))
LANES = os.environ.get("ROUTE_LANES") or os.path.expanduser("~/AI_Projects/_system/lanes.json")
HOW = "run `route list`, pick the task class, and use the model (and effort) `route <class>` prints"


def code_only(src):
    """src with string, template-text and comment characters blanked (newlines kept); ${...} stays code."""
    out, stack, i, n = [], [], 0, len(src)  # stack: "`" = in template text, "{" = in a ${...} or a brace inside one
    blank = lambda t: re.sub(r"[^\n]", " ", t)  # noqa: E731
    while i < n:
        c = src[i]
        if stack and stack[-1] == "`":
            if c == "\\":
                out.append(blank(src[i:i + 2])); i += 2
            elif c == "`":
                stack.pop(); out.append(c); i += 1
            elif src.startswith("${", i):
                stack.append("{"); out.append("  "); i += 2
            else:
                out.append(blank(c)); i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i); j = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2); j = n if j < 0 else j + 2
        elif c in "'\"":
            j = i + 1
            while j < n and src[j] not in (c, "\n"):
                j += 2 if src[j] == "\\" else 1
            j = min(j + 1, n)
            if src[i + 1:j - 1] == "model":  # a quoted property key: {"model": "haiku"}
                out.append(src[i:j]); i = j
                continue
        else:
            if c == "`":
                stack.append("`")
            elif c == "{" and stack:
                stack.append("{")
            elif c == "}" and stack:
                stack.pop()
                if stack and stack[-1] == "`":
                    c = " "  # the closing brace of ${...} belongs to the template
            out.append(c); i += 1
            continue
        out.append(blank(src[i:j])); i = j
    # ponytail: regex literals are not recognised; a quote inside one could mis-blank the rest of the line
    return "".join(out)


def unnamed_agent_calls(src):
    """1-based line numbers of agent(...) calls whose argument text has no `model` and no spread of a route table
    (`...ROUTES['bulk-sweep']`, the pattern open-loops.js uses)."""
    code, bad = code_only(src), []
    for m in re.finditer(r"(?<![\w$.])agent\s*\(", code):
        depth, j = 0, m.end() - 1
        while j < len(code):
            depth += {"(": 1, ")": -1}.get(code[j], 0)
            if depth == 0:
                break
            j += 1
        if not re.search(r"(?<![\w$.])[\"']?model[\"']?\s*:|[{,]\s*model\s*[,}]|\.\.\.\s*[\w$]*route", code[m.end():j], re.I):
            bad.append(code.count("\n", 0, m.start()) + 1)
    return bad


def workflow_source(ti, cwd):
    if ti.get("script"):
        return ti["script"]
    path = ti.get("scriptPath")
    if not path and ti.get("name"):
        dirs = (os.path.join(cwd or "", ".claude", "workflows"),) + WORKFLOW_DIRS
        path = next((p for d in dirs for p in [os.path.join(d, ti["name"] + ".js")] if os.path.isfile(p)), None)
    try:
        return open(path, encoding="utf-8").read() if path else ""  # a built-in named workflow: nothing to check
    except OSError:
        return ""


def routed_families(path=LANES):
    """{'opus', 'haiku', 'fable'}: families of the Claude models some route's lane uses; None if lanes.json is unreadable."""
    try:
        L = json.load(open(path))
        return {L["lanes"][r["lane"]]["model"].split("-")[1] for k, r in L["routes"].items()
                if not k.startswith("_") and r["lane"].startswith("claude.")}
    except (OSError, ValueError, KeyError, IndexError, AttributeError):
        return None  # fail open: a cost guard never blocks on its own config error


def xhigh_classes(path=LANES):
    """Route classes that may dispatch at xhigh/max: a Claude lane at xhigh (hard-engineering, verify-judge) or a route
    whose use names the Opus xhigh second reviewer (code-review, plan-review, security-review). None if unreadable."""
    try:
        L = json.load(open(path))
        return {k for k, r in L["routes"].items() if not k.startswith("_") and (
            (r["lane"].startswith("claude.") and L["lanes"][r["lane"]].get("effort") in ("xhigh", "max"))
            or "Opus xhigh" in r.get("use", ""))}
    except (OSError, ValueError, KeyError, AttributeError):
        return None  # fail open


def unrouted(models, fams):
    """Model names (alias or full id) whose Claude family no route uses."""
    fam = lambda m: (m.split("-")[1] if m.startswith("claude-") else m).lower()  # noqa: E731
    return sorted({m for m in models if fams and fam(m) not in fams and fam(m) in ("sonnet", "opus", "haiku", "fable")})


def reason(data, fams=None, classes=None):
    tool, ti = data.get("tool_name"), data.get("tool_input") or {}
    fams = routed_families() if fams is None else fams
    desc = ti.get("description") or ""
    if tool == "Agent" and ti.get("subagent_type") != "fork" and not ti.get("model"):
        return f"route-guard (cost guard, not a safety hook): this Agent call names no model. {HOW}, then retry."
    if tool == "Agent" and "you-pick" not in desc and unrouted([ti.get("model") or ""], fams):
        return (f"route-guard (cost guard, not a safety hook): no route uses {ti['model']} (lanes.json not_routed). {HOW}, "
                "then retry. If the user named this model themselves, put you-pick in the description.")
    # Effort too (weekly review 2026-10-25 #9): an Agent call with no effort inherits the session's, xhigh whenever
    # the user's session runs xhigh, and xhigh dispatches outside the xhigh routes ran ~200 Opus turns that week.
    if tool == "Agent" and ti.get("subagent_type") != "fork" and "you-pick" not in desc:
        if not ti.get("effort"):
            return (f"route-guard (cost guard, not a safety hook): this Agent call names no effort, so it inherits the "
                    f"session's. {HOW}, then retry.")
        ok = xhigh_classes() if classes is None else classes
        if ti["effort"] in ("xhigh", "max") and ok and not any(re.search(rf"\b{re.escape(c)}\b", desc) for c in ok):
            return (f"route-guard (cost guard, not a safety hook): effort {ti['effort']} is routed only for "
                    f"{', '.join(sorted(ok))}. Name that class in the description, or {HOW}, then retry.")
    if tool == "Workflow":
        src = workflow_source(ti, data.get("cwd"))
        off = [] if "you-pick" in src else unrouted(re.findall(r"""model["']?\s*:\s*["'`]([\w.-]+)""", src), fams)
        if off:
            return f"route-guard (cost guard, not a safety hook): no route uses {', '.join(off)} (lanes.json not_routed). {HOW}, then retry."
        bad = unnamed_agent_calls(src)
        if bad:
            return (f"route-guard (cost guard, not a safety hook): agent() call(s) on script line(s) {', '.join(map(str, bad))} "
                    f"name no model. Put model: (and effort:), or a spread of a route table like ...ROUTES['bulk-sweep'], in each agent() call's own options; {HOW}. Then retry.")
    return None


def main():
    why = reason(json.load(sys.stdin))
    if why:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": why}}))


def self_test():
    ok = "await agent(`find ${x.key} (see agent( docs)`, { label: 'f', model: 'haiku', effort: 'medium' })"
    assert unnamed_agent_calls(ok) == []
    assert unnamed_agent_calls("const a = 1\nawait agent('p', {label: 'x'})") == [2]
    assert unnamed_agent_calls("agent(`a ${f(1)} b ${`nested ${y}`} c`, {model: m})\nagent(p)") == [2]
    assert unnamed_agent_calls("// agent(p)\n/* agent(q) */ s = 'agent(r)'; t = `agent(u)`") == []  # not calls
    assert unnamed_agent_calls("myagent(p); x.agent(q)") == []
    assert unnamed_agent_calls("agent(prompt, {...opts, model})") == []
    assert unnamed_agent_calls("agent('p', {\"model\": \"haiku\"})") == []
    assert unnamed_agent_calls("agent(model.prompt, {label: 'x'})") == [1]  # a variable named model is not the option
    # ponytail: an inline nested object with its own model: key (e.g. an inline schema) still passes; schemas are consts here
    assert unnamed_agent_calls("agent(`p`, {label: 'x', ...ROUTES['bulk-sweep']})") == []
    assert unnamed_agent_calls("agent(`p`, {label: 'x', ...opts})") == [1]
    assert unnamed_agent_calls("agent(`the model is good`, {label: 'x'})") == [1]  # prompt text never counts
    a = lambda **ti: reason({"tool_name": "Agent", "tool_input": ti})  # noqa: E731
    assert a(prompt="p") and "names no model" in a(prompt="p")
    assert a(prompt="p", model="haiku", effort="medium") is None and a(prompt="p", subagent_type="fork") is None
    assert "names no effort" in a(prompt="p", model="haiku")
    assert a(prompt="p", model="haiku", description="x (you-pick)") is None
    xc = {"hard-engineering", "verify-judge", "code-review"}
    e = lambda d, eff="xhigh": reason({"tool_name": "Agent", "tool_input": {"prompt": "p", "model": "opus", "effort": eff, "description": d}}, {"opus"}, xc)  # noqa: E731
    assert e("Hold Task 7B (hard-engineering)") is None and e("Review Task 3 (code-review, Opus xhigh)") is None
    assert "routed only for" in e("thor media move") and "routed only for" in e("x", "max")
    assert e("thor media move", "high") is None and e("x (you-pick)") is None
    assert reason({"tool_name": "Agent", "tool_input": {"prompt": "p", "model": "opus", "effort": "xhigh"}}, {"opus"}, set()) is None  # unreadable: open
    live = xhigh_classes()
    assert live is None or {"hard-engineering", "verify-judge"} <= live, live
    w = lambda **ti: reason({"tool_name": "Workflow", "tool_input": ti})  # noqa: E731
    assert w(script="export const meta = {}\nawait agent('p')") and w(script=ok) is None
    assert w(name="no-such-saved-workflow") is None
    assert reason({"tool_name": "Bash", "tool_input": {"command": "ls"}}) is None
    fams = {"opus", "haiku", "fable"}
    for m in ("sonnet", "claude-sonnet-5-5", "claude-sonnet-5"):
        assert "no route uses" in reason({"tool_name": "Agent", "tool_input": {"prompt": "p", "model": m}}, fams), m
    for m in ("opus", "haiku", "claude-opus-5-5", "claude-fable-5-1", "gpt-6.1-sol"):
        assert reason({"tool_name": "Agent", "tool_input": {"prompt": "p", "model": m, "effort": "high"}}, fams) is None, m
    assert reason({"tool_name": "Agent", "tool_input": {"prompt": "p", "model": "sonnet", "description": "x (you-pick)"}}, fams) is None
    assert "no route uses sonnet" in reason({"tool_name": "Workflow", "tool_input": {"script": "agent('p', {model: 'sonnet'})"}}, fams)
    assert reason({"tool_name": "Workflow", "tool_input": {"script": "agent('p', {model: 'haiku'})"}}, fams) is None
    assert reason({"tool_name": "Agent", "tool_input": {"prompt": "p", "model": "sonnet", "effort": "high"}}, set()) is None  # unreadable lanes: open
    assert routed_families() is None or {"opus", "haiku"} <= routed_families()  # live lanes.json parses
    for d in WORKFLOW_DIRS:  # saved workflows on disk must pass today
        for f in (os.listdir(d) if os.path.isdir(d) else []):
            if f.endswith(".js"):
                assert unnamed_agent_calls(open(os.path.join(d, f)).read()) == [], f
    print("route-guard self-test passed")


if __name__ == "__main__":
    self_test() if sys.argv[1:] == ["--self-test"] else main()
