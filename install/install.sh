#!/usr/bin/env bash
# install.sh --profile <a[,b]> [--dry-run] [--yes] [--os arch|debian|macos] [--harness]
#            [--optional all|none|<id,...>] [--only <id,...>] [--ignore-hardware]
# Reads install/tools.json and profiles/<name>/profile.json, checks the OS and the hardware, prints the plan, asks
# once, then installs what is missing, merges agent rules (only with --harness), starts services and installs units.
# Never replaces one of your files without a timestamped backup beside it. Exit: 0 ok, 1 something failed,
# 2 bad arguments or a failed prerequisite. The contract: install/README.md.
set -uo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export PYTHONDONTWRITEBYTECODE=1

py_ok() { command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; }
if ! py_ok; then
  dry="" yes=""
  for a in "$@"; do case $a in --dry-run) dry=1 ;; --yes) yes=1 ;; esac; done
  if command -v pacman >/dev/null 2>&1; then cmd="sudo pacman -S --needed --noconfirm python"
  elif command -v apt-get >/dev/null 2>&1; then cmd="sudo apt-get update -qq && sudo apt-get install -y python3 python3-venv"
  elif command -v brew >/dev/null 2>&1; then cmd="brew install python"
  else cmd=""; fi
  echo "install.sh needs Python 3.10 or newer to read the manifest (it is the only thing it needs first)."
  [ -n "$cmd" ] || { echo "Install it from https://www.python.org, then run this again."; exit 2; }
  if [ -n "$dry" ]; then echo "would run: $cmd"; echo "Dry run: nothing installed; the plan needs Python first."; exit 2; fi
  if [ -z "$yes" ]; then
    [ -t 0 ] || { echo "no terminal to ask on; re-run with --yes to run: $cmd"; exit 2; }
    read -r -p "Run: $cmd ? [y/N] " ans
    case $ans in y|Y|yes) ;; *) echo "Nothing installed."; exit 2 ;; esac
  fi
  [[ $cmd == *sudo* ]] && echo "why sudo: Python is a system package"
  if [ "$(id -u)" = 0 ] && ! command -v sudo >/dev/null 2>&1; then cmd=${cmd//sudo /}; fi
  bash -c "$cmd" || { echo "installing Python failed"; exit 1; }
  py_ok || { echo "still no Python 3.10+ on PATH"; exit 2; }
fi
exec python3 "$here/lib.py" install "$@"
