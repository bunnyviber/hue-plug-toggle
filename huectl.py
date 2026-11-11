#!/usr/bin/env python3
"""huectl - control Philips Hue smart plugs (and lights) from scripts.

Talks to the Hue bridge's local CLIP v2 API. Standard library only,
Python 3.8+. Run ``huectl --help`` for usage.
"""

from __future__ import annotations

import argparse
import errno
import fnmatch
import hashlib
import hmac
import http.client
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

__version__ = "1.0.0"

# Exit codes (documented in README.md).
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_BRIDGE = 4
EXIT_CONFIG = 5
EXIT_TIMEOUT = 6

DISCOVERY_URL = "https://discovery.meethue.com/"

_quiet = False
_verbose = False


class HueError(Exception):
    """An error with an exit code attached."""

    exit_code = EXIT_ERROR


class ConfigError(HueError):
    exit_code = EXIT_CONFIG


class NotFoundError(HueError):
    exit_code = EXIT_NOT_FOUND


class BridgeError(HueError):
    """The bridge could not be reached or returned an error."""

    exit_code = EXIT_BRIDGE

    def __init__(self, message, status=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class CertificateMismatch(BridgeError):
    pass


# --------------------------------------------------------------------------
# Logging


def log(msg):
    if not _quiet:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print("%s huectl: %s" % (ts, msg), file=sys.stderr, flush=True)


def debug(msg):
    if _verbose:
        log(msg)


# --------------------------------------------------------------------------
# Configuration


def _config_candidates():
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return [os.path.join(xdg, "huectl", "config.json"), "/etc/huectl/config.json"]


def default_config_path():
    return _config_candidates()[0]


def resolve_config_path(explicit):
    if explicit:
        return explicit
    env = os.environ.get("HUECTL_CONFIG")
    if env:
        return env
    for path in _config_candidates():
        if os.path.exists(path):
            return path
    return default_config_path()


def load_config(path):
    cfg = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                cfg = json.load(f)
        except (OSError, ValueError) as e:
            raise ConfigError("cannot read config %s: %s" % (path, e))
    # Environment variables override the file.
    for key, env in (("bridge", "HUE_BRIDGE"), ("app_key", "HUE_APP_KEY"),
                     ("cert_sha256", "HUE_CERT_SHA256")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def save_config(path, cfg):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def state_dir():
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    path = os.environ.get("HUECTL_STATE_DIR") or os.path.join(xdg, "huectl")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


# --------------------------------------------------------------------------
# HTTP transport with certificate pinning
#
# Hue bridges use certificates that are not issued for their IP address, so
# normal hostname verification cannot work. Instead the SHA-256 fingerprint
# of the bridge certificate is recorded when pairing (trust on first use) and
# every later connection is checked against it before any data is sent.


def _insecure_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, fingerprint, timeout):
        super().__init__(host, timeout=timeout, context=_insecure_context())
        self.expected_fingerprint = (fingerprint or "").lower().replace(":", "")
        self.peer_fingerprint = None

    def connect(self):
        super().connect()
        der = self.sock.getpeercert(binary_form=True)
        self.peer_fingerprint = hashlib.sha256(der).hexdigest()
        if self.expected_fingerprint and not hmac.compare_digest(
                self.peer_fingerprint, self.expected_fingerprint):
            self.sock.close()
            raise CertificateMismatch(
                "bridge certificate fingerprint changed (expected %s, got %s). "
                "If you replaced or reset the bridge, run 'huectl trust'."
                % (self.expected_fingerprint, self.peer_fingerprint))


def fetch_fingerprint(host, timeout=10):
    conn = PinnedHTTPSConnection(host, None, timeout)
    try:
        conn.connect()
        return conn.peer_fingerprint
    except (OSError, ssl.SSLError) as e:
        raise BridgeError("cannot connect to bridge %s: %s" % (host, e),
                          retryable=True)
    finally:
        conn.close()


class Bridge:
    def __init__(self, host, app_key, cert_sha256, timeout=10):
        self.host = host
        self.app_key = app_key
        self.cert_sha256 = cert_sha256
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg):
        missing = [k for k in ("bridge", "app_key", "cert_sha256") if not cfg.get(k)]
        if missing:
            raise ConfigError("not paired with a bridge (missing %s). "
                              "Run 'huectl pair' first." % ", ".join(missing))
        return cls(cfg["bridge"], cfg["app_key"], cfg["cert_sha256"])

    def _connect(self, timeout=None):
        return PinnedHTTPSConnection(self.host, self.cert_sha256,
                                     timeout or self.timeout)

    def request(self, method, path, body=None, auth=True):
        """Send a request and return the decoded JSON response."""
        conn = self._connect()
        headers = {"Accept": "application/json"}
        if auth:
            headers["hue-application-key"] = self.app_key
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        debug("%s %s %s" % (method, path, payload.decode() if payload else ""))
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except CertificateMismatch:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as e:
            raise BridgeError("cannot reach bridge %s: %s" % (self.host, e),
                              retryable=True)
        finally:
            conn.close()
        try:
            data = json.loads(raw.decode() or "null")
        except ValueError:
            data = None
        if resp.status >= 400:
            desc = ""
            if isinstance(data, dict) and data.get("errors"):
                desc = "; ".join(e.get("description", "") for e in data["errors"])
            if resp.status in (401, 403):
                raise BridgeError("bridge rejected the application key (HTTP %d). "
                                  "Run 'huectl pair' again." % resp.status,
                                  status=resp.status)
            raise BridgeError("bridge returned HTTP %d %s" % (resp.status, desc),
                              status=resp.status,
                              retryable=resp.status in (429, 500, 502, 503, 504))
        return data

    # CLIP v2 helpers -----------------------------------------------------

    def get(self, rtype=None, rid=None):
        path = "/clip/v2/resource"
        if rtype:
            path += "/" + rtype
        if rid:
            path += "/" + rid
        data = self.request("GET", path)
        return data.get("data", []) if isinstance(data, dict) else []

    def put(self, rtype, rid, body):
        data = self.request("PUT", "/clip/v2/resource/%s/%s" % (rtype, rid), body)
        errors = data.get("errors") if isinstance(data, dict) else None
        if errors:
            raise BridgeError("bridge reported: " + "; ".join(
                e.get("description", "") for e in errors))
        return data

    def event_stream(self, read_timeout=300):
        """Yield lists of events from the bridge's server-sent event stream.

        Returns (StopIteration) when the connection ends; callers reconnect.
        """
        conn = self._connect(timeout=read_timeout)
        try:
            conn.request("GET", "/eventstream/clip/v2", headers={
                "hue-application-key": self.app_key,
                "Accept": "text/event-stream",
            })
            resp = conn.getresponse()
            if resp.status != 200:
                raise BridgeError("event stream returned HTTP %d" % resp.status,
                                  status=resp.status,
                                  retryable=resp.status not in (401, 403))
            data_lines = []
            while True:
                line = resp.readline()
                if not line:
                    return
                line = line.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    if data_lines:
                        try:
                            events = json.loads("\n".join(data_lines))
                        except ValueError:
                            events = []
                        data_lines = []
                        if isinstance(events, list):
                            yield events
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                # "id:", "event:" and ":" comment lines are ignored.
        except CertificateMismatch:
            raise
        except socket.timeout:
            # Idle for read_timeout seconds: just reconnect.
            debug("event stream idle; reconnecting")
            return
        except (OSError, ssl.SSLError, http.client.HTTPException) as e:
            raise BridgeError("event stream interrupted: %s" % e, retryable=True)
        finally:
            conn.close()


