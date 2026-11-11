#!/usr/bin/env bash
# Example: act on events in the Hue home.
#
# Each matching event runs the handler below with details in environment
# variables ($HUE_EVENT $HUE_TYPE $HUE_ID $HUE_NAME $HUE_TIME $HUE_ON) and
# the full event as JSON on stdin and in $HUE_JSON.

# 1. Notify when any plug is switched off (by the app, a switch, or huectl):
huectl events --type light --exec '
    if [ "$HUE_ON" = false ]; then
        echo "$HUE_TIME: $HUE_NAME was switched off"
    fi
' &

# 2. Warn when a device drops off the Zigbee network:
huectl events --type zigbee_connectivity --exec '
    status=$(jq -r ".data.status")   # event JSON arrives on stdin
    [ "$status" != connected ] && echo "$HUE_NAME is $status"
' &

wait
