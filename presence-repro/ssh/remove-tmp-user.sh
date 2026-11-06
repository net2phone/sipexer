#!/bin/bash
# Removes the temporary presence debug user and everything create-tmp-user.sh added.
# Run as root:  sudo bash remove-tmp-user.sh
set -eu
USERNAME="${USERNAME:-tmp_claude_presence}"
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

# --- rollback: same function as in create-tmp-user.sh ---
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

rollback "$USERNAME"
echo "done: $USERNAME and its sudo/sshd settings are gone"