def with_retries(fn, retry_for, what):
    """Call fn(), retrying retryable bridge errors for up to retry_for seconds."""
    deadline = time.monotonic() + retry_for
    delay = 1.0
    while True:
        try:
            return fn()
        except BridgeError as e:
            if not e.retryable or time.monotonic() + delay > deadline:
                raise
            log("%s failed (%s); retrying in %.0fs" % (what, e, delay))
            time.sleep(delay)
            delay = min(delay * 2, 30)


# --------------------------------------------------------------------------
# Home model: a snapshot of all bridge resources with name lookup


class Home:
    def __init__(self, resources):
        self.resources = resources
        self.by_id = {r["id"]: r for r in resources if "id" in r}

    @classmethod
    def load(cls, bridge):
        return cls(bridge.get())

    def device_of(self, res):
        owner = res.get("owner") or {}
        if owner.get("rtype") == "device":
            return self.by_id.get(owner.get("rid"))
        return None

    def name_of(self, res):
        if res is None:
            return None
        meta = res.get("metadata") or {}
        if meta.get("name"):
            name = meta["name"]
            device = self.device_of(res)
            dev_name = ((device or {}).get("metadata") or {}).get("name")
            # Prefer the device name, which is what the Hue app shows, unless
            # the device has several services of this type.
            if dev_name and self._service_count(device, res.get("type")) <= 1:
                return dev_name
            return name
        owner = res.get("owner") or {}
        parent = self.by_id.get(owner.get("rid"))
        if parent is not None and parent is not res:
            return self.name_of(parent)
        return None

    @staticmethod
    def _service_count(device, rtype):
        return sum(1 for s in device.get("services", []) if s.get("rtype") == rtype)

    def lights(self):
        return [r for r in self.resources if r.get("type") == "light"]

    def is_plug(self, light):
        if (light.get("metadata") or {}).get("archetype") == "plug":
            return True
        device = self.device_of(light) or {}
        prod = device.get("product_data") or {}
        return (prod.get("product_archetype") == "plug"
                or str(prod.get("model_id", "")).startswith("LOM"))

    def connectivity(self, res):
        """Zigbee status of the device that owns res ('connected', ...)."""
        device = self.device_of(res)
        if device is None:
            return None
        for svc in device.get("services", []):
            if svc.get("rtype") == "zigbee_connectivity":
                zc = self.by_id.get(svc.get("rid"))
                if zc:
                    return zc.get("status")
        return None

    def find_light(self, query, plugs_only=False):
        candidates = self.lights()
        if plugs_only:
            candidates = [l for l in candidates if self.is_plug(l)]
        for l in candidates:
            if l["id"] == query or l.get("id_v1") == query:
                return l
        q = query.casefold()

        def names(l):
            n = {self.name_of(l) or ""}
            n.add((l.get("metadata") or {}).get("name") or "")
            return {x.casefold() for x in n if x}

        for matcher in (lambda l: q in names(l),
                        lambda l: any(n.startswith(q) for n in names(l)),
                        lambda l: any(q in n for n in names(l))):
            found = [l for l in candidates if matcher(l)]
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise NotFoundError("'%s' is ambiguous; matches: %s" % (
                    query, ", ".join(sorted(self.name_of(l) or l["id"] for l in found))))
        kind = "plug" if plugs_only else "light or plug"
        raise NotFoundError("no %s named '%s' (see 'huectl list%s')"
                            % (kind, query, "" if plugs_only else " --all"))

    def light_info(self, light):
        return {
            "id": light["id"],
            "name": self.name_of(light),
            "kind": "plug" if self.is_plug(light) else "light",
            "on": bool((light.get("on") or {}).get("on")),
            "reachable": self.connectivity(light) in (None, "connected"),
            "connectivity": self.connectivity(light),
        }


