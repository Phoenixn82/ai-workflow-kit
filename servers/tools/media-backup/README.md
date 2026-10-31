# media-backup: nightly restic copy of your photos and videos to a second drive

A root timer (03:40, wakes a sleeping thor) that snapshots your media folders with restic onto another drive in the
same box, dumps Immich's database first when Immich runs there, prunes to 7 daily, 8 weekly and 12 monthly
snapshots, checks the repository, and clears stale locks left by a run that a reboot or suspend killed.

| File | Goes to |
|---|---|
| `media-backup` | `/usr/local/sbin/media-backup` (mode 755) |
| `media-backup.conf.example` | `/etc/homeserver/media-backup.conf` (your folders, the second drive) |
| `media-backup.service`, `media-backup.timer` | `/etc/systemd/system/` |
| `test.sh` | stays here: an offline run against a throwaway repository (needs restic) |

Install, check, restore and undo: [profiles/thor](../../../profiles/thor/README.md#media-backup-to-a-second-drive).
Quick check after install: `sudo media-backup --check`. Kill switch: `sudo touch /etc/media-backup.disable`.
