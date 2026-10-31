#!/usr/bin/env python3
"""Check profiles/*/profile.json against install/tools.json and the contract in profiles/README.md.
Stdlib only, no network, writes nothing.

    python3 profiles/test_profiles.py              # PASS, or every problem found and exit 1
    python3 profiles/test_profiles.py --self-test  # broken copies of the profiles must each be caught
"""
import copy
import json
import re
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
PROFILES = ("odin", "loki", "heimdall", "thor", "raven", "telegram")
OSES = {"arch", "debian", "macos"}
TOP = {"name": str, "role": str, "for": str, "addon": bool, "os": list, "tools": list, "services": list,
       "automations": list, "rules": list, "requires_hardware": dict}
HW_KEYS = {"ram_gb", "disk_gb", "always_on", "any_of"}
BRANCH = {"gpu": {"nvidia"}, "cpu": {"apple-silicon"}}           # allowed values per kind
BRANCH_NUM = {"vram_gb", "unified_memory_gb", "ram_gb"}
SERVICE_KEYS = {"compose": {"name", "kind", "tool", "compose", "service", "compose_profiles", "env_example", "env_file",
                            "project_dir", "health", "optional", "linux_only"},
                "systemd": {"name", "kind", "tool", "unit", "scope", "health", "optional", "linux_only"}}
AUTO_KEYS = {"name", "tool", "unit", "files", "scope", "enable", "kill_switch", "linux_only"}
RULE_KEYS = {"src", "dest", "how", "tool"}
MERGE = {"merge-markdown", "merge-json"}
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
# a path into the kit named in a README (a command or a file to copy): it must exist, relative to the kit's root
KIT_PATH = re.compile(r"(?<![\w/.~$-])((?:servers|profiles|install|framework|examples|docs|tools)/[A-Za-z0-9_.@/-]*[A-Za-z0-9_@/-])")
# commands written for the old servers/ folder: wrong once the page moved to profiles/<name>/
STALE = re.compile(r"(?<![\w/])\./(?:linux|macos|tools)/|\.\\windows\\|(?<![\w/])compose/(?:media|heimdall|workhorse)\b")
MOVED = {"servers/media.md": "loki", "servers/heimdall.md": "heimdall", "servers/workhorse.md": "thor"}


def load_manifest():
    return {t["id"]: t for t in json.loads((KIT / "install" / "tools.json").read_text())["tools"]}


def load_profiles():
    out = {}
    for name in PROFILES:
        f = KIT / "profiles" / name / "profile.json"
        out[name] = json.loads(f.read_text()) if f.is_file() else None
    return out


def compose_has(path, service, profile=None):
    """service is a top-level entry under services: in the compose file; profile appears in a profiles: list."""
    text = (KIT / path).read_text()
    if not re.search(rf"^  {re.escape(service)}:\s*$", text, re.M):
        return False
    return profile is None or re.search(rf"^\s+profiles:\s*\[[^\]]*\b{re.escape(profile)}\b", text, re.M) is not None


def compose_name(path):
    """The project name a compose file sets for itself (top-level name:), or None."""
    m = re.search(r"^name:\s*['\"]?([A-Za-z0-9_.-]+)", (KIT / path).read_text(), re.M)
    return m.group(1) if m else None


def docs():
    """{kit-relative path: text} of every page the link checks cover: the profile READMEs and servers/*.md."""
    pages = sorted((KIT / "profiles").rglob("*.md")) + sorted((KIT / "servers").glob("*.md"))
    return {str(f.relative_to(KIT)): f.read_text() for f in pages}


def check_docs(pages, exists=lambda rel: (KIT / rel).exists()):
    """Links resolve; the profile READMEs name only kit paths that exist, say where commands run from, and use no
    command written for the old servers/ folder; the old server pages are one-line pointers to the profiles."""
    errs = []
    for rel, text in pages.items():
        base = Path(rel).parent
        for target in LINK.findall(text):
            if re.match(r"[a-z]+:", target) or target.startswith("#"):
                continue
            path = target.split("#", 1)[0]
            if path and not exists(str(Path(base, path))):
                errs.append(f"{rel}: broken link: {target}")
        if not rel.startswith("profiles/"):
            continue
        for m in sorted(set(KIT_PATH.findall(text))):
            if not exists(m.rstrip("/")):
                errs.append(f"{rel}: names {m}, which is not in the kit")
        for m in sorted(set(STALE.findall(text))):
            errs.append(f"{rel}: '{m}' is a path relative to the old servers/ folder: name it from the kit's root")
        if Path(rel).name == "README.md" and rel.count("/") == 2 and "kit's root folder" not in text:
            errs.append(f"{rel}: say which folder the commands run from (the kit's root folder)")
    for rel, name in MOVED.items():
        text = pages.get(rel, "")
        if f"../profiles/{name}/README.md" not in text or len(text.strip().splitlines()) != 1:
            errs.append(f"{rel}: must be a one-line pointer to ../profiles/{name}/README.md")
    return errs