# --------------------------------------------------------------------------
# Plug operations


def read_on(bridge, light_id):
    lights = bridge.get("light", light_id)
    if not lights:
        raise NotFoundError("light %s disappeared from the bridge" % light_id)
    return bool((lights[0].get("on") or {}).get("on"))


def set_on(bridge, light_id, on, retry_for=30, verify_for=10):
    """Switch a light/plug and wait until the bridge reports the new state."""
    word = "on" if on else "off"
    with_retries(lambda: bridge.put("light", light_id, {"on": {"on": on}}),
                 retry_for, "switching %s" % word)
    deadline = time.monotonic() + verify_for
    while True:
        try:
            if read_on(bridge, light_id) == on:
                return
        except BridgeError as e:
            if not e.retryable:
                raise
        if time.monotonic() >= deadline:
            raise BridgeError("plug did not report %s within %ds" % (word, verify_for),
                              retryable=True)
        time.sleep(0.5)


class _Interrupted(Exception):
    pass


class _EventsTimeout(Exception):
    pass


def _lock(path):
    """Take a non-blocking exclusive lock. Returns the fd or None if held."""
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return None
        raise
    return fd


def power_cycle(bridge, home, light, off_time, cooldown, on_retry_for):
    info = home.light_info(light)
    name, lid = info["name"], light["id"]
    sdir = state_dir()
    lock_fd = _lock(os.path.join(sdir, "cycle-%s.lock" % lid))
    if lock_fd is None:
        log("power-cycle of '%s' already in progress elsewhere; skipping" % name)
        return
    stamp = os.path.join(sdir, "cycle-%s.last" % lid)
    try:
        if cooldown > 0 and os.path.exists(stamp):
            age = time.time() - os.path.getmtime(stamp)
            if age < cooldown:
                log("'%s' was power-cycled %ds ago (cooldown %ds); skipping"
                    % (name, age, cooldown))
                return
        if not info["reachable"]:
            log("warning: bridge reports '%s' as %s; trying anyway"
                % (name, info["connectivity"]))

        if info["on"]:
            log("turning '%s' off" % name)
            set_on(bridge, lid, False)
        else:
            log("'%s' is already off" % name)

        # From here on the plug is off: whatever happens, try hard to turn it
        # back on. SIGTERM/SIGINT/SIGHUP cut the wait short but never stop
        # the plug from being switched back on.
        state = {"sleeping": True, "interrupted": False}

        def handler(signum, frame):
            state["interrupted"] = True
            if state["sleeping"]:
                raise _Interrupted()

        old = {s: signal.signal(s, handler)
               for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        try:
            log("waiting %gs" % off_time)
            try:
                time.sleep(off_time)
            except _Interrupted:
                pass
            state["sleeping"] = False
            if state["interrupted"]:
                log("interrupted; turning '%s' back on now" % name)
            log("turning '%s' on" % name)
            set_on(bridge, lid, True, retry_for=on_retry_for, verify_for=30)
        finally:
            for s, h in old.items():
                signal.signal(s, h)
        interrupted = state["interrupted"]
        with open(stamp, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat() + "\n")
        log("power-cycle of '%s' complete" % name)
        if interrupted:
            raise HueError("power-cycle interrupted (plug was turned back on)")
    finally:
        os.close(lock_fd)


# --------------------------------------------------------------------------
# Events


def flatten(d, prefix=""):
    out = []
    for k, v in d.items():
        key = prefix + k
        if isinstance(v, dict):
            out.extend(flatten(v, key + "."))
        else:
            out.append((key, v))
    return out


def describe_change(data):
    skip = {"id", "id_v1", "owner", "type"}
    parts = []
    for k, v in flatten({k: v for k, v in data.items() if k not in skip}):
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, (list, dict)) or v is None:
            v = json.dumps(v, separators=(",", ":"))
        parts.append("%s=%s" % (k, v))
    return " ".join(parts)


