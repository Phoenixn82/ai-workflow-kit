#!/usr/bin/env bash
# Undo courier/install-guards.sh for this user: exactly what its manifest (~/.local/share/web/guards-manifest.json)
# records, and nothing else. Registrations the install added are removed (a registration that was already there
# stays); files it created are deleted and files it replaced are restored from their backups, but only while they
# still hold what the install wrote; the shared-log patch is restored from its backup, or reversed if the file was
# edited since; directories it created go if empty. Whatever changed since the install is left alone with a WARNING
# (exit 1) and stays in the manifest for a re-run; the hook script stays while a registration of it does. With no
# manifest (a guard installed before it existed): deregister, delete the guard's files only where byte-identical
# to the repo copies, reverse the shared-log patch. It never touches ~/.codex/config.toml (a stale hook-trust entry
# there is harmless). The courier itself (system user, units, store) is not touched.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
G="$HERE/guards"
STAMP=$(date +%Y%m%d-%H%M%S)
exec python3 "$G/manifest.py" "$HOME/.local/share/web/guards-manifest.json" "$STAMP" undo \
    "$G/shared-log-redaction.patch"
