#!/bin/bash
# sync-fleet.sh -- THE way code reaches the fleet (2026-09-22): commit +
# push to GitHub first, then run this. Never hand-scp files onto a host --
# that's what left every puppet's git state stale and blocked `git pull`
# until the 2026-09-22 cleanup (see project_mcbrain).
#
# For MP and every STRINGS puppet (P1-P4 + PRODUCTION): `git pull
# --ff-only` every app repo installed under /opt (plus STRINGS itself on
# puppets), then restart only what actually needs it:
#   - puppets: the currently assigned app, via STRINGS's /restart, only if
#     that specific app changed -- an installed-but-idle app still gets
#     pulled so it's ready next time it's assigned. If STRINGS itself
#     changed, the strings service is restarted instead (which relaunches
#     the assigned app anyway).
#   - MP: SCRUTE itself, using the safe kill-and-let-relaunch procedure
#     from project_health_monitor's restart-hazard note (kill only the
#     real python3 process, never the whole console-attach chain). MP's
#     other apps only ever run as one-off LOCAL launches from SCRUTE, so
#     they're pulled but never restarted.
#
# Any repo that's dirty, diverged, or fails to pull is reported loudly as
# DRIFT and left alone -- fix it by hand (never by scp), then re-run.
# Per-machine files (settings.ini, state.json) are gitignored, so they
# never count as drift.
#
# Must run from a machine with the fleet's SSH aliases (mp, p1-p4,
# production) -- in practice the Windows workstation; PRODUCTION itself
# has no SSH access to the puppets.
#
# Usage: ./sync-fleet.sh
set -uo pipefail

APP_NAMES="bars loudness bebop channel38 tvdinner joanjett weatherstar"

# curl needs a real IP -- SSH aliases only resolve for `ssh` itself. P2/P4
# have no DHCP reservation and can drift after a power event (see
# project_puppet_fleet); if one shows as unreachable, re-check its IP.
declare -A PUPPET_IPS=(
    [p1]=192.168.68.72
    [p2]=192.168.68.69
    [p3]=192.168.68.65
    [p4]=192.168.68.70
    [production]=192.168.68.71
)
PUPPETS="p1 p2 p3 p4 production"

DRIFT=0
PY=$(command -v python3 || command -v python)

# Returns 0 if the repo was updated, 2 if unchanged, 1 if it isn't a git
# repo, 3 on drift (dirty/diverged/pull failed -- reported, not touched).
sync_repo() {
    local host="$1" dir="$2" name
    name=$(basename "$dir")
    if ! ssh "$host" "test -d '$dir/.git'"; then
        echo "  $name: not a git repo, skipped"
        return 1
    fi
    local dirty untracked before after out
    # Untracked files can't block a fast-forward pull, so they're only
    # noted -- tracked-file changes are real drift.
    untracked=$(ssh "$host" "git -C '$dir' ls-files --others --exclude-standard")
    if [ -n "$untracked" ]; then
        echo "  $name: note -- untracked: $(echo $untracked)"
    fi
    dirty=$(ssh "$host" "git -C '$dir' status --porcelain --untracked-files=no")
    if [ -n "$dirty" ]; then
        echo "  $name: DRIFT -- local changes, not pulling:"
        echo "$dirty" | sed 's/^/      /'
        DRIFT=1
        return 3
    fi
    before=$(ssh "$host" "git -C '$dir' rev-parse HEAD")
    if ! out=$(ssh "$host" "git -C '$dir' pull -q --ff-only" 2>&1); then
        echo "  $name: DRIFT -- pull failed: $out"
        DRIFT=1
        return 3
    fi
    after=$(ssh "$host" "git -C '$dir' rev-parse HEAD")
    if [ "$before" != "$after" ]; then
        echo "  $name: ${before:0:7} -> ${after:0:7} (updated)"
        return 0
    fi
    echo "  $name: up to date"
    return 2
}

installed_apps_on() {
    local host="$1" present
    present=$(ssh "$host" "ls /opt 2>/dev/null")
    for app in $APP_NAMES; do
        echo "$present" | grep -qx "$app" && echo "$app"
    done
}

echo "=== MP ==="
sync_repo mp /opt/scrutinizer
if [ $? -eq 0 ]; then
    echo "  scrutinizer changed -- restarting SCRUTE"
    pid=$(ssh mp "pgrep -f '^python3 /opt/scrutinizer/scrutinizer\\.py\$'" || true)
    if [ -n "$pid" ]; then
        ssh mp "sudo kill -TERM $pid"
        sleep 3
    fi
    # No getty auto-respawn is common -- relaunch if nothing came back.
    # stdin from /dev/null, NOT /dev/tty1 (see project_health_monitor's
    # corrected-fallback note: /dev/tty1 there loses KD_GRAPHICS).
    if ! ssh mp "pgrep -f '^python3 /opt/scrutinizer/scrutinizer\\.py\$'" > /dev/null; then
        ssh mp "setsid -f /usr/local/bin/scrutinizer > /tmp/scrutinizer.log 2>&1 < /dev/null"
    fi
fi
for app in $(installed_apps_on mp); do
    sync_repo mp "/opt/$app"
done

for p in $PUPPETS; do
    echo "=== $p ==="
    ip="${PUPPET_IPS[$p]}"
    status=$(curl -s --max-time 3 "http://$ip:8420/status" 2>/dev/null)
    if [ -z "$status" ]; then
        echo "  STRINGS unreachable at $ip, skipping"
        DRIFT=1
        continue
    fi
    current_app=$(echo "$status" | "$PY" -c "import sys,json; print(json.load(sys.stdin).get('app') or '')" 2>/dev/null)

    sync_repo "$p" /opt/strings
    strings_changed=$?
    app_changed=false
    for app in $(installed_apps_on "$p"); do
        sync_repo "$p" "/opt/$app"
        if [ $? -eq 0 ] && [ "$app" = "$current_app" ]; then
            app_changed=true
        fi
    done

    if [ $strings_changed -eq 0 ]; then
        echo "  STRINGS changed -- restarting the strings service (relaunches $current_app)"
        ssh "$p" "sudo systemctl restart strings"
    elif [ "$app_changed" = true ]; then
        echo "  assigned app ($current_app) changed -- restarting via STRINGS"
        curl -s --max-time 5 -X POST -H "Content-Type: application/json" \
            -d "{\"app\":\"$current_app\"}" "http://$ip:8420/restart" > /dev/null
    fi
done

if [ $DRIFT -ne 0 ]; then
    echo "=== sync finished WITH DRIFT/ERRORS -- see above ==="
    exit 1
fi
echo "=== sync complete, fleet clean ==="