def iter_changes(bridge, home, reconnect=True):
    """Yield one dict per changed resource, forever (reconnecting)."""
    delay = 1
    while True:
        try:
            for batch in bridge.event_stream():
                delay = 1
                for ev in batch:
                    etype = ev.get("type")
                    for res in ev.get("data", []):
                        rid = res.get("id")
                        if etype == "add" and rid:
                            home.by_id[rid] = res
                            home.resources.append(res)
                        known = home.by_id.get(rid)
                        if etype == "update" and known is not None:
                            _merge(known, res)
                        name = home.name_of(known if known is not None else res)
                        yield {
                            "time": ev.get("creationtime"),
                            "event": etype,
                            "type": res.get("type"),
                            "id": rid,
                            "name": name,
                            "data": res,
                        }
                        if etype == "delete" and rid in home.by_id:
                            home.resources.remove(home.by_id.pop(rid))
            if not reconnect:
                return
            debug("event stream closed; reconnecting")
        except BridgeError as e:
            if not e.retryable or not reconnect:
                raise
            log("%s; reconnecting in %ds" % (e, delay))
            time.sleep(delay)
            delay = min(delay * 2, 60)


def _merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


def change_env(change):
    env = dict(os.environ)
    env.update({
        "HUE_EVENT": change["event"] or "",
        "HUE_TYPE": change["type"] or "",
        "HUE_ID": change["id"] or "",
        "HUE_NAME": change["name"] or "",
        "HUE_TIME": change["time"] or "",
        "HUE_JSON": json.dumps(change, separators=(",", ":")),
    })
    on = (change["data"].get("on") or {}).get("on")
    if on is not None:
        env["HUE_ON"] = "true" if on else "false"
    return env


