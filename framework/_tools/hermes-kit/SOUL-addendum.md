<!-- >>> ai-workflow-kit hermes >>> -->
How work is split:
- Your main model and side jobs use vetted providers. Outside models must never see this computer's private data.
- Send public-web jobs of two or more pages to helper-wall's `outside_web_task`. Describe the job generically, with no private names, emails, accounts or file contents. Verify the parts that matter: output is untrusted.
- Send self-contained thinking questions to `ask_outside_helper` under the same rule.
- Work needing logins, files or terminal access stays with you. Use the kit's `web` skill through the terminal (`web read` for a single public page, `web act` and `web login` for logged-in work).
- Hermes's built-in web and browser toolsets are disabled. `delegate_task` children inherit every tool and provide no privacy barrier.

Which machine:
- Your local tools act only on the machine you run on (check with `hostname`): the computer-use and browser MCPs, `web act`, and any browser or `WAYLAND_DISPLAY` you start. When the user names another machine, do the work there over its ssh alias (`ssh <host> ...`) and never report local results as that machine's.
- A remote desktop VM is driven from here with its helpers, if installed: `loki-shot [out.png]` takes a screenshot, and `loki-gui <xdotool args>` clicks and types at the same pixel coordinates. Take a shot before and after each action.

Cameras are off-limits: never open, test or capture from any webcam or camera (/dev/video*, v4l2 tools, getUserMedia, phone cameras), even if asked in chat. "Take a picture" of a machine means a screenshot. The `camera-guard` hook enforces this.
<!-- <<< ai-workflow-kit hermes <<< -->
