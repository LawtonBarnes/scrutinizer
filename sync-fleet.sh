#!/bin/bash
# sync-fleet.sh -- pull the latest code for every app repo actually
# installed on MP + each puppet (not just whichever one is currently
# running) and restart whatever needs it. Run from PR (or anywhere with
# SSH access to the fleet using the p1-p4/mp aliases in ~/.ssh/config --
# see project_naming_conventions). Puppets restart their currently
# assigned app via STRINGS's /restart endpoint, but only if that
# specific app was among the ones that changed -- an installed-but-idle
# app (e.g. CHANNEL 38 sitting unused on a puppet currently running
# BARS) still gets pulled so it's ready the next time it's assigned,
# without needlessly restarting whatever IS currently on screen. MP
# restarts SCRUTE itself using the safe kill-and-let-relaunch procedure
# documented in project_health_monitor's restart-hazard note (kill only
# the real python3 process, never the whole console-attach chain, since
# a plain `pkill -f scrutinizer.py` there caused a double-process bug
# once) -- MP's other installed apps (bars/loudness/channel38, for
# LOCAL launching) never run persistently in the background there, so
# they're synced but never need a restart of their own.
#
# Usage: ./sync-fleet.sh
set -uo pipefail

APP_NAMES="bars loudness channel38 weatherstar"

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

# Which of APP_NAMES actually have a repo checked out on $host's /opt --
# discovered live rather than hardcoded, since the install set differs
# per host (e.g. WEATHERSTAR isn't installed anywhere yet) and changes
# over time as new apps get rolled out.
installed_apps_on() {
    local host="$1"
    local present
    present=$(ssh "$host" "ls /opt 2>/dev/null")
    for app in $APP_NAMES; do
        echo "$present" | grep -qx "$app" && echo "$app"
    done
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
for app in $(installed_apps_on mp); do
    # No restart needed -- these only ever run as a one-off foreground
    # launch from SCRUTE's LOCAL menu selection (subprocess.run, blocks
    # until the app exits), never persistently in the background on MP
    # the way STRINGS supervises them on a puppet. Syncing just means
    # the next LOCAL launch picks up the latest code.
    sync_repo mp "/opt/$app" > /dev/null
    echo "  $app: synced (no restart needed on MP)"
done

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
    current_app=$(echo "$status" | python3 -c "import sys,json; print(json.load(sys.stdin).get('app') or '')" 2>/dev/null)
    if [ -z "$status" ]; then
        echo "  STRINGS unreachable, skipping"
        continue
    fi
    restart_needed=false
    for app in $(installed_apps_on "$p"); do
        if sync_repo "$p" "/opt/$app"; then
            if [ "$app" = "$current_app" ]; then
                restart_needed=true
            fi
        fi
    done
    if [ "$restart_needed" = true ]; then
        echo "  currently-assigned app ($current_app) changed -- restarting via STRINGS"
        curl -s -X POST "http://$ip:8420/restart" > /dev/null
    fi
done

echo "=== sync complete ==="
