#!/bin/bash
# Замер сети раз в 15 секунд (launchd: com.iykisoba.netwatch).
# В лог пишет только изменения: когда что-то упало и когда поднялось.
LOG="$HOME/Library/Logs/netwatch.log"
STATE="$HOME/Library/Logs/.netwatch.state"
HOST=devml4dbs.duckdns.org
IP=151.241.221.245

ok() { "$@" >/dev/null 2>&1 && echo ok || echo FAIL; }
resolves() { dig +short +time=2 +tries=1 "$HOST" "$@" 2>/dev/null | grep -q '^[0-9]' && echo ok || echo FAIL; }

gw=$(route -n get default 2>/dev/null | awk '/gateway:/{print $2}')
state="iface=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
state+=" route_1.1.1.1=$(route -n get 1.1.1.1 2>/dev/null | awk '/interface:/{print $2}')"
state+=" dns_sys=$(resolves) dns_router=$(resolves @"$gw") dns_1.1.1.1=$(resolves @1.1.1.1)"
state+=" tcp22=$(ok nc -z -G 3 $IP 22) tcp443=$(ok nc -z -G 3 $IP 443) tcp2053=$(ok nc -z -G 3 $IP 2053)"
state+=" ssh=$(ok ssh -o BatchMode=yes -o ConnectTimeout=5 -o ClearAllForwardings=yes dnk true)"
state+=" web=$(ok curl -s -m 5 --noproxy '*' -o /dev/null https://www.google.com)"

if [ "$state" != "$(cat "$STATE" 2>/dev/null)" ]; then
  echo "$(date '+%F %T') $state" >> "$LOG"
  echo "$state" > "$STATE"
fi
