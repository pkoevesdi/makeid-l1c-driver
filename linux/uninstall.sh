#!/bin/sh
# Remove the CUPS queue "L1-C" and the l1c-ippd user service.
lpadmin -x L1-C 2>/dev/null
systemctl --user disable --now l1c-ippd.service 2>/dev/null
rm -f "$HOME/.config/systemd/user/l1c-ippd.service"
systemctl --user daemon-reload
echo "L1-C removed."
