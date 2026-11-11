#!/usr/bin/env bash
# Example: what to add to the script that sends the "NAS unresponsive" alert.
#
# It power-cycles the plug the NAS is connected to. The cooldown stops the
# plug from being cycled again while the NAS is still booting, even if the
# alert fires again in the meantime.

PLUG="NAS"              # the plug's name in the Hue app
OFF_TIME=60             # seconds to keep the NAS unpowered
COOLDOWN=1800           # don't cycle again within 30 minutes

send_alert() {
    # ... your existing alert code ...
    echo "ALERT: $*"
}

if ! ping -c 3 -W 2 nas.local >/dev/null 2>&1; then
    send_alert "NAS is unresponsive; power-cycling plug '$PLUG'"
    if huectl power-cycle "$PLUG" --off-time "$OFF_TIME" --cooldown "$COOLDOWN"; then
        send_alert "Plug '$PLUG' was power-cycled; NAS should be back shortly"
    else
        send_alert "Power-cycling plug '$PLUG' FAILED (exit code $?); check it manually"
    fi
fi
