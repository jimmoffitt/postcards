#!/bin/bash
# One-time setup of the Pi's system pieces (run on the Pi with sudo):
#   sudo ~/projects/postcards/app/pi/install.sh
# Safe to re-run.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)

install -m 755 "$here/wifi-watchdog.sh" /usr/local/sbin/wifi-watchdog.sh
install -m 644 "$here/wifi-watchdog.service" "$here/wifi-watchdog.timer" /etc/systemd/system/

for unit in weather-bot snowbot; do
    install -D -m 644 "$here/journald-ratelimit.conf" "/etc/systemd/system/$unit.service.d/ratelimit.conf"
done

# Wi-Fi power saving is a common trigger for brcmfmac hangs on the Pi 3.
conn=$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: '$2=="wlan0"{print $1; exit}')
if [ -n "$conn" ]; then
    nmcli connection modify "$conn" 802-11-wireless.powersave 2   # 2 = disable
    echo "Wi-Fi power saving off for '$conn' (applies at next reconnect/reboot)"
fi

systemctl daemon-reload
systemctl enable --now wifi-watchdog.timer
echo "Done. Restart weather-bot and snowbot for the log rate limit to apply."