# --------------------------------------------------------------------------
# Commands


def cmd_discover(args, cfg):
    try:
        req = urllib.request.Request(DISCOVERY_URL, headers={"User-Agent": "huectl"})
        with urllib.request.urlopen(req, timeout=10) as r:
            bridges = json.load(r)
    except (OSError, ValueError) as e:
        raise BridgeError("discovery via %s failed: %s. Find the bridge IP in the "
                          "Hue app (Settings > Bridges) instead." % (DISCOVERY_URL, e))
    if not bridges:
        print("no bridges found", file=sys.stderr)
        return EXIT_NOT_FOUND
    for b in bridges:
        print("%s\t%s" % (b.get("internalipaddress"), b.get("id")))
    return EXIT_OK


def cmd_pair(args, cfg):
    host = args.bridge or cfg.get("bridge")
    if not host:
        raise ConfigError("give the bridge address with --bridge "
                          "(find it with 'huectl discover' or in the Hue app)")
    fingerprint = fetch_fingerprint(host)
    print("Bridge %s certificate SHA-256: %s" % (host, fingerprint), file=sys.stderr)
    print("Press the link button on the bridge now (waiting up to %ds)..."
          % args.timeout, file=sys.stderr)
    bridge = Bridge(host, None, fingerprint)
    devicetype = "huectl#%s" % (socket.gethostname()[:19] or "host")
    deadline = time.monotonic() + args.timeout
    while True:
        result = bridge.request("POST", "/api", {"devicetype": devicetype,
                                                  "generateclientkey": True},
                                auth=False)
        entry = result[0] if isinstance(result, list) and result else {}
        if "success" in entry:
            break
        err = entry.get("error", {})
        if err.get("type") != 101:  # 101 = link button not pressed
            raise BridgeError("pairing failed: %s" % err.get("description", result))
        if time.monotonic() >= deadline:
            raise HueError("timed out waiting for the link button")
        time.sleep(2)
    cfg = {k: v for k, v in cfg.items()}
    cfg.update({"bridge": host, "app_key": entry["success"]["username"],
                "cert_sha256": fingerprint})
    if entry["success"].get("clientkey"):
        cfg["client_key"] = entry["success"]["clientkey"]
    save_config(args.config_path, cfg)
    print("Paired. Configuration saved to %s" % args.config_path, file=sys.stderr)
    return EXIT_OK


def cmd_trust(args, cfg):
    host = args.bridge or cfg.get("bridge")
    if not host:
        raise ConfigError("no bridge configured; use --bridge")
    fingerprint = fetch_fingerprint(host)
    if args.fingerprint:
        expected = args.fingerprint.lower().replace(":", "")
        if fingerprint != expected:
            raise CertificateMismatch("bridge presents %s, not %s"
                                      % (fingerprint, expected))
    print("Bridge %s certificate SHA-256: %s" % (host, fingerprint), file=sys.stderr)
    cfg = dict(cfg, bridge=host, cert_sha256=fingerprint)
    save_config(args.config_path, cfg)
    print("Saved to %s" % args.config_path, file=sys.stderr)
    return EXIT_OK


def cmd_list(args, cfg):
    bridge = Bridge.from_config(cfg)
    home = Home.load(bridge)
    lights = home.lights() if args.all else [l for l in home.lights() if home.is_plug(l)]
    infos = sorted((home.light_info(l) for l in lights),
                   key=lambda i: (i["name"] or "").casefold())
    if args.json:
        print(json.dumps(infos, indent=2))
        return EXIT_OK
    if not infos:
        print("no plugs found" + ("" if args.all else " (try --all)"), file=sys.stderr)
        return EXIT_OK
    width = max(len(i["name"] or "") for i in infos)
    for i in infos:
        print("%-*s  %-3s  %-5s  %-18s  %s" % (
            width, i["name"], "on" if i["on"] else "off", i["kind"],
            i["connectivity"] or "-", i["id"]))
    return EXIT_OK


