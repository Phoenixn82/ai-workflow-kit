# Implementation prompt

Copy everything below the line into your AI (Claude Code, Codex, ChatGPT, Gemini, a local model, anything). If it can read files and run commands, open it in this repo folder; if it can't, attach the files it asks for and run the commands it gives you.

---

You are helping me adopt parts of an AI workflow kit. The kit is in this folder: `README.md`, `profiles/` (six setups named after machines: odin, loki, heimdall, thor, raven, and a telegram add-on), `install/` (a tool manifest, `install.sh` and `doctor.sh`), `docs/` (the ideas), `framework/` (real skills, hooks, CLIs and config, privacy-scrubbed), `servers/` (turning old laptops into headless servers) and `examples/` (starter templates, including `hosts.env.example`).

Work in this order and **ask before changing anything on my machine**:

1. **Read** `README.md`, `profiles/README.md` and every file in `docs/`. Skim `install/README.md`, `framework/SKILLS_INDEX.md` and `framework/DECISION_MAP.md`. Don't read the whole framework up front; open a file only when it becomes relevant.
2. **Interview me**, one question at a time, max ~8 questions:
   - Which AI tools do I use, and what can each actually do (chat only, run shell commands, edit files, run hooks, use MCP servers)?
   - Which machines do I have, with which OS? For each: is it my daily laptop, a spare laptop, a GPU box (which GPU, how much VRAM or unified memory), a tablet or second laptop I only connect from?
   - Do I want a media server for my own media, an always-on box for scheduled jobs and my projects, phone alerts on Telegram?
   - What do I spend most of my time on (coding, writing, research, business, media)?
   - What annoys me most about my current AI setup?
   - Do I keep notes anywhere? Do I use GitHub and Tailscale?
3. **Match profiles to my machines.** For each machine, say which profile fits (odin = the cockpit where the agents run; loki = media server; heimdall = always-on server and project host; thor = heavy local AI box; raven = viewer that only connects; telegram = add-on for any of them), which I can skip, and why. Use each profile's README for its hardware needs. If a machine is short of a profile's hardware (for example thor without an NVIDIA GPU with 8 GB VRAM or Apple silicon with 16 GB), say so and suggest the smaller option.
   If I have a free-model router or want a free multi-model harness, offer the optional Hermes setup in `docs/hermes.md`: one session, trusted pools, model hops and scrubbed outside helpers. Include it in the plan only if it fits.
4. **Check what each machine already has.** Run `install/doctor.sh --profile <name>` on it (or give me the command to run) and summarise: what is ok, missing, or the wrong version.
5. **Show me the plan before anything is installed.** Run `install/install.sh --profile <name>[,<name>] --dry-run` (add `--os arch|debian|macos` if detection is wrong). Show me the plan in plain words: what gets installed and how, what is skipped as already present, what needs a manual step, and which files would be backed up. Explain `--harness` (it merges hooks and settings into my agent's config, after a backup) and recommend whether I need it. Ask me to confirm.
6. **Install only on my yes.** Run the same command without `--dry-run` (with `--yes` only if I said "go ahead" for the whole plan). If an install fails, stop, show me the error, and ask before retrying another way. Then:
   - if `~/.config/ai-workflow-kit/hosts.env` doesn't exist yet, create it from `examples/hosts.env.example` (mode 600) and fill in only the hosts I have, asking me for each value; if it exists, leave it as it is and tell me which keys the new tools read;
   - if I don't have an agent or model that `lanes.json` names (for example no Codex), point those lanes at what I do have, in the file `route --lanes-path` prints (see "Changing a route or a lane" in `docs/agents-and-lanes.md`), then run `route --self-test` and show me `route list`;
   - run `install/doctor.sh --profile <name>` again and each installed tool's self-test, and show me the result;
   - tell me how to undo it (`install/uninstall.md`, and the `.bak-<timestamp>` files the installer left).
7. **Adopt the ideas that aren't machinery.** Offer 1–3 habits from `docs/` that fit what I told you, ranked by payoff (for example the decision ladder in `docs/philosophy.md` as rules in my agent's instruction file, a plain-markdown vault from `examples/vault/`, or the lessons in `docs/lessons.md`). For each: what it does for me, the effort, and how to undo it. If my AI can't run hooks or shell commands, keep the *idea* as written rules and drop the machinery. Rewrite placeholder paths (`$HOME/AI_Projects`, `<your-github>`, machine names like `thor`) for my setup, and rename anything that clashes with mine.
8. **Never** paste secrets into chat, push anything public, or delete files without asking. Put new files in a git repo so every change can be undone.
9. **At the end, ask me whether I'd like to share this session's transcript with the kit's maintainer.** It's optional. It shows them where onboarding is clear and where it's confusing. If I say yes:
   - tell me where this session is saved (Claude Code: `~/.claude/projects/<project-folder>/*.jsonl`; Codex: `~/.codex/sessions/YYYY/MM/DD/*.jsonl`; chat apps: a share link or a copy-paste into a text file);
   - help me find and redact keys, tokens, passwords, and anything else I want to keep private;
   - put the redacted files (zipped if they're big) in a new **private** GitHub repo on my account called `ai-workflow-kit-transcript`, then add the account the kit came from as a collaborator. Their invite is how they find out it's there. Use `gh` and git if you can run them; otherwise walk me through the GitHub website (New repository → Private → *uploading an existing file* → Settings → Collaborators). Get the kit owner's account from this folder's git remote, or ask me. I can delete the repo any time;
   - never attach a transcript to an issue, because everyone who can see the issue can open its attachments.

Start with step 1, then ask your first question.
