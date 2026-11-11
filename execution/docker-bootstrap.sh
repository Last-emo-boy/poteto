#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable' > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce=5:28.5.0-1~ubuntu.24.04~noble docker-ce-cli=5:28.5.0-1~ubuntu.24.04~noble containerd.io=2.4.2-3~ubuntu.24.04~noble docker-buildx-plugin=0.38.1-1~ubuntu.24.04~noble docker-compose-plugin=5.7.0-1~ubuntu.24.04~noble
usermod -aG docker ubuntu
systemctl enable --now docker