def check(profiles, manifest):
    errs = []
    projects = {}   # effective compose project name -> {compose files}; one machine can take several profiles

    def err(where, msg):
        errs.append(f"{where}: {msg}")

    for name in PROFILES:
        p = profiles.get(name)
        if p is None:
            err(name, f"profiles/{name}/profile.json is missing or not valid JSON")
            continue
        for k, typ in TOP.items():
            if not isinstance(p.get(k), typ):
                err(name, f"'{k}' missing or not a {typ.__name__}")
        for k in p.keys() - TOP.keys():
            err(name, f"unknown key '{k}'")
        if any(not isinstance(p.get(k), typ) for k, typ in TOP.items()):
            continue
        if p["name"] != name:
            err(name, f"name '{p['name']}' must match its folder")
        if p["addon"] != (name == "telegram"):
            err(name, "only the telegram profile is an add-on")
        if not p["os"] or not set(p["os"]) <= OSES:
            err(name, f"bad os list {p['os']}")
        if not p["role"].strip() or not p["for"].strip():
            err(name, "role and for must say something")

        # tools: exist, no duplicates, and match the manifest's own profiles field both ways
        listed = p["tools"]
        if not all(isinstance(tid, str) for tid in listed):
            err(name, "tools must be a list of tool id strings")
            continue
        if len(set(listed)) != len(listed):
            err(name, "duplicate tool ids")
        for tid in listed:
            t = manifest.get(tid)
            if t is None:
                err(name, f"tool '{tid}' is not in install/tools.json")
            elif not set(t.get("os", OSES)) & set(p["os"]):
                err(name, f"tool '{tid}' supports none of this profile's OSes")
        want = {tid for tid, t in manifest.items() if name in t.get("profiles", [])}
        for tid in sorted(want - set(listed)):
            err(name, f"tools.json puts '{tid}' in this profile, but profile.json does not list it")
        for tid in sorted(set(listed) - want):
            if tid in manifest:
                err(name, f"lists '{tid}', but its tools.json entry does not name this profile")
        if not listed:
            err(name, "a profile needs at least one tool")

        def tool_ok(where, item):
            tid = item.get("tool")
            if tid is not None and tid not in listed:
                err(where, f"tool '{tid}' is not in this profile's tools")

        for i, s in enumerate(p["services"]):
            where = f"{name} services[{i}]"
            if not isinstance(s, dict) or s.get("kind") not in SERVICE_KEYS:
                err(where, "needs kind compose or systemd")
                continue
            for k in s.keys() - SERVICE_KEYS[s["kind"]]:
                err(where, f"unknown key '{k}'")
            for k in ("name", "health"):
                if not isinstance(s.get(k), str) or not s[k].strip():
                    err(where, f"'{k}' must be a non-empty string")
            for k in ("optional", "linux_only"):
                if not isinstance(s.get(k), bool):
                    err(where, f"'{k}' must be true or false")
            tool_ok(where, s)
            if s["kind"] == "compose":
                path = s.get("compose", "")
                if not (KIT / path).is_file():
                    err(where, f"compose file missing: {path}")
                    continue
                if not compose_has(path, s.get("service", "")):
                    err(where, f"service '{s.get('service')}' is not in {path}")
                if not isinstance(s.get("compose_profiles"), list):
                    err(where, "compose_profiles must be a list (empty = the default profile)")
                else:
                    for cp in s["compose_profiles"]:
                        if not compose_has(path, s.get("service", ""), cp):
                            err(where, f"compose profile '{cp}' is not used in {path}")
                if "env_example" in s and not (KIT / s["env_example"]).is_file():
                    err(where, f"env_example missing: {s['env_example']}")
                if "env_file" in s and not str(s["env_file"]).startswith("~/"):
                    err(where, "env_file must be a path under ~/")
                # where docker compose runs: never the template in the kit (a second copy of the project would
                # recreate the containers with their data folders inside the clone)
                pd = s.get("project_dir")
                want = {"linux"} if s.get("linux_only") else {"linux", "macos"}
                if not isinstance(pd, dict) or set(pd) != want:
                    err(where, f"project_dir must map exactly {sorted(want)} to the directory compose runs in")
                    pd = pd if isinstance(pd, dict) else {}
                for o, d in pd.items():
                    if not isinstance(d, str) or not d.startswith(("/", "~/")) or d.rstrip("/") in ("", "~"):
                        err(where, f"project_dir.{o} must be an absolute or ~/ directory: {d!r}")
                        continue
                    proj = compose_name(path) or Path(d.rstrip("/")).name
                    projects.setdefault(proj, set()).add(path)
            else:
                if not str(s.get("unit", "")).endswith(".service"):
                    err(where, "a systemd service needs unit: <name>.service")
                if s.get("scope") not in ("user", "system"):
                    err(where, "scope must be user or system")
                if s.get("linux_only") is not True:
                    err(where, "a systemd service is linux_only")

        for i, a in enumerate(p["automations"]):
            where = f"{name} automations[{i}]"
            if not isinstance(a, dict):
                err(where, "must be an object")
                continue
            for k in AUTO_KEYS - a.keys():
                err(where, f"missing '{k}'")
            for k in a.keys() - AUTO_KEYS:
                err(where, f"unknown key '{k}'")
            if not str(a.get("kill_switch", "")).strip():
                err(where, "every automation names its kill switch")
            if a.get("scope") not in ("user", "system"):
                err(where, "scope must be user or system")
            if not isinstance(a.get("enable"), bool):
                err(where, "enable must be true or false")
            unit, files = a.get("unit", ""), a.get("files", [])
            if not re.search(r"\.(timer|service|slice|path)$", str(unit)):
                err(where, f"unit must be a systemd unit file: {unit}")
            if not isinstance(files, list) or unit not in files:
                err(where, "files must list every unit file to install, the enabled unit included")
                files = files if isinstance(files, list) else []
            for f in files:
                if not (KIT / f).is_file():
                    err(where, f"unit file missing: {f}")
            if re.search(r"\.(timer|service|slice|path)$", str(unit)) and a.get("linux_only") is not True:
                err(where, "systemd units are linux_only: true")
            tool_ok(where, a)
            t = manifest.get(a.get("tool"))
            if t is not None and not set(t.get("os", OSES)) & {"arch", "debian"}:
                err(where, f"its tool '{t['id']}' does not run on Linux")

        for i, r in enumerate(p["rules"]):
            where = f"{name} rules[{i}]"
            if not isinstance(r, dict):
                err(where, "must be an object")
                continue
            for k in {"src", "dest", "how"} - r.keys():
                err(where, f"missing '{k}'")
            for k in r.keys() - RULE_KEYS:
                err(where, f"unknown key '{k}'")
            if not (KIT / str(r.get("src", ""))).is_file():
                err(where, f"src missing: {r.get('src')}")
            if not str(r.get("dest", "")).startswith("~/"):
                err(where, "dest must be a path under ~/")
            if r.get("how") not in MERGE:
                err(where, f"how must be one of {sorted(MERGE)} (rules are merged, never replaced)")
            if r.get("how") == "merge-json" and str(r.get("src", "")).endswith(".json"):
                try:
                    json.loads((KIT / r["src"]).read_text())
                except (OSError, ValueError) as e:
                    err(where, f"src is not valid JSON: {e}")
            tool_ok(where, r)

        hw = p["requires_hardware"]
        for k in hw.keys() - HW_KEYS:
            err(name, f"requires_hardware: unknown key '{k}'")
        for k in ("ram_gb", "disk_gb"):
            if k in hw and (not isinstance(hw[k], int) or isinstance(hw[k], bool) or hw[k] <= 0):
                err(name, f"requires_hardware.{k} must be a positive integer")
        if "always_on" in hw and not isinstance(hw["always_on"], bool):
            err(name, "requires_hardware.always_on must be true or false")
        if "any_of" in hw:
            if not isinstance(hw["any_of"], list) or len(hw["any_of"]) < 2:
                err(name, "requires_hardware.any_of needs at least two alternative branches")
            else:
                for j, b in enumerate(hw["any_of"]):
                    kinds = [k for k in b if k in BRANCH] if isinstance(b, dict) else []
                    if len(kinds) != 1:
                        err(name, f"any_of[{j}] needs exactly one of {sorted(BRANCH)}")
                        continue
                    for k, v in b.items():
                        if k in BRANCH:
                            if v not in BRANCH[k]:
                                err(name, f"any_of[{j}].{k} must be one of {sorted(BRANCH[k])}")
                        elif k in BRANCH_NUM:
                            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                                err(name, f"any_of[{j}].{k} must be a positive integer")
                        else:
                            err(name, f"any_of[{j}]: unknown key '{k}'")
                    if b.get("cpu") == "apple-silicon" and "macos" not in p["os"]:
                        err(name, "an apple-silicon branch needs macos in the profile's os list")

    for proj, files in sorted(projects.items()):
        if len(files) > 1:
            err("services", f"compose project '{proj}' would hold {len(files)} different files ({', '.join(sorted(files))}): "
                "give one of them another project_dir")

    errs += check_docs(docs())
    return errs


