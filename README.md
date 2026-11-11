# hue-plug-toggle

`huectl` is a small command-line tool for controlling Philips Hue smart plugs
(and lights) from scripts. Its main job is to **power-cycle a plug**, so the
script that sends a "NAS is unresponsive" alert can turn the NAS off and on
again by itself. It can also list plugs, switch them, and stream events
from the Hue home so you can react to them.

- It is one file and uses only the Python standard library (Python 3.8 or
  newer). There is nothing to `pip install`.
- It talks to the Hue bridge directly over your local network, using the
  bridge's local API (CLIP v2). No Hue account or internet connection is
  needed.
- It pins the bridge's TLS certificate when you pair, so later connections
  only go to that bridge.

## Install

```sh
sudo install -m 755 huectl.py /usr/local/bin/huectl
```

Run it on the machine that runs your alert script. Don't run it on the NAS
itself: a frozen NAS can't power-cycle itself, and the tool would be cut off
as soon as the plug turns off.

## Pair with the bridge (once)

1. Find the bridge's IP address. Use `huectl discover`, or look in the Hue
   app under **Settings → Bridges → (your bridge) → IP address**. Give the
   bridge a fixed IP address (a DHCP reservation in your router), so the
   address doesn't change later.
2. Run the command below, then press the round link button on the bridge
   within 60 seconds:

   ```sh
   huectl pair --bridge 192.168.1.20
   ```

This writes the bridge address, an application key and the bridge's
certificate fingerprint to `~/.config/huectl/config.json` (mode 600). Keep
this file private: the key gives full control of your Hue home.

The config file belongs to the user that ran `pair`. If your alert script
runs as another user (for example root from cron), either pair as that user,
copy the file to `/etc/huectl/config.json`, or point to it with
`--config FILE` or `HUECTL_CONFIG=FILE`. You can also supply the settings
with the `HUE_BRIDGE`, `HUE_APP_KEY` and `HUE_CERT_SHA256` environment
variables, which override the file.

## Use it from the alert script

```sh
huectl power-cycle NAS --off-time 60 --cooldown 1800
```

This turns the plug called "NAS" off, checks that the bridge reports it as
off, waits 60 seconds, then turns it on and checks that it is on.

- **It always tries hard to turn the plug back on.** If the bridge is briefly
  unreachable, it keeps retrying for up to 10 minutes (`--on-retry-for`).
  If the script is stopped with Ctrl-C, `kill`, or a hangup during the wait,
  it turns the plug on before exiting.
- **`--cooldown 1800`** skips the power-cycle if this plug was power-cycled
  less than 30 minutes ago. This stops repeated alerts from cutting power
  again while the NAS is still booting. Without `--cooldown`, there's no
  limit.
- **Only one power-cycle per plug runs at a time.** If another one is
  already running, the new one is skipped.
- Skipped runs exit with 0 and print why to stderr.

See [`examples/nas-watchdog.sh`](examples/nas-watchdog.sh) for a complete
example.

## Commands

Names are not case-sensitive. A unique prefix or part of a name is enough,
so `nas` matches "NAS" and `kitch` matches "Kitchen lamp". You can also use
a resource id from `huectl list`.

| Command | What it does |
| --- | --- |
| `huectl list` | List plugs: name, on/off, kind, Zigbee connectivity, id. `--all` includes lights; `--json` prints JSON. |
| `huectl get NAME` | Print `on` or `off`. `--json` prints details, including reachability. |
| `huectl on NAME...` / `off` / `toggle` | Switch one or more plugs or lights. |
| `huectl power-cycle NAME` | Turn off, wait, turn on (see above). Also available as `cycle`. |
| `huectl events` | Stream events from the bridge (see below). |
| `huectl api METHOD PATH [JSON]` | Send any CLIP v2 request, e.g. `huectl api GET device` or `huectl api GET room`. |
| `huectl discover` | Find bridges through the Hue discovery service (needs internet). |
| `huectl pair --bridge IP` | Pair with a bridge. |
| `huectl trust` | Re-pin the bridge's certificate after you replace or reset the bridge. |

Global options: `-q` hides progress messages, `-v` logs every API request,
and `--config FILE` uses a different config file. Progress messages go to
stderr; results go to stdout.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success, or a power-cycle was skipped (cooldown or already running) |
| 1 | Other error, e.g. a power-cycle was interrupted (the plug was turned back on) |
| 2 | Invalid command-line usage |
| 3 | No plug or light matches the name, or the name matches several |
| 4 | Bridge unreachable, key rejected, certificate mismatch, or the plug didn't switch |
| 5 | Not paired or invalid config |
| 6 | `events --timeout` expired |

## Events: see and react to what happens

`huectl events` connects to the bridge's event stream and prints one line
per change: plugs and lights switching, motion sensors, buttons, devices
going offline, and so on. It reconnects automatically if the connection
drops.

```
$ huectl events
2026-11-11T16:05:00Z update light                "NAS"                    on.on=false
2026-11-11T16:06:00Z update light                "NAS"                    on.on=true
2026-11-11T16:07:12Z update zigbee_connectivity  "Hallway sensor"         status=connectivity_issue
```

Filters:

- `--type light,motion` or `-t button`: only these resource types
- `--name 'NAS'` or `-n 'kitchen*'`: only resources whose name matches
  this glob (not case-sensitive)
- `--once`: exit after the first matching event
- `--timeout SEC`: give up after SEC seconds (exit code 6)

To process events in another program, `--json` prints one JSON object per
line:

```sh
huectl events --json | jq .
```

To run a command for every event, use `--exec`. The command runs through
`sh`, gets the event as JSON on stdin, and gets these environment
variables: `HUE_EVENT` (`update`, `add`, `delete`), `HUE_TYPE`, `HUE_ID`,
`HUE_NAME`, `HUE_TIME`, `HUE_JSON`, and `HUE_ON` (`true`/`false`, for
plugs and lights).

```sh
# Log whenever someone switches the NAS plug off
huectl events -t light -n NAS --exec '[ "$HUE_ON" = false ] && logger "NAS plug switched off"'

# Wait (up to 5 minutes) until the NAS plug is switched on
huectl events -t light -n NAS --once --timeout 300
```

Commands run one at a time, in order. Put a long-running command in the
background (`--exec 'mycmd &'`) so it doesn't hold up later events. To keep
an event handler running, run it as a systemd service or under your
process supervisor. See
[`examples/react-to-events.sh`](examples/react-to-events.sh) for more
examples.

## Development

Run the tests (they need `openssl` to create a certificate for the fake
bridge):

```sh
python3 -m unittest discover -s tests -v
```

`tests/fake_bridge.py` is a minimal fake Hue bridge. You can also run it
directly (`python3 tests/fake_bridge.py`) to try `huectl` without real
hardware: put the printed config into a file and pass it with `--config`.
