#!/bin/bash
# Creates a temporary, limited login user for presence debugging on a Debian host.
# Every run first rolls back what an earlier run added, then sets everything up from scratch.
# Run as root on each presence node:  sudo bash create-tmp-user.sh
# The script asks for the password (typed twice, not shown); it is not in this file.
#
# Settings (override with env, e.g. sudo EXPIRE=2026-11-12 SUDO_MODE=none bash create-tmp-user.sh)
#   USERNAME     login name                         default tmp_claude_presence
#   EXPIRE       account expiry date (YYYY-MM-DD)   default today + 7 days
#   SUDO_MODE    none | readonly                    default readonly
#   KAMCMD_RPCS  kamcmd RPCs allowed via sudo        default: read-only list below
#   SUDO_CMDS    extra full paths allowed with any arguments, e.g. "/usr/sbin/kamctl" (careful: write access)
#   SSH_PASSWORD yes = sshd Match block for this user: password login, no forwarding
#   ALLOW_RESTART yes = also allow exactly "systemctl restart kamailio" via sudo   default yes
set -eu

USERNAME="${USERNAME:-tmp_claude_presence}"
EXPIRE="${EXPIRE:-$(date -d '+7 days' +%F)}"
SUDO_MODE="${SUDO_MODE:-readonly}"
SSH_PASSWORD="${SSH_PASSWORD:-yes}"
ALLOW_RESTART="${ALLOW_RESTART:-yes}"
KAMCMD_RPCS="${KAMCMD_RPCS:-core.uptime core.version core.psx core.shmmem core.tcp_info pkg.stats stats.get_statistics stats.fetch tm.stats sl.stats dmq.list_nodes htable.listTables htable.stats dispatcher.list presence.presentity_list}"
CFG=/etc/ssh/sshd_config
SSHD_DROPIN="/etc/ssh/sshd_config.d/90-${USERNAME}.conf"
SUDOERS_FILE="/etc/sudoers.d/90-${USERNAME}"

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
[ -t 0 ] || { echo "run from a terminal - the script asks for the password"; exit 1; }

# --- rollback: same function as in remove-tmp-user.sh ---
rollback() {
  local u="$1" changed=no cfg=/etc/ssh/sshd_config
  pkill -u "$u" 2>/dev/null || true                 # end open sessions of this user
  rm -f "/etc/sudoers.d/90-$u" "/etc/sudoers.d/91-$u-restart"
  if [ -f "/etc/ssh/sshd_config.d/90-$u.conf" ]; then rm -f "/etc/ssh/sshd_config.d/90-$u.conf"; changed=yes; fi
  if grep -q "^# BEGIN $u " "$cfg"; then
    cp -p "$cfg" "$cfg.bak-remove-$u"
    # delete the marked block and the empty line put before it
    awk -v b="# BEGIN $u " -v e="# END $u" '
      index($0, b) == 1 { skip = 1; held = 0; next }
      skip && $0 == e   { skip = 0; next }
      skip              { next }
      { if (held) print ""; held = 0; if ($0 == "") { held = 1; next } print }
      END { if (held) print "" }' "$cfg.bak-remove-$u" > "$cfg"
    changed=yes
  fi
  if [ "$changed" = yes ]; then
    if sshd -t; then
      systemctl reload ssh 2>/dev/null || systemctl reload sshd
      echo "rollback: sshd block for $u removed"
    else
      echo "rollback: sshd config test failed - restoring $cfg"
      cp -p "$cfg.bak-remove-$u" "$cfg"; return 1
    fi
  fi
  rm -f "$cfg.bak-remove-$u" "$cfg.bak-$u"
  if id "$u" >/dev/null 2>&1; then
    userdel --remove "$u" 2>/dev/null || userdel "$u"
    echo "rollback: user $u removed"
  fi
}

# 0. undo any earlier run
rollback "$USERNAME"

# 1. user (no sudo group, bash shell, home dir)
useradd --create-home --shell /bin/bash --comment "temporary presence debug (Claude)" "$USERNAME"
echo "created user $USERNAME"

# 2. read access to logs: adm = /var/log files, systemd-journal = journalctl
for g in adm systemd-journal; do
  if getent group "$g" >/dev/null; then usermod -aG "$g" "$USERNAME"; echo "added to group $g"; fi
done

# 3. expiry: the account stops working after this date
chage -E "$EXPIRE" "$USERNAME"
echo "account expires $EXPIRE"

