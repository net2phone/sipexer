#!/bin/bash
# Opens shared SSH connections (ControlMaster) to the presence nodes listed in
# credential-presence.properties, so later "ssh <alias> '<cmd>'" runs need no password.
# The password goes to ssh only through SSH_ASKPASS; it is never printed or put in arguments.
#
# usage: presence-login.sh                 open the connections (password used once, via askpass)
#        presence-login.sh --check         show which connections are open
#        presence-login.sh --exit          close the connections
#        presence-login.sh --run <alias> '<cmd>'   run a command over an open connection
# PRESENCE_PROPS=<file> overrides the properties file (default: next to this script)
set -eu
umask 077

ACTION=open
case "${1:-}" in
  --exit) ACTION=exit; shift ;;
  --check) ACTION=check; shift ;;
  --run) ACTION=run; RUN_ALIAS="${2:?alias}"; shift 2 ;;
esac
PROPS="${PRESENCE_PROPS:-$(cd "$(dirname "$0")" && pwd)/credential-presence.properties}"
[ -f "$PROPS" ] || { echo "missing $PROPS (copy credential-presence.properties.sample)"; exit 1; }

prop() { grep -m1 "^$1=" "$PROPS" | cut -d= -f2- || true; }
USERNAME=$(prop username)
HOSTS=$(prop hosts)
JUMP=$(prop jump)
PERSIST=$(prop persist); PERSIST=${PERSIST:-8h}
[ -n "$USERNAME" ] && [ -n "$HOSTS" ] || { echo "username and hosts must be set in $PROPS"; exit 1; }

SOCK_DIR="$HOME/.ssh/cm"; mkdir -p "$SOCK_DIR"
CFG=$(mktemp); ASK=$(mktemp)
trap 'rm -f "$CFG" "$ASK"' EXIT
# askpass helper: prints only the password, to ssh, never to the terminal
printf '#!/bin/sh\ngrep -m1 "^password=" "%s" | cut -d= -f2- | tr -d "\\n"\n' "$PROPS" > "$ASK"
chmod 700 "$ASK"

for pair in $HOSTS; do
  alias=${pair%%=*}; ip=${pair#*=}
  opts=(-o "HostName=$ip" -o "User=$USERNAME" -o PubkeyAuthentication=no
        -o PreferredAuthentications=keyboard-interactive,password -o NumberOfPasswordPrompts=1
        -o ControlMaster=auto -o "ControlPath=$SOCK_DIR/%r@%h:%p" -o "ControlPersist=$PERSIST"
        -o StrictHostKeyChecking=accept-new)
  [ -n "$JUMP" ] && opts+=(-J "$JUMP")
  if [ "$ACTION" = run ]; then
    [ "$alias" = "$RUN_ALIAS" ] || continue
    ssh "${opts[@]}" -o ControlMaster=no -o BatchMode=yes "$alias" "$@"
    exit $?
  fi
  if [ "$ACTION" = check ]; then
    ssh "${opts[@]}" -O check "$alias" 2>/dev/null && echo "$alias: open" || echo "$alias: not connected"
    continue
  fi
  if [ "$ACTION" = exit ]; then
    ssh "${opts[@]}" -O exit "$alias" 2>/dev/null && echo "$alias: closed" || echo "$alias: not connected"
    continue
  fi
  if ssh "${opts[@]}" -O check "$alias" 2>/dev/null; then echo "$alias: already connected"; continue; fi
  if SSH_ASKPASS="$ASK" SSH_ASKPASS_REQUIRE=force DISPLAY=none \
       ssh "${opts[@]}" -fN "$alias" </dev/null >/dev/null 2>&1; then
    echo "$alias: connected ($ip, open for $PERSIST)"
  else
    echo "$alias: login failed ($ip)"
  fi
done
[ "$ACTION" = run ] && { echo "unknown alias $RUN_ALIAS (see hosts= in $PROPS)"; exit 1; }
[ "$ACTION" = open ] && echo "run commands with: $0 --run <alias> '<cmd>'"
exit 0
