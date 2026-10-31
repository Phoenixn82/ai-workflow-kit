# Sourced (as BASH_ENV) by every command install.sh and doctor.sh run. lib.py sets the switches; nothing else here.

# Root without a sudo binary (a fresh container): run the sudo'd command directly.
if [ -n "${KIT_ROOT_NO_SUDO:-}" ]; then
  sudo() { while [ $# -gt 0 ] && [ "${1#-}" != "$1" ]; do shift; done; "$@"; }
fi

# Right after Docker is installed you are in the docker group on disk but not in this login yet, so plain `docker`
# is refused until you log in again. sg picks the group up for one command without a new login.
if [ -n "${KIT_DOCKER_SG:-}" ]; then
  docker() { sg docker -c "$(printf '%q ' docker "$@")"; }
fi
