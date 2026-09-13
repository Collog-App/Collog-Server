#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

[[ $(id -u) == 0 ]] || { echo 'Run as root'; exit 1; }
source /etc/os-release
[[ "$ID" == ubuntu && ( "$VERSION_ID" == 24.04 || "$VERSION_ID" == 26.04 ) && $(uname -m) == x86_64 ]] || {
    echo 'Ubuntu 24.04 or 26.04 x86_64 is required'
    exit 1
}
script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl unzip
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $VERSION_CODENAME
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

if ! command -v aws >/dev/null; then
    temporary=$(mktemp -d)
    trap 'rm -rf "$temporary"' EXIT
    curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o "$temporary/aws.zip"
    unzip -q "$temporary/aws.zip" -d "$temporary"
    "$temporary/aws/install"
    rm -rf "$temporary"
    trap - EXIT
fi
if ! snap list amazon-ssm-agent >/dev/null 2>&1; then snap install amazon-ssm-agent --classic; fi
systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service

if [[ ! -e /swapfile ]] && [[ -z $(swapon --show --noheadings) ]]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    printf '/swapfile none swap sw 0 0\n' >> /etc/fstab
fi
umask 077
install -d -m 0700 /etc/collog /opt/collog /opt/collog/bin /opt/collog/releases /opt/collog/backups
install -m 0750 "$script_directory/deploy.sh" /opt/collog/bin/deploy.sh
cat > /etc/collog/deploy.conf <<'EOF'
AWS_ACCOUNT_ID=534545247934
AWS_REGION=ap-northeast-2
ECR_REPOSITORY=collog-server
EOF
cat > /opt/collog/bin/start.sh <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
exec 9>/run/lock/collog-deploy.lock
flock 9
[[ -d /opt/collog/current ]] || exit 0
docker compose --project-name collog-production --env-file /etc/collog/backend.env \
    --env-file /opt/collog/current/image.env -f /opt/collog/current/docker-compose.yml \
    up -d --no-build --wait --wait-timeout 180
EOF
chmod 0750 /opt/collog/bin/start.sh
cat > /etc/systemd/system/collog.service <<'EOF'
[Unit]
Description=Collog application containers
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/collog/bin/start.sh
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable collog.service
echo 'EC2 runtime ready'