# 4. password: typed twice, not shown, passed to chpasswd on stdin (not in arguments)
while true; do
  read -rsp "password for $USERNAME: " PW1; echo
  read -rsp "repeat password: " PW2; echo
  if [ -z "$PW1" ]; then echo "empty password - try again"; continue; fi
  if [ "$PW1" != "$PW2" ]; then echo "passwords differ - try again"; continue; fi
  break
done
# "BAD PASSWORD ... error loading dictionary" only means cracklib has no dictionary; root can still set it
printf '%s:%s\n' "$USERNAME" "$PW1" | chpasswd
unset PW1 PW2
if [ "$(passwd -S "$USERNAME" | awk '{print $2}')" = "P" ]; then echo "password set"; else echo "password NOT set"; exit 1; fi

# 5. sshd: password login for this user, no forwarding
if [ "$SSH_PASSWORD" = yes ]; then
  BLOCK="Match User $USERNAME
    PasswordAuthentication yes
    AllowTcpForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no"
  if grep -qsE '^\s*Include\s+/etc/ssh/sshd_config.d/\*\.conf' "$CFG"; then
    printf '# temporary - remove with remove-tmp-user.sh\n%s\n' "$BLOCK" > "$SSHD_DROPIN"
    chmod 644 "$SSHD_DROPIN"
    sshd -t || { echo "sshd config test failed - removing $SSHD_DROPIN"; rm -f "$SSHD_DROPIN"; exit 1; }
  else
    # no sshd_config.d include on this host: append a marked block at the end (Match must be last), keep a backup
    cp -p "$CFG" "$CFG.bak-$USERNAME"
    printf '\n# BEGIN %s (temporary - remove with remove-tmp-user.sh)\n%s\n# END %s\n' "$USERNAME" "$BLOCK" "$USERNAME" >> "$CFG"
    sshd -t || { echo "sshd config test failed - restoring $CFG"; cp -p "$CFG.bak-$USERNAME" "$CFG"; exit 1; }
    echo "appended Match block to $CFG (backup $CFG.bak-$USERNAME)"
  fi
  systemctl reload ssh 2>/dev/null || systemctl reload sshd
  echo "sshd: Match block for $USERNAME active (password login, no forwarding)"
fi

# 6. sudo: read-only commands only, no password
if [ "$SUDO_MODE" = readonly ]; then
  CMDS=""
  # kamcmd only with the listed read-only RPCs (plain kamcmd opens a console that can change state)
  KAMCMD=$(command -v kamcmd 2>/dev/null || true)
  if [ -n "$KAMCMD" ]; then
    for r in $KAMCMD_RPCS; do CMDS="${CMDS:+$CMDS, }$KAMCMD $r, $KAMCMD $r *"; done
  else
    echo "sudo: kamcmd not found - skipped"
  fi
  # restart of kamailio only: exact command, no other systemctl actions or units
  if [ "$ALLOW_RESTART" = yes ]; then
    SYSTEMCTL=$(command -v systemctl 2>/dev/null || echo /usr/bin/systemctl)
    CMDS="${CMDS:+$CMDS, }$SYSTEMCTL restart kamailio, $SYSTEMCTL restart kamailio.service"
  fi
  for c in journalctl ss ${SUDO_CMDS:-}; do
    p=$(command -v "$c" 2>/dev/null || true)
    if [ -z "$p" ] && [ -x "$c" ]; then p="$c"; fi
    if [ -n "$p" ]; then CMDS="${CMDS:+$CMDS, }$p"; else echo "sudo: $c not found - skipped"; fi
  done
  if [ -n "$CMDS" ]; then
    printf '# temporary - remove with remove-tmp-user.sh\n%s ALL=(root) NOPASSWD: %s\n' "$USERNAME" "$CMDS" > "$SUDOERS_FILE"
    chmod 440 "$SUDOERS_FILE"
    visudo -cf "$SUDOERS_FILE" || { echo "sudoers check failed - removing $SUDOERS_FILE"; rm -f "$SUDOERS_FILE"; exit 1; }
    echo "sudo: NOPASSWD for kamcmd RPCs [$KAMCMD_RPCS], journalctl, ss ${SUDO_CMDS:-}"
    if [ "$ALLOW_RESTART" = yes ]; then echo "sudo: NOPASSWD for '$SYSTEMCTL restart kamailio' (exact command only)"; fi
  else
    echo "sudo: no commands found - no sudo rule written"
  fi
fi

echo
echo "done. check:"
echo "  id $USERNAME; chage -l $USERNAME | grep 'Account expires'"
echo "  sshd -T -C user=$USERNAME,host=x,addr=127.0.0.1 | grep -Ei '^(passwordauthentication|allowtcpforwarding|x11forwarding) '"
echo "  sudo -l -U $USERNAME"
