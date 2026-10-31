#!/usr/bin/env bash
# Rescan commits before they are pushed (r1 #2): author and committer emails, messages, and every file each commit
# adds or changes, with the CURRENT rules (the private rules can grow after a commit was made). Same checks as the
# pre-commit hook: tools/scrub.py --check (plus the guard denylist when configured) and gitleaks.
#   tools/check-commits.sh origin/main..HEAD          # any `git rev-list` arguments
# tools/githooks/pre-push runs it on the commits a push sends that the remote does not have yet.
# Exit 0 clean, 1 hits, 2 usage error. Fix a hit in an unpublished commit by rewriting that commit, never by a
# follow-up commit (the old blob would still ship in the history).
set -euo pipefail
fail() { echo "[kit check-commits] BLOCKED: $*" >&2; exit 1; }
[[ $# -gt 0 ]] || { echo "usage: tools/check-commits.sh <git rev-list arguments, e.g. origin/main..HEAD>" >&2; exit 2; }
top=$(git rev-parse --show-toplevel)
list=$(git rev-list "$@") || { echo "[kit check-commits] git rev-list $* failed" >&2; exit 2; }
[[ -n $list ]] || { echo "[kit check-commits] no new commits to check" >&2; exit 0; }

tmp=$(mktemp -d "${TMPDIR:-/tmp}/kit-commits.XXXXXX")
trap 'rm -rf -- "${tmp:?}"' EXIT
mkdir -p "$tmp/scan/messages"
bad=0 count=0
for c in $list; do
  count=$((count + 1))
  short=${c:0:12}
  emails=$(git log -1 --format='%ae%n%ce' "$c")
  [[ ${emails%%$'\n'*} == *@users.noreply.github.com ]] || { echo "    $short: author email is not a GitHub noreply address" >&2; bad=1; }
  [[ ${emails#*$'\n'} == *@users.noreply.github.com ]] || { echo "    $short: committer email is not a GitHub noreply address" >&2; bad=1; }
  git log -1 --format=%B "$c" > "$tmp/scan/messages/$short"
  # The files as this commit has them; symlinks come out as plain files holding their target.
  GIT_INDEX_FILE="$tmp/index" git read-tree "$c"
  git diff-tree -r --root -m --no-commit-id --name-only -z --diff-filter=ACMRT "$c" | sort -zu |
    GIT_INDEX_FILE="$tmp/index" git -c core.symlinks=false checkout-index --prefix="$tmp/scan/files/$short/" -z --stdin
done

guard=${GUARD_DENYLIST_FILE:-$(git config --get kit.guardDenylist || true)}
check=(python3 "$top/tools/scrub.py" --check "$tmp/scan")
if [[ -n $guard ]]; then
  owner=$(git remote get-url origin 2>/dev/null | sed -E 's#^.*github\.com[:/]##; s#\.git$##' || true)
  check+=(--guard-denylist "${guard/#\~/$HOME}" --owner-repo "$owner")
else
  echo "[kit check-commits] guard denylist not configured (git config kit.guardDenylist <file>); skipped" >&2
fi
"${check[@]}" >&2 || bad=1
command -v gitleaks >/dev/null || fail "gitleaks is not installed (fail closed)"
gitleaks git --log-opts="$*" --no-banner --redact --log-level warn "$top" >&2 || bad=1

[[ $bad -eq 0 ]] || fail "$count commit(s) checked; fix the items above by rewriting the unpublished commits"
echo "[kit check-commits] clean ($count commit(s))" >&2
