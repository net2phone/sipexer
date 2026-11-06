#!/bin/bash
# Tune the load-generator host (Debian 13) for the 6071 SIP load tests. Run as root once.
set -eu
U="${USERNAME:-tmp_claude_presence}"
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

# UDP socket buffers (sipexer and pod-repro ask for 8 MB per socket; default cap is 208 KB),
# more packets queued per interface, more UDP memory
cat > /etc/sysctl.d/90-loadgen.conf <<EOF
net.core.rmem_max = 33554432
net.core.wmem_max = 33554432
net.core.rmem_default = 8388608
net.core.wmem_default = 8388608
net.core.netdev_max_backlog = 50000
net.ipv4.udp_mem = 262144 524288 1048576
net.ipv4.udp_rmem_min = 16384
net.ipv4.udp_wmem_min = 16384
EOF
sysctl -q --system

# open files for the test user (new logins)
printf '%s soft nofile 1048576\n%s hard nofile 1048576\n' "$U" "$U" > /etc/security/limits.d/90-loadgen.conf

# let the test user read and adjust kernel settings (sysctl only)
echo "$U ALL=(root) NOPASSWD: /usr/sbin/sysctl" > /etc/sudoers.d/91-loadgen-sysctl
chmod 440 /etc/sudoers.d/91-loadgen-sysctl
visudo -cf /etc/sudoers.d/91-loadgen-sysctl

echo "rmem_max $(sysctl -n net.core.rmem_max), rmem_default $(sysctl -n net.core.rmem_default), backlog $(sysctl -n net.core.netdev_max_backlog)"
echo "done"
