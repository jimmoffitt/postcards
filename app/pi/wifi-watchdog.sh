#!/bin/bash
# Wi-Fi watchdog for the Pi, run every 2 minutes by wifi-watchdog.timer.
#
# The Pi 3's Wi-Fi chip (brcmfmac) can wedge after the router drops out: every
# scan then fails with error -110 and it never reconnects on its own. In Nov
# 2026 that kept the Pi offline for two weeks.
#
# Each run pings the router. After 3 failures in a row (~6 min) it reloads the
# Wi-Fi driver; after 6 (~12 min) it reboots -- but at most once an hour, and
# never within 15 minutes of boot, so a router that is down for hours costs an
# hourly reboot, not a reboot loop.
set -u

STATE=/run/wifi-watchdog.fails          # cleared by a reboot, on purpose
LAST_REBOOT=/var/lib/wifi-watchdog.last-reboot
IFACE=wlan0

log() { echo "wifi-watchdog: $*"; }

gateway=$(ip route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}')
if [ -n "$gateway" ] && ping -c 2 -W 3 -I "$IFACE" "$gateway" >/dev/null 2>&1; then
    if [ -s "$STATE" ]; then log "router $gateway reachable again"; fi
    rm -f "$STATE"
    exit 0
fi

fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE"
log "router ${gateway:-<no default route>} unreachable ($fails in a row)"

if [ "$fails" -eq 3 ]; then
    log "reloading the brcmfmac Wi-Fi driver"
    modprobe -r brcmfmac_wcc brcmfmac 2>/dev/null || modprobe -r brcmfmac
    sleep 3
    modprobe brcmfmac
    sleep 5
    nmcli radio wifi on
    exit 0
fi

if [ "$fails" -ge 6 ]; then
    uptime_s=$(cut -d. -f1 /proc/uptime)
    last=$(cat "$LAST_REBOOT" 2>/dev/null || echo 0)
    now=$(date +%s)
    if [ "$uptime_s" -lt 900 ]; then
        log "up only ${uptime_s}s; not rebooting yet"
    elif [ $(( now - last )) -lt 3600 ]; then
        log "rebooted less than an hour ago; not rebooting again yet"
    else
        log "Wi-Fi still down after a driver reload; rebooting"
        echo "$now" > "$LAST_REBOOT"
        sync
        systemctl reboot
    fi
fi
