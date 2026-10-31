# ssh-keylog: see what each ssh key really runs

The first step toward **one limited key per job**. Over time every box collects keys that can run anything:
your laptop's key, a job's key, an agent's key. Before you lock them down, you need to know what each one is
used for, or a restriction breaks a nightly job you forgot about.

`ssh-keylog` is a pass-through forced command. Put it in front of a key line in `~/.ssh/authorized_keys` and every
login with that key writes one journal line (what ran, from where, under which label), then runs exactly what it
would have run anyway. Shells, commands, sftp, scp and rsync all keep working. It changes nothing about what the key
may do: it only watches.

`ssh-keylog-report` then groups a week of those lines per key and program, so you can see the patterns.

## Install

On each box whose keys you want to watch (Linux or macOS with OpenSSH), from the kit's root folder:

```sh
sudo install -m 755 servers/tools/ssh-keylog/ssh-keylog /usr/local/bin/ssh-keylog
ssh-keylog --self-test                                  # redaction check, offline
```

Then wrap a key. Pick a short label per key (`laptop`, `backup-job`, `agent`) and match the key by its comment
(the last word on the line). Back up first, change one line, and keep your current session open until a fresh
login works:

```sh
cp -p ~/.ssh/authorized_keys ~/.ssh/authorized_keys.bak-keylog
awk -v c='you@laptop' -v pre='command="/usr/local/bin/ssh-keylog laptop" ' \
  '$NF == c && $1 ~ /^(ssh-|ecdsa-|sk-)/ {print pre $0; next} {print}' \
  ~/.ssh/authorized_keys.bak-keylog > ~/.ssh/authorized_keys.new &&
  chmod 600 ~/.ssh/authorized_keys.new && mv ~/.ssh/authorized_keys.new ~/.ssh/authorized_keys
grep -c ssh-keylog ~/.ssh/authorized_keys                # 1
```

Only a line that starts with the key type is changed. A line that already has options (`command=`, `restrict`,
`from=`) is left alone: that key is limited already, or needs its options merged by hand (comma-separated, no
spaces: `command="/usr/local/bin/ssh-keylog laptop",from="192.0.2.0/24" ssh-ed25519 ...`).

Only OpenSSH reads `authorized_keys`. A login through **Tailscale SSH** (port 22 on a tailnet box with
`tailscale up --ssh`) never passes through it, so it is not logged: only keys used over OpenSSH (port 2222 on
the kit's servers, or a box without Tailscale SSH) show up.

## Check it

From the machine that holds the key, open a fresh connection each time (not a shared one):

```sh
ssh -o ControlPath=none <box> 'echo cmd-ok'                       # a command
ssh -o ControlPath=none -t <box> 'echo tty-ok'                    # a terminal
echo 'ls /etc/hostname' | sftp -q -o ControlPath=none -b - <box>   # sftp (and scp, which uses it)
rsync -e 'ssh -o ControlPath=none' <box>:/etc/hostname /tmp/      # rsync
ssh <box> 'journalctl -q -t ssh-keylog --since -5min -o cat'      # one line per login above
```

If any of them fails, put the backup back: `cp -p ~/.ssh/authorized_keys.bak-keylog ~/.ssh/authorized_keys`.

## Read the log

```sh
servers/tools/ssh-keylog/ssh-keylog-report                       # this machine, last 7 days
servers/tools/ssh-keylog/ssh-keylog-report --since -14d thor heimdall loki
```

It reads each host's journal as you, or with `sudo -n`, so your user needs to be in the `systemd-journal` (or
`adm`) group there, or have passwordless sudo. Each line: how many times, the key's label, the client address,
the program (`rsync`, `restic`, `sftp-server`, `<shell>` for an interactive login). Commands are cut at 160
characters and long tokens become `<redacted>` before they reach the journal, but treat the log as sensitive
anyway: it shows what runs where.

## What comes next (by hand, when you are ready)

1. **Job keys.** After a week or two, every recurring pattern in the report gets its own key that can do only that
   job, for example `restrict,from="<caller address>",command="rrsync -wo /srv/backups/laptop" ssh-ed25519 ...`.
   Point the job (its unit, script or `~/.ssh/config` Host alias with `IdentityFile`) at the new key and check
   its next run.
2. **Keep the all-access key away from unattended jobs.** Interactive sessions keep the full key. Unattended
   units on your laptop get a systemd drop-in with `InaccessiblePaths=%h/.ssh/id_ed25519`, so they can only use
   their job keys.
3. **Remove what is unused.** A key with no lines in a month is a key you can delete. When every key is either a
   job key or your interactive key, remove the `ssh-keylog` prefixes.

## Undo

Delete the `command="/usr/local/bin/ssh-keylog <label>" ` prefix from the key line (or restore
`~/.ssh/authorized_keys.bak-keylog`), then `sudo rm /usr/local/bin/ssh-keylog`. Remove the prefixes before the
script: a key whose forced command is missing cannot log in at all.