def cmd_get(args, cfg):
    bridge = Bridge.from_config(cfg)
    home = Home.load(bridge)
    light = home.find_light(args.name)
    info = home.light_info(light)
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        if not info["reachable"]:
            log("warning: '%s' is %s; state may be stale"
                % (info["name"], info["connectivity"]))
        print("on" if info["on"] else "off")
    return EXIT_OK


def cmd_switch(args, cfg):
    bridge = Bridge.from_config(cfg)
    home = Home.load(bridge)
    lights = [home.find_light(n) for n in args.names]
    for light in lights:
        info = home.light_info(light)
        if args.command == "toggle":
            target = not read_on(bridge, light["id"])
        else:
            target = args.command == "on"
        log("turning '%s' %s" % (info["name"], "on" if target else "off"))
        set_on(bridge, light["id"], target, retry_for=args.retry_for)
    return EXIT_OK


def cmd_power_cycle(args, cfg):
    if args.off_time < 0:
        raise HueError("--off-time must not be negative")
    bridge = Bridge.from_config(cfg)
    home = with_retries(lambda: Home.load(bridge), args.retry_for, "reading bridge")
    light = home.find_light(args.name)
    power_cycle(bridge, home, light, args.off_time, args.cooldown, args.on_retry_for)
    return EXIT_OK


def cmd_events(args, cfg):
    bridge = Bridge.from_config(cfg)
    home = Home.load(bridge)
    types = set()
    for t in args.type or []:
        types.update(x.strip() for x in t.split(",") if x.strip())
    deadline = time.monotonic() + args.timeout if args.timeout else None

    if deadline is not None:
        def on_alarm(signum, frame):
            raise _EventsTimeout()
        signal.signal(signal.SIGALRM, on_alarm)
        signal.setitimer(signal.ITIMER_REAL, args.timeout)

    try:
        for change in iter_changes(bridge, home):
            if types and change["type"] not in types:
                continue
            if args.name and not any(
                    fnmatch.fnmatch((change["name"] or "").casefold(), pat.casefold())
                    for pat in args.name):
                continue
            line = json.dumps(change, separators=(",", ":"))
            if args.json:
                print(line, flush=True)
            elif not args.exec:
                print("%s %-6s %-20s %-24s %s" % (
                    change["time"] or "", change["event"], change["type"],
                    json.dumps(change["name"] or change["id"]),
                    describe_change(change["data"])), flush=True)
            if args.exec:
                rc = subprocess.run(args.exec, shell=True, input=line.encode(),
                                    env=change_env(change)).returncode
                if rc != 0:
                    log("command exited with status %d" % rc)
            if args.once:
                return EXIT_OK
    except _EventsTimeout:
        return EXIT_TIMEOUT
    finally:
        if deadline is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
    return EXIT_OK


def cmd_api(args, cfg):
    bridge = Bridge.from_config(cfg)
    path = args.path
    if not path.startswith("/"):
        path = "/clip/v2/resource/" + path
    body = None
    if args.body is not None:
        text = sys.stdin.read() if args.body == "-" else args.body
        try:
            body = json.loads(text)
        except ValueError as e:
            raise HueError("body is not valid JSON: %s" % e)
    print(json.dumps(bridge.request(args.method.upper(), path, body), indent=2))
    return EXIT_OK


# --------------------------------------------------------------------------
# CLI


