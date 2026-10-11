#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run the one-time cgroup delegation as root; research runs remain non-root.' >&2
  exit 1
fi
target=/sys/fs/cgroup/popper-adaptive
mkdir -p "$target"
printf '%s' '+cpu +memory +pids' > "$target/cgroup.subtree_control"
chown research-runner:research-runner "$target" "$target/cgroup.procs" "$target/cgroup.subtree_control"
