# Example vault (second brain)

Plain markdown, built for an AI to read. Copy this folder to `~/AI_Projects/_system/second-brain/`
(or anywhere, then tell your agent where it is). Keep it in a **private** git repo so it syncs between machines.

```
boot.md          tiny always-injected orientation (keep it stable: it is prompt-cached)
index.md         the router: one line per note, so the agent loads ONE file, never the whole vault
Actions/         standing defaults ("how I want X done"), one file each
Reference/       facts that rarely change (hardware, accounts, where things live)
Projects/<slug>/ per project: NOTES.md (you), RESUME.md + _brief.md + history/decisions/errors/lessons (pipeline)
```

Rules that keep it useful:
- Load lean: boot + index always; a project's `_brief.md` when it comes up; one named file on explicit need.
- Every error gets **what / cause / fix / prevention**, so it never happens twice.
- When you ask for the same thing twice, it becomes an `Actions/` file and is never asked again.
- Archive, don't delete (`_archive/`).
