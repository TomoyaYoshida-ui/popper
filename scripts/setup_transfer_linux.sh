#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo 'Run system preparation as root; tests run separately as research-runner.' >&2; exit 1; fi
apt-get update
apt-get install -y ca-certificates curl git python3 python3-venv bubblewrap fonts-wqy-microhei fonts-wqy-zenhei
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
if [ "$ID" != ubuntu ] || [ "$VERSION_ID" != 24.04 ]; then echo 'Expected Ubuntu 24.04' >&2; exit 1; fi
cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $VERSION_CODENAME
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
if ! id research-runner >/dev/null 2>&1; then useradd --create-home --shell /bin/bash research-runner; fi
# No Docker group membership or TCP listener: Docker administration remains root-only.
if [ "$(ps -p 1 -o comm=)" = systemd ]; then systemctl enable --now docker; else service docker start; fi
# A bwrap-specific exception, never disable AppArmor globally.
if [ -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns ] &&
   [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns)" = 1 ]; then
    apt-get install -y apparmor
    cat >/etc/apparmor.d/popper-bwrap <<'EOF'
#include <tunables/global>
profile popper-bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/popper-bwrap>
}
EOF
    apparmor_parser -r /etc/apparmor.d/popper-bwrap
fi
mkdir -p /home/research-runner/transfer-validation
chown research-runner:research-runner /home/research-runner/transfer-validation
docker version
docker buildx version
docker compose version
dpkg-query -W docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin bubblewrap