def self_test(profiles, manifest):
    cases = [
        ("missing profile", lambda p, m: p.update(raven=None), "missing or not valid JSON"),
        ("unknown tool", lambda p, m: p["odin"]["tools"].append("nope"), "'nope' is not in install/tools.json"),
        ("drift", lambda p, m: p["loki"]["tools"].remove("jellyfin"), "does not list it"),
        ("no kill switch", lambda p, m: p["odin"]["automations"][0].update(kill_switch=""), "kill switch"),
        ("missing unit", lambda p, m: p["thor"]["automations"][0]["files"].append("framework/nope.timer"), "unit file missing"),
        ("bad compose service", lambda p, m: p["loki"]["services"][0].update(service="jelly"), "is not in"),
        ("bad compose profile", lambda p, m: p["loki"]["services"][0].update(compose_profiles=["arrr"]), "compose profile"),
        ("replace rule", lambda p, m: p["odin"]["rules"][0].update(how="replace"), "merged, never replaced"),
        ("one branch", lambda p, m: p["thor"]["requires_hardware"].update(any_of=[{"gpu": "nvidia"}]), "two alternative"),
        ("bad gpu", lambda p, m: p["thor"]["requires_hardware"]["any_of"][0].update(gpu="amd"), "must be one of"),
        ("not linux_only", lambda p, m: p["heimdall"]["automations"][0].update(linux_only=False), "linux_only"),
        ("addon flag", lambda p, m: p["odin"].update(addon=True), "only the telegram profile"),
        ("no project_dir", lambda p, m: p["loki"]["services"][0].pop("project_dir"), "project_dir must map"),
        ("macos dir on linux_only", lambda p, m: p["loki"]["services"][0]["project_dir"].update(macos="~/x"),
         "project_dir must map"),
        ("relative project_dir", lambda p, m: p["heimdall"]["services"][0]["project_dir"].update(linux="srv/x"),
         "absolute or ~/"),
        ("project clash", lambda p, m: p["heimdall"]["services"][3]["project_dir"].update(linux="~/srv/heimdall"),
         "compose project 'heimdall' would hold 2"),
        ("foreign tool", lambda p, m: p["raven"]["services"].append(
            {"name": "x", "kind": "systemd", "tool": "ollama", "unit": "x.service", "scope": "user",
             "health": "true", "optional": True, "linux_only": True}), "not in this profile's tools"),
    ]
    real = docs()
    doc_cases = [
        ("broken link", "profiles/loki/README.md", "\n[x](nope.md)\n", "broken link: nope.md"),
        ("stale command", "profiles/thor/README.md", "\n    sudo ./linux/setup.sh thor\n", "old servers/ folder"),
        ("stale windows", "profiles/loki/README.md", "\n    .\\windows\\setup.ps1 -Lane loki\n", "old servers/ folder"),
        ("missing kit path", "profiles/heimdall/README.md", "\n    profiles/heimdall/deploy/nope.sh\n", "not in the kit"),
        ("server page link", "servers/common.md", "\n[x](media-gone.md)\n", "broken link: media-gone.md"),
        ("pointer grew", "servers/media.md", "more text\n", "one-line pointer"),
    ]
    bad = 0
    for name, rel, extra, expect in doc_cases:
        pages = dict(real)
        pages[rel] = pages[rel] + extra
        if not any(expect in e for e in check_docs(pages)):
            bad += 1
            print(f"FAIL self-test '{name}': expected '{expect}'")
    pages = dict(real)
    pages["profiles/raven/README.md"] = pages["profiles/raven/README.md"].replace("kit's root folder", "clone")
    if not any("which folder the commands run from" in e for e in check_docs(pages)):
        bad += 1
        print("FAIL self-test 'no working directory': not caught")
    cases_total = len(cases) + len(doc_cases) + 1
    for name, mutate, expect in cases:
        p, m = copy.deepcopy(profiles), copy.deepcopy(manifest)
        mutate(p, m)
        errs = check(p, m)
        if not any(expect in e for e in errs):
            bad += 1
            print(f"FAIL self-test '{name}': expected '{expect}', got {errs[:3]}")
    if check(copy.deepcopy(profiles), copy.deepcopy(manifest)):
        bad += 1
        print("FAIL self-test: the real profiles must pass first")
    print(f"self-test: {cases_total}/{cases_total} broken profiles and pages caught" if not bad else "self-test FAILED")
    return 1 if bad else 0


def main():
    try:
        manifest, profiles = load_manifest(), load_profiles()
    except (OSError, ValueError) as e:
        print(f"FAIL: cannot load: {e}")
        return 1
    if "--self-test" in sys.argv[1:]:
        return self_test(profiles, manifest)
    errs = check(profiles, manifest)
    if errs:
        print("\n".join(f"FAIL {e}" for e in errs))
        print(f"FAIL: {len(errs)} problem(s)")
        return 1
    print(f"PASS: {len(PROFILES)} profiles, " + ", ".join(f"{n} {len(profiles[n]['tools'])}" for n in PROFILES)
          + " tools; every tool, service, unit, rule, link and kit path resolves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
