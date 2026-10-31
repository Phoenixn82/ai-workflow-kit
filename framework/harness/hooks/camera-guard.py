#!/usr/bin/env python3
"""PreToolUse guard shared by Claude Code, Codex and Hermes: no AI touches a camera on any of the user's machines.

The user, 2026-10-20: "I really don't want AI to be able to look at my camera on any of my computers."
The uvcvideo driver is blocked by /etc/modprobe.d/no-camera.conf on odin, thor, heimdall and loki. raven keeps
its driver for the user's own Anker preview, so this hook is the only fence there. It denies shell commands that
open, test or re-enable a camera (any host, including over ssh), and browser/MCP calls that ask for getUserMedia.
"Take a picture" means a screenshot (loki-shot, raven-shot, grim), which stays allowed.
No override: only the user, by hand, re-enables a camera.

Default output is Claude Code's deny JSON. `--exit2` (Codex, Hermes) prints Hermes's block JSON, the reason on
stderr, and exits 2.
"""
import json
import re
import sys

SHELL = re.compile(
    r"/dev/video|/dev/v4l|v4l2|video4linux|\bfswebcam\b|\bwebcam|\bcheese\b|\bguvcview\b|\bimagesnap\b"
    r"|\buvcvideo\b|\bfacetimehd\b|no-camera\.conf|camera-(?:preview|photo|capture|snap)|\blibcamera|\bcam\s+-c"
    r"|getusermedia|mediadevices|termux-camera|usb-[\w.-]*_camera",
    re.I,
)
BROWSER = re.compile(r"getusermedia|mediadevices|\bvideoinput\b", re.I)
SHELL_TOOLS = {"Bash", "terminal", "shell", "exec_command", "local_shell", "unified_exec"}
REASON = ("camera-guard: the user does not allow any AI to use a camera on their machines (2026-10-20). "
          "If they asked for a picture, they mean a screenshot: loki-shot for Loki, grim or raven-shot elsewhere. "
          "Do not retry another way.")


def offending(tool, tool_input):
    if not isinstance(tool_input, dict):
        tool_input = {"command": str(tool_input)}
    if tool in SHELL_TOOLS or "command" in tool_input or "cmd" in tool_input:
        cmd = tool_input.get("command") or tool_input.get("cmd") or ""
        if isinstance(cmd, list):
            cmd = " ".join(map(str, cmd))
        return bool(SHELL.search(cmd))
    if tool.startswith("mcp"):
        return bool(BROWSER.search(json.dumps(tool_input)))
    return False


def main():
    data = json.load(sys.stdin)
    tool = data.get("tool_name") or data.get("tool") or ""
    if not offending(tool, data.get("tool_input") or data.get("arguments") or {}):
        return
    if "--exit2" in sys.argv:
        print(json.dumps({"decision": "block", "reason": REASON}))
        print(REASON, file=sys.stderr)
        sys.exit(2)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": REASON}}))


def self_test():
    bad = ["fswebcam -d /dev/video0 x.jpg", "ssh loki 'ffmpeg -f v4l2 -i /dev/video0 -frames 1 a.jpg'",
           "ssh raven anker-camera-preview", "sudo rm /etc/modprobe.d/no-camera.conf", "sudo modprobe uvcvideo",
           "v4l2-ctl --list-devices", "mpv av://v4l2:/dev/video0", "gst-launch-1.0 v4l2src ! jpegenc",
           "omarchy-capture-webcam", "ls /dev/v4l/by-id", "cam -c 1 --capture=1"]
    good = ["loki-shot /tmp/a.png", "grim /tmp/s.png", "ssh raven raven-shot", "ls -la ~/Downloads",
            "git commit -m 'fix camera clock dates'", "magick a.png b.jpg"]
    assert all(offending("Bash", {"command": c}) for c in bad), [c for c in bad if not offending("Bash", {"command": c})]
    assert not any(offending("Bash", {"command": c}) for c in good), [c for c in good if offending("Bash", {"command": c})]
    assert offending("terminal", {"command": "fswebcam a.jpg"})
    assert offending("mcp__chrome-devtools__evaluate_script", {"function": "() => navigator.mediaDevices.getUserMedia({video:true})"})
    assert not offending("mcp__chrome-devtools__take_screenshot", {})
    assert not offending("Write", {"file_path": "/x/notes.md", "content": "uvcvideo is blocked"})
    print("camera-guard self-test passed")


if __name__ == "__main__":
    self_test() if sys.argv[1:] == ["--self-test"] else main()
