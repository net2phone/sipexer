#!/bin/bash
# Shared SSH connection to the load-generator host (same user/password as presence-login.sh, read from
# credential-presence.properties only by the askpass helper; never printed).
#
# usage: loadgen-login.sh                      open the connection (password used once, via askpass)
#        loadgen-login.sh --check              show whether it is open
#        loadgen-login.sh --exit               close it
#        loadgen-login.sh --run '<cmd>'        run a command on the host
#        loadgen-login.sh --put <local> <remote>   copy a file or directory to the host (scp -r)
#        loadgen-login.sh --get <remote> <local>   copy a file from the host
# LOADGEN_HOST=<ip> overrides the host, PRESENCE_PROPS=<file> the properties file.
set -eu
umask 077

HOST_IP="${LOADGEN_HOST:-10.94.48.227}"
ALIAS="loadgen-6071"
PROPS="${PRESENCE_PROPS:-$(cd "$(dirname "$0")" && pwd)/credential-presence.properties}"
[ -f "$PROPS" ] || { echo "missing $PROPS"; exit 1; }
USERNAME=$(grep -m1 "^username=" "$PROPS" | cut -d= -f2- || true)
[ -n "$USERNAME" ] || { echo "username must be set in $PROPS"; exit 1; }

SOCK_DIR="$HOME/.ssh/cm"; mkdir -p "$SOCK_DIR"
OPTS=(-o "HostName=$HOST_IP" -o "User=$USERNAME" -o PubkeyAuthentication=no
      -o PreferredAuthentications=keyboard-interactive,password -o NumberOfPasswordPrompts=1
      -o ControlMaster=auto -o "ControlPath=$SOCK_DIR/%r@%h:%p" -o ControlPersist=12h
      -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30)

open_conn() {
  if ssh "${OPTS[@]}" -O check "$ALIAS" 2>/dev/null; then echo "$ALIAS: already connected"; return 0; fi
  local ask; ask=$(mktemp)
  printf '#!/bin/sh\ngrep -m1 "^password=" "%s" | cut -d= -f2- | tr -d "\\n"\n' "$PROPS" > "$ask"; chmod 700 "$ask"
  if SSH_ASKPASS="$ask" SSH_ASKPASS_REQUIRE=force DISPLAY=none ssh "${OPTS[@]}" -fN "$ALIAS" </dev/null >/dev/null 2>&1; then
    rm -f "$ask"; echo "$ALIAS: connected ($HOST_IP)"
  else
    rm -f "$ask"; echo "$ALIAS: login failed ($HOST_IP)"; return 1
  fi
}

case "${1:-}" in
  --check) ssh "${OPTS[@]}" -O check "$ALIAS" 2>/dev/null && echo "$ALIAS: open" || echo "$ALIAS: not connected" ;;
  --exit)  ssh "${OPTS[@]}" -O exit "$ALIAS" 2>/dev/null && echo "$ALIAS: closed" || echo "$ALIAS: not connected" ;;
  --run)   shift; open_conn >/dev/null; ssh "${OPTS[@]}" -o ControlMaster=no -o BatchMode=yes "$ALIAS" "$@" ;;
  --put)   open_conn >/dev/null; scp -r -q "${OPTS[@]}" -o ControlMaster=no -o BatchMode=yes "$2" "$ALIAS:$3" ;;
  --get)   open_conn >/dev/null; scp -r -q "${OPTS[@]}" -o ControlMaster=no -o BatchMode=yes "$ALIAS:$2" "$3" ;;
  *)       open_conn ;;
esac
