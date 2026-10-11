#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo 'The launcher delegates cgroups as root, then executes research as research-runner.' >&2
  exit 1
fi
bash "$(dirname "$0")/setup_adaptive_cgroup.sh"
target=/sys/fs/cgroup/popper-adaptive/controller-host
mkdir -p "$target"
chown research-runner:research-runner "$target" "$target/cgroup.procs"
# Non-root moves may cross only a delegated common ancestor. Start the host in
# a sibling leaf so every candidate can enter its bounded leaf without privilege.
printf '%s' "$$" > "$target/cgroup.procs"
exec runuser -u research-runner -- "$@"
