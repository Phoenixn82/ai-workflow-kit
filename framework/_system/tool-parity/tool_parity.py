#!/usr/bin/env python3
"""Audit Claude/Codex tool parity and check named tool requests.

This intentionally prefers existing local capabilities over broad installs.
It is a routing guard, not a package manager.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None


HOME = Path.home()
ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = TOOL_ROOT / "registry.json"
CLAUDE_HOME = HOME / ".claude"
CODEX_HOME = HOME / ".codex"
CLAUDE_MCP = CLAUDE_HOME / "mcp.json"
CLAUDE_USER_CONFIG = HOME / ".claude.json"  # Claude Code on Linux keeps user-scope mcpServers here
CODEX_CONFIG = CODEX_HOME / "config.toml"
CODEX_PLUGIN_CACHE = CODEX_HOME / "plugins" / "cache"  # <marketplace>/<plugin>; holds remote installs too
CANONICAL_SKILLS = ROOT / "_system" / "skills"


@dataclass
class RuntimeState:
    claude_skills: set[str]
    codex_skills: set[str]
    canonical_skills: set[str]
    codex_local_skills: set[str]
    claude_mcps: set[str]
    codex_mcps: set[str]
    codex_plugins: set[str]
    commands: dict[str, str | None]
    npx_packages: dict[str, bool]


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude/Codex tool parity checker")
    sub = parser.add_subparsers(dest="cmd", required=True)

    audit = sub.add_parser("audit", help="Audit all registry tools")
    audit.add_argument("--write", action="store_true", help="Write markdown/json reports")
    audit.add_argument("--json", action="store_true", help="Print JSON instead of markdown")

    check = sub.add_parser("check", help="Check a single tool name")
    check.add_argument("tool")
    check.add_argument("--json", action="store_true")

    repair = sub.add_parser("repair", help="Run conservative repair helpers")
    repair.add_argument("--json", action="store_true")

    args = parser.parse_args()
    registry = load_json(REGISTRY_PATH)
    state = collect_state(registry)

    if args.cmd == "audit":
      results = audit_registry(registry, state)
      if args.write:
          write_reports(results)
      print_result(results, as_json=args.json)
      return 0 if (
          not results["summary"]["missing_codex"]
          and not results["summary"]["codex_partial"]
          and not results["summary"]["skill_mirror_drift"]
      ) else 1

    if args.cmd == "check":
      result = check_tool(args.tool, registry, state)
      print_result(result, as_json=args.json)
      return 0 if result["status"] == "codex_ready" else 2

    if args.cmd == "repair":
      result = repair_basics()
      print_result(result, as_json=args.json)
      return 0 if not result["failed"] else 1

    return 2


def collect_state(registry: dict[str, Any]) -> RuntimeState:
    command_names = set()
    npx_packages = set()
    for tool in registry["priority_tools"]:
        command_names.update(tool.get("commands", []))
        npx_packages.update(tool.get("npx_packages", []))
    command_names.update(["node", "npx", "python", "gh", "vercel", "go"])
    commands = {name: shutil.which(name) for name in sorted(command_names)}

    return RuntimeState(
        claude_skills=parse_claude_skills(),
        codex_skills=parse_codex_skills(),
        canonical_skills=scan_root_skill_names(CANONICAL_SKILLS),
        codex_local_skills=scan_skill_names(CODEX_HOME / "skills"),
        claude_mcps=parse_claude_mcps(),
        codex_mcps=parse_codex_mcps(),
        codex_plugins=parse_codex_plugins(),
        commands=commands,
        npx_packages={name: bool(commands.get("npx")) for name in sorted(npx_packages)},
    )


def parse_claude_skills() -> set[str]:
    return scan_skill_names(CLAUDE_HOME / "skills")


def parse_codex_skills() -> set[str]:
    skills = set()
    for base in [
        CODEX_HOME / "skills",
        CODEX_HOME / "plugins" / "cache",
    ]:
        skills.update(scan_skill_names(base))
    return skills


def inspect_project_skill_mirror() -> dict[str, Any]:
    expected = {}
    if CANONICAL_SKILLS.exists():
        expected = {
            path.name: path
            for path in CANONICAL_SKILLS.iterdir()
            if path.is_dir() and (path / "SKILL.md").is_file()
        }

    missing = []
    wrong_target = []
    mirror_root = CODEX_HOME / "skills"
    for name, source in sorted(expected.items()):
        mirror = mirror_root / name
        if not mirror.exists():
            missing.append(name)
            continue
        try:
            if mirror.resolve(strict=True) != source.resolve(strict=True):
                wrong_target.append(name)
        except OSError:
            wrong_target.append(name)

    return {
        "expected": len(expected),
        "missing": missing,
        "wrong_target": wrong_target,
        "exact": not missing and not wrong_target,
    }


def scan_skill_names(base: Path) -> set[str]:
    names = set()
    if not base.exists():
        return names
    for path in base.rglob("SKILL.md"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:1000]
        except OSError:
            continue
        match = re.search(r"(?m)^name:\s*([^\n\r]+)", text)
        if match:
            names.add(match.group(1).strip().strip('"'))
        else:
            names.add(path.parent.name)
    return names


def scan_root_skill_names(base: Path) -> set[str]:
    """Return callable skills only, excluding nested wrench/helper SKILL.md files."""
    names = set()
    if not base.exists():
        return names
    for directory in base.iterdir():
        path = directory / "SKILL.md"
        if not directory.is_dir() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:1000]
        except OSError:
            continue
        match = re.search(r"(?m)^name:\s*([^\n\r]+)", text)
        names.add(match.group(1).strip().strip('"') if match else directory.name)
    return names


def parse_claude_mcps() -> set[str]:
    names = set()
    for path in (CLAUDE_MCP, CLAUDE_USER_CONFIG):
        if not path.exists():
            continue
        try:
            data = load_json(path)
        except json.JSONDecodeError:
            continue
        names.update((data.get("mcpServers") or {}).keys())
    return names


def parse_codex_mcps() -> set[str]:
    if not CODEX_CONFIG.exists():
        return set()
    text = CODEX_CONFIG.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"(?m)^\[mcp_servers\.([^\].]+)\]$", text))


def parse_codex_plugins() -> set[str]:
    plugins = set()
    if CODEX_CONFIG.exists():
        text = CODEX_CONFIG.read_text(encoding="utf-8", errors="replace")
        plugins.update(re.findall(r'(?m)^\[plugins\."([^"]+)"\]', text))
    # Remote catalog installs (e.g. github@openai-curated-remote) never appear in config.toml.
    if CODEX_PLUGIN_CACHE.is_dir():
        for marketplace in CODEX_PLUGIN_CACHE.iterdir():
            if marketplace.is_dir():
                plugins.update(f"{plugin.name}@{marketplace.name}" for plugin in marketplace.iterdir() if plugin.is_dir())
    return plugins


def audit_registry(registry: dict[str, Any], state: RuntimeState) -> dict[str, Any]:
    tools = [evaluate_tool(tool, state) for tool in registry["priority_tools"]]
    missing = [tool for tool in tools if tool["status"] == "missing_codex"]
    partial = [tool for tool in tools if tool["status"] == "codex_partial"]
    mirror = inspect_project_skill_mirror()
    return {
        "policy": registry["policy"],
        "summary": {
            "total": len(tools),
            "codex_ready": sum(1 for tool in tools if tool["status"] == "codex_ready"),
            "codex_partial": len(partial),
            "missing_codex": len(missing),
            "skill_mirror_drift": len(mirror["missing"]) + len(mirror["wrong_target"]),
        },
        "inventory": {
            "claude_skill_count": len(state.claude_skills),
            "codex_skill_count": len(state.codex_skills),
            "canonical_skill_count": len(state.canonical_skills),
            "codex_local_skill_count": len(state.codex_local_skills),
            "project_skill_mirror": mirror,
            "claude_mcps": sorted(state.claude_mcps),
            "codex_mcps": sorted(state.codex_mcps),
            "codex_plugins": sorted(state.codex_plugins),
            "commands": {name: bool(path) for name, path in sorted(state.commands.items())},
            "npx_packages": sorted(name for name, ok in state.npx_packages.items() if ok),
        },
        "tools": tools,
    }


def check_tool(name: str, registry: dict[str, Any], state: RuntimeState) -> dict[str, Any]:
    needle = normalize(name)
    for tool in registry["priority_tools"]:
        aliases = [tool["name"], *tool.get("aliases", [])]
        if any(_tok_match(needle, normalize(alias)) for alias in aliases):
            return evaluate_tool(tool, state)
    discovered = discover_named_tool(name, state)
    if discovered:
        return discovered
    return {
        "name": name,
        "status": "unknown_tool",
        "codex_ready_reasons": [],
        "claude_reasons": [],
        "missing": ["not present in registry"],
        "next_action": "Search Codex tools/plugins first; if still missing, route through Printing Press or MCP builder.",
    }


def evaluate_tool(tool: dict[str, Any], state: RuntimeState) -> dict[str, Any]:
    codex_ready = []
    claude_reasons = []
    missing = []

    for plugin in tool.get("codex_plugins", []):
        if plugin in state.codex_plugins:
            codex_ready.append(f"codex plugin: {plugin}")
        else:
            missing.append(f"codex plugin: {plugin}")

    if prefix_any(state.codex_skills, tool.get("codex_skill_prefixes", [])):
        codex_ready.append("codex skill")
    elif tool.get("codex_skill_prefixes"):
        missing.append(f"codex skill prefix: {', '.join(tool.get('codex_skill_prefixes', []))}")

    found_commands = [cmd for cmd in tool.get("commands", []) if state.commands.get(cmd)]
    if found_commands:
        codex_ready.append(f"cli: {', '.join(found_commands)}")
    elif tool.get("commands"):
        missing.append(f"cli: {', '.join(tool.get('commands', []))}")

    found_paths = []
    for value in tool.get("file_paths", []):
        path = Path(os.path.expandvars(value)).expanduser()
        if path.exists():
            found_paths.append(path.name)
    if found_paths:
        codex_ready.append(f"local route: {', '.join(found_paths)}")
    elif tool.get("file_paths"):
        missing.append(f"local route: {', '.join(tool.get('file_paths', []))}")

    if endpoint := tool.get("endpoint"):
        codex_ready.append(f"configured endpoint: {endpoint}")

    found_npx = [pkg for pkg in tool.get("npx_packages", []) if state.npx_packages.get(pkg)]
    if found_npx:
        codex_ready.append(f"npx: {', '.join(found_npx)}")
    elif tool.get("npx_packages"):
        missing.append(f"npx package route: {', '.join(tool.get('npx_packages', []))}")

    if any(mcp in state.codex_mcps for mcp in tool.get("mcp_names", [])):
        codex_ready.append("codex mcp")
    elif tool.get("mcp_names"):
        missing.append(f"codex mcp: {', '.join(tool.get('mcp_names', []))}")

    if prefix_any(state.claude_skills, tool.get("claude_skill_prefixes", [])):
        claude_reasons.append("claude skill")
    if any(mcp in state.claude_mcps for mcp in tool.get("mcp_names", [])):
        claude_reasons.append("claude mcp")

    if codex_ready:
        status = "codex_ready"
    elif claude_reasons:
        status = "missing_codex"
    else:
        status = "codex_partial"

    return {
        "name": tool["name"],
        "status": status,
        "codex_ready_reasons": codex_ready,
        "claude_reasons": claude_reasons,
        "missing": missing,
        "next_action": tool.get("repair_hint", ""),
    }


def discover_named_tool(name: str, state: RuntimeState) -> dict[str, Any] | None:
    needle = normalize(name)
    codex_ready = []
    claude_reasons = []

    codex_skill_matches = fuzzy_matches(needle, state.codex_skills)
    codex_mcp_matches = fuzzy_matches(needle, state.codex_mcps)
    codex_plugin_matches = fuzzy_matches(needle, state.codex_plugins)
    claude_skill_matches = fuzzy_matches(needle, state.claude_skills)
    claude_mcp_matches = fuzzy_matches(needle, state.claude_mcps)

    if codex_skill_matches:
        codex_ready.append(f"codex skill: {', '.join(codex_skill_matches[:5])}")
    if codex_mcp_matches:
        codex_ready.append(f"codex mcp: {', '.join(codex_mcp_matches[:5])}")
    if codex_plugin_matches:
        codex_ready.append(f"codex plugin: {', '.join(codex_plugin_matches[:5])}")
    if state.commands.get(name):
        codex_ready.append(f"cli: {name}")
    if claude_skill_matches:
        claude_reasons.append(f"claude skill: {', '.join(claude_skill_matches[:5])}")
    if claude_mcp_matches:
        claude_reasons.append(f"claude mcp: {', '.join(claude_mcp_matches[:5])}")

    if not codex_ready and not claude_reasons:
        return None

    return {
        "name": name,
        "status": "codex_ready" if codex_ready else "missing_codex",
        "codex_ready_reasons": codex_ready,
        "claude_reasons": claude_reasons,
        "missing": [] if codex_ready else ["no Codex skill/plugin/MCP/CLI match"],
        "next_action": "Use the matched Codex route, or add this tool to registry.json if it should become a tracked priority tool.",
    }


def _tok_match(needle: str, normalized: str) -> bool:
    """Token-boundary match, NOT raw substring. Avoids 'read' matching 'spreadsheets'.

    True iff needle equals the whole normalized string, or needle is a whole '-'-token of
    it (or vice-versa). normalize() emits '-'-separated tokens, so this is word-boundary safe.
    """
    if needle == normalized:
        return True
    return needle in normalized.split("-") or normalized in needle.split("-")


def fuzzy_matches(needle: str, values: set[str]) -> list[str]:
    matches = []
    for value in values:
        if _tok_match(needle, normalize(value)):
            matches.append(value)
    return sorted(matches)


def prefix_any(values: set[str], prefixes: list[str]) -> bool:
    if not prefixes:
        return False
    for value in values:
        for prefix in prefixes:
            if value == prefix or value.startswith(prefix if prefix.endswith((":", "-")) else f"{prefix}:"):
                return True
    return False


def repair_basics() -> dict[str, Any]:
    actions = []
    failed = []
    mirror_script = CLAUDE_HOME / "scripts" / "mirror-skills-to-codex.ps1"
    if mirror_script.exists():
        command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(mirror_script)]
        checked = run([*command, "-Check"])
        actions.append({"action": "audit canonical skill junctions", "ok": checked.returncode in {0, 2}})
        if checked.returncode == 2:
            repaired = run(command)
            actions.append({"action": "repair safe skill-junction drift", "ok": repaired.returncode == 0})
            if repaired.returncode != 0:
                failed.append("mirror-skills-to-codex.ps1 repair")
        elif checked.returncode != 0:
            failed.append("skill mirror has a collision or configuration error")
    else:
        failed.append("missing mirror-skills-to-codex.ps1")

    return {
        "actions": actions,
        "failed": failed,
        "note": "Conservative repair only. Install plugins/CLIs explicitly after audit confirms the missing capability.",
    }


def write_reports(results: dict[str, Any]) -> None:
    (TOOL_ROOT / "tool-parity-report.json").write_text(
        json.dumps(results, indent=2),
        encoding="utf-8",
    )
    (TOOL_ROOT / "tool-parity-report.md").write_text(render_markdown(results), encoding="utf-8")


def render_markdown(results: dict[str, Any]) -> str:
    summary = results["summary"]
    lines = [
        "# Claude/Codex Tool Parity Report",
        "",
        f"- Total tools tracked: {summary['total']}",
        f"- Codex ready: {summary['codex_ready']}",
        f"- Codex partial: {summary['codex_partial']}",
        f"- Missing Codex capability: {summary['missing_codex']}",
        f"- Project skill mirror drift: {summary['skill_mirror_drift']}",
        f"- Claude skills discovered: {results.get('inventory', {}).get('claude_skill_count', 0)}",
        f"- Codex skills discovered: {results.get('inventory', {}).get('codex_skill_count', 0)}",
        f"- Canonical project skills: {results.get('inventory', {}).get('canonical_skill_count', 0)}",
        "",
        "## Codex Plugins",
        "",
        ", ".join(results.get("inventory", {}).get("codex_plugins", [])) or "-",
        "",
        "## MCP Servers",
        "",
        f"- Claude: {', '.join(results.get('inventory', {}).get('claude_mcps', [])) or '-'}",
        f"- Codex: {', '.join(results.get('inventory', {}).get('codex_mcps', [])) or '-'}",
        "",
        "## Priority Tool Routes",
        "",
        "| Tool | Status | Codex Ready | Missing | Next Action |",
        "|---|---|---|---|---|",
    ]
    for tool in results["tools"]:
        lines.append(
            "| {name} | {status} | {ready} | {missing} | {next_action} |".format(
                name=tool["name"],
                status=tool["status"],
                ready=", ".join(tool["codex_ready_reasons"]) or "-",
                missing=", ".join(tool["missing"]) or "-",
                next_action=tool["next_action"].replace("|", "/"),
            )
        )
    lines.append("")
    return "\n".join(lines)


def print_result(value: dict[str, Any], as_json: bool) -> None:
    if as_json or "tools" not in value:
        print(json.dumps(value, indent=2))
    else:
        print(render_markdown(value))


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True)


if __name__ == "__main__":
    raise SystemExit(main())