def build_parser():
    p = argparse.ArgumentParser(
        prog="huectl",
        description="Control Philips Hue smart plugs and lights via the local bridge.",
        epilog="Exit codes: 0 ok, 1 error, 2 usage, 3 not found/ambiguous, "
               "4 bridge unreachable or error, 5 not configured, 6 timeout.")
    p.add_argument("--config", help="config file (default: $HUECTL_CONFIG, "
                   "~/.config/huectl/config.json, /etc/huectl/config.json)")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress messages")
    p.add_argument("-v", "--verbose", action="store_true", help="log API requests")
    p.add_argument("--version", action="version", version="huectl " + __version__)
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    s = sub.add_parser("discover", help="find bridges on your network (via Hue cloud)")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("pair", help="pair with a bridge (press its link button)")
    s.add_argument("--bridge", help="bridge IP address or hostname")
    s.add_argument("--timeout", type=int, default=60,
                   help="seconds to wait for the link button (default 60)")
    s.set_defaults(func=cmd_pair)

    s = sub.add_parser("trust", help="re-pin the bridge's TLS certificate")
    s.add_argument("--bridge", help="bridge IP address or hostname")
    s.add_argument("--fingerprint", help="only accept this SHA-256 fingerprint")
    s.set_defaults(func=cmd_trust)

    s = sub.add_parser("list", help="list plugs (or all lights with --all)")
    s.add_argument("-a", "--all", action="store_true", help="include lights")
    s.add_argument("--json", action="store_true", help="JSON output")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("get", help="print 'on' or 'off' for a plug or light")
    s.add_argument("name", help="name (case-insensitive, unique prefix ok) or id")
    s.add_argument("--json", action="store_true", help="JSON output")
    s.set_defaults(func=cmd_get)

    for name, desc in (("on", "turn on"), ("off", "turn off"), ("toggle", "toggle")):
        s = sub.add_parser(name, help="%s one or more plugs/lights" % desc)
        s.add_argument("names", nargs="+", metavar="name")
        s.add_argument("--retry-for", type=float, default=30, metavar="SEC",
                       help="keep retrying if the bridge is unreachable (default 30)")
        s.set_defaults(func=cmd_switch)

    s = sub.add_parser("power-cycle", aliases=["cycle"],
                       help="turn a plug off, wait, and turn it back on")
    s.add_argument("name", help="name (case-insensitive, unique prefix ok) or id")
    s.add_argument("--off-time", type=float, default=60, metavar="SEC",
                   help="how long to keep the plug off (default 60)")
    s.add_argument("--cooldown", type=float, default=0, metavar="SEC",
                   help="skip if this plug was power-cycled less than SEC ago")
    s.add_argument("--retry-for", type=float, default=60, metavar="SEC",
                   help="retry reading the bridge for this long (default 60)")
    s.add_argument("--on-retry-for", type=float, default=600, metavar="SEC",
                   help="keep trying to turn the plug back on for this long "
                        "(default 600)")
    s.set_defaults(func=cmd_power_cycle)

    s = sub.add_parser("events", help="stream events (state changes) from the bridge")
    s.add_argument("-t", "--type", action="append", metavar="TYPE",
                   help="only these resource types, e.g. light, motion, button, "
                        "zigbee_connectivity (repeatable or comma-separated)")
    s.add_argument("-n", "--name", action="append", metavar="GLOB",
                   help="only resources whose name matches (case-insensitive glob)")
    s.add_argument("--json", action="store_true", help="one JSON object per line")
    s.add_argument("--exec", metavar="CMD",
                   help="run CMD (via sh) for each event; event JSON on stdin and "
                        "in $HUE_JSON, plus $HUE_EVENT $HUE_TYPE $HUE_ID $HUE_NAME "
                        "$HUE_TIME and $HUE_ON")
    s.add_argument("--once", action="store_true", help="exit after the first match")
    s.add_argument("--timeout", type=float, metavar="SEC",
                   help="give up after SEC seconds (exit code 6)")
    s.set_defaults(func=cmd_events)

    s = sub.add_parser("api", help="raw CLIP v2 API request")
    s.add_argument("method", help="GET, PUT, POST or DELETE")
    s.add_argument("path", help="e.g. 'device' or '/clip/v2/resource/light/<id>'")
    s.add_argument("body", nargs="?", help="JSON body, or - to read from stdin")
    s.set_defaults(func=cmd_api)
    return p


def main(argv=None):
    global _quiet, _verbose
    args = build_parser().parse_args(argv)
    _quiet, _verbose = args.quiet, args.verbose
    if args.command == "cycle":
        args.command = "power-cycle"
    args.config_path = resolve_config_path(args.config)
    try:
        cfg = load_config(args.config_path)
        return args.func(args, cfg)
    except HueError as e:
        print("huectl: error: %s" % e, file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
