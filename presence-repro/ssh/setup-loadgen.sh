#!/bin/bash
# One-time setup of the SIP load-generator host (Debian 13, arm64) for the 6071 presence tests.
# Run ON THE INSTANCE as root:  sudo bash setup-loadgen.sh
# Asks for the password of the test user (typed twice, not shown); use the same one as in
# credential-presence.properties so loadgen-login.sh can log in.
set -eu
USERNAME="${USERNAME:-tmp_claude_presence}"
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo bash $0)"; exit 1; }
[ -t 0 ] || { echo "run from a terminal - the script asks for the password"; exit 1; }

# 1. packages: python3 for pod-repro, tools for checks
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3 procps iproute2 netcat-openbsd sudo >/dev/null
echo "packages ok: $(python3 --version)"

# 2. user with password login
id "$USERNAME" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$USERNAME"
while true; do
  read -rsp "password for $USERNAME: " PW1; echo
  read -rsp "repeat password: " PW2; echo
  [ -n "$PW1" ] && [ "$PW1" = "$PW2" ] && break
  echo "empty or different - try again"
done
printf '%s:%s\n' "$USERNAME" "$PW1" | chpasswd; unset PW1 PW2
echo "user $USERNAME ready"

# 3. password SSH for this user (the cloud image may switch password login off globally)
mkdir -p /etc/ssh/sshd_config.d
printf 'Match User %s\n    PasswordAuthentication yes\n' "$USERNAME" > /etc/ssh/sshd_config.d/90-loadgen.conf
sshd -t && systemctl reload ssh
echo "ssh: password login on for $USERNAME"

# 4. sudo only for sysctl (UDP buffer checks/changes)
echo "$USERNAME ALL=(root) NOPASSWD: /usr/sbin/sysctl" > /etc/sudoers.d/90-loadgen
chmod 440 /etc/sudoers.d/90-loadgen; visudo -cf /etc/sudoers.d/90-loadgen

# 5. kernel tuning for ~20k UDP packets/s and 300k+ SIP dialogs
cat > /etc/sysctl.d/90-loadgen.conf <<EOF
net.core.rmem_max = 33554432
net.core.wmem_max = 33554432
net.core.rmem_default = 8388608
net.core.wmem_default = 8388608
net.core.netdev_max_backlog = 50000
net.ipv4.udp_mem = 262144 524288 1048576
fs.file-max = 2097152
EOF
sysctl -q --system
echo "sysctl: rmem_max $(sysctl -n net.core.rmem_max), rmem_default $(sysctl -n net.core.rmem_default)"

# 6. open files for the test user
printf '%s soft nofile 1048576\n%s hard nofile 1048576\n' "$USERNAME" "$USERNAME" > /etc/security/limits.d/90-loadgen.conf

# 7. work directory
install -d -o "$USERNAME" -g "$USERNAME" "/home/$USERNAME/loadgen"
echo "done. Check from the Mac: presence-rig/ssh/loadgen-login.sh --run 'id; ulimit -n; nproc; free -g'"
