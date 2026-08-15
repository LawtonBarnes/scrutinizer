#!/bin/bash
# sync-fleet.sh -- pull the latest code for every app on MP + each puppet
# and restart whatever actually changed. Run from PR (or anywhere with
# SSH access to the fleet using the p1-p4/mp aliases in ~/.ssh/config --
# see project_naming_conventions). Puppets restart their currently
# assigned app via STRINGS's /restart endpoint; MP restarts SCRUTE
# itself using the safe kill-and-let-relaunch procedure documented in
# project_health_monitor's restart-hazard note (kill only the real
# python3 process, never the whole console-attach chain, since a plain
# `pkill -f scrutinizer.py` there caused a double-process bug once).
#
# Usage: ./sync-fleet.sh
set -uo pipefail

sync_repo() {
    local host="$1" dir="$2"
    ssh "$host" "test -d '$dir/.git'" || return 1
    local before after
    before=$(ssh "$host" "git -C '$dir' rev-parse HEAD")
    ssh "$host" "git -C '$dir' pull --ff-only" > /dev/null 2>&1
    after=$(ssh "$host" "git -C '$dir' rev-parse HEAD")
    if [ "$before" != "$after" ]; then
        echo "  $dir: $before -> $after (updated)"
        return 0
    else
        echo "  $dir: unchanged"
        return 2
    fi
}

echo "=== MP (masterofpuppets) ==="
if sync_repo mp /opt/scrutinizer; then
    echo "  scrutinizer changed -- restarting SCRUTE"
    pid=$(ssh mp "pgrep -f '^python3 /opt/scrutinizer/scrutinizer\\.py\$'" || true)
    if [ -n "$pid" ]; then
        ssh mp "sudo kill -TERM $pid"
        sleep 2
        # If nothing came back on its own (no getty auto-respawn this
        # time), force a relaunch -- same fallback used throughout the
        # SCRUTE work this session.
        if ! ssh mp "pgrep -f '^python3 /opt/scrutinizer/scrutinizer\\.py\$'" > /dev/null; then
            ssh mp "setsid -f /usr/local/bin/scrutinizer > /tmp/scrutinizer.log 2>&1 < /dev/null"
        fi
    fi
fi

# curl needs a real IP -- SSH aliases (p1-p4 in ~/.ssh/config) only
# resolve for the `ssh` command itself, not arbitrary HTTP requests.
declare -A PUPPET_IPS=(
    [p1]=192.168.68.72
    [p2]=192.168.68.65
    [p3]=192.168.68.68
    [p4]=192.168.68.64
)

for p in p1 p2 p3 p4; do
    echo "=== $p ==="
    ip="${PUPPET_IPS[$p]}"
    status=$(curl -s --max-time 3 "http://$ip:8420/status" 2>/dev/null)
    app=$(echo "$status" | python3 -c "import sys,json; print(json.load(sys.stdin).get('app') or '')" 2>/dev/null)
    if [ -z "$app" ]; then
        echo "  no app assigned or STRINGS unreachable, skipping"
        continue
    fi
    if sync_repo "$p" "/opt/$app"; then
        echo "  $app changed -- restarting via STRINGS"
        curl -s -X POST "http://$ip:8420/restart" > /dev/null
    fi
done

echo "=== sync complete ==="
