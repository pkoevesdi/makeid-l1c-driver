#!/bin/sh
# Run l1c-ippd as a systemd user service and add the CUPS queue "L1-C".
# Usage: linux/install.sh [MAC]    (no root needed if you are in the lpadmin group)
set -e
REPO=$(cd "$(dirname "$0")/.." && pwd)
UNIT="$HOME/.config/systemd/user/l1c-ippd.service"
ADDR="${1:-$L1C_ADDR}"

mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<UNIT
[Unit]
Description=IPP Everywhere server for the MakeID L1-C label printer
After=bluetooth.target

[Service]
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 $REPO/l1c-ippd${ADDR:+ --addr $ADDR}
Restart=on-failure

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable l1c-ippd.service
systemctl --user restart l1c-ippd.service
for _ in 1 2 3 4 5 6 7 8 9 10; do
    ipptool -q ipp://localhost:8631/ipp/print get-printer-attributes.test 2>/dev/null && break
    sleep 0.5
done
lpadmin -p L1-C -E -v ipp://localhost:8631/ipp/print -m everywhere -D "MakeID L1-C" -L "Bluetooth LE"
echo "Queue L1-C installed."
