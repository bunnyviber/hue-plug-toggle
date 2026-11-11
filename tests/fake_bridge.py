"""A minimal fake Hue bridge (CLIP v2) for tests and local experiments.

Run directly to start one on https://127.0.0.1:8443:

    python3 tests/fake_bridge.py
"""

import json
import os
import queue
import ssl
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_KEY = "test-app-key"


def make_cert(directory):
    cert = os.path.join(directory, "cert.pem")
    key = os.path.join(directory, "key.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", key, "-out", cert, "-days", "2",
                    "-subj", "/CN=001788fffe000000"],
                   check=True, capture_output=True)
    return cert, key


def _res(rtype, **kw):
    r = {"id": str(uuid.uuid4()), "type": rtype}
    r.update(kw)
    return r


def default_resources():
    """Two plugs (one unreachable) and one bulb."""
    out = []

    def add_device(name, model, archetype, light_archetype, on, status):
        dev = _res("device", metadata={"name": name, "archetype": light_archetype},
                   product_data={"model_id": model, "product_archetype": archetype})
        light = _res("light", owner={"rid": dev["id"], "rtype": "device"},
                     metadata={"name": name, "archetype": light_archetype},
                     on={"on": on})
        zc = _res("zigbee_connectivity", owner={"rid": dev["id"], "rtype": "device"},
                  status=status)
        dev["services"] = [{"rid": light["id"], "rtype": "light"},
                           {"rid": zc["id"], "rtype": "zigbee_connectivity"}]
        out.extend([dev, light, zc])

    add_device("NAS", "LOM007", "plug", "plug", True, "connected")
    add_device("Router", "LOM007", "plug", "plug", True, "connectivity_issue")
    add_device("Kitchen lamp", "LCA001", "sultan_bulb", "sultan_bulb", False,
               "connected")
    out.append(_res("room", metadata={"name": "Office", "archetype": "office"},
                    children=[]))
    return out


class FakeBridge:
    def __init__(self, port=0):
        self.tmp = tempfile.TemporaryDirectory()
        cert, key = make_cert(self.tmp.name)
        self.resources = default_resources()
        self.lock = threading.Lock()
        self.subscribers = []
        self.requests = []          # (method, path, body)
        self.link_button_presses = 0  # pairing succeeds once this is > 0
        self.fail_puts = 0          # number of PUTs to answer with HTTP 503
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, status, obj):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def _authorized(self):
                if self.headers.get("hue-application-key") != APP_KEY:
                    self._send(403, {"errors": [{"description": "unauthorized user"}],
                                     "data": []})
                    return False
                return True

            def do_POST(self):
                body = self._body()
                bridge.requests.append(("POST", self.path, body))
                if self.path == "/api":
                    if bridge.link_button_presses > 0:
                        self._send(200, [{"success": {"username": APP_KEY,
                                                      "clientkey": "ABCDEF"}}])
                    else:
                        bridge.link_button_presses_waiting = True
                        self._send(200, [{"error": {
                            "type": 101, "address": "",
                            "description": "link button not pressed"}}])
                    return
                self._send(404, {})

            def do_GET(self):
                bridge.requests.append(("GET", self.path, None))
                if not self._authorized():
                    return
                if self.path == "/eventstream/clip/v2":
                    return self._stream()
                parts = self.path.strip("/").split("/")
                if parts[:3] != ["clip", "v2", "resource"]:
                    return self._send(404, {"errors": [], "data": []})
                with bridge.lock:
                    data = list(bridge.resources)
                    if len(parts) > 3:
                        data = [r for r in data if r["type"] == parts[3]]
                    if len(parts) > 4:
                        data = [r for r in data if r["id"] == parts[4]]
                    data = json.loads(json.dumps(data))
                if len(parts) > 4 and not data:
                    return self._send(404, {"errors": [{"description": "not found"}],
                                            "data": []})
                self._send(200, {"errors": [], "data": data})

            def do_PUT(self):
                body = self._body()
                bridge.requests.append(("PUT", self.path, body))
                if not self._authorized():
                    return
                if bridge.fail_puts > 0:
                    bridge.fail_puts -= 1
                    return self._send(503, {"errors": [{"description": "busy"}],
                                            "data": []})
                parts = self.path.strip("/").split("/")
                rtype, rid = parts[3], parts[4]
                with bridge.lock:
                    res = next((r for r in bridge.resources
                                if r["type"] == rtype and r["id"] == rid), None)
                    if res is None:
                        return self._send(404, {"errors": [{"description": "nf"}],
                                                "data": []})
                    for k, v in body.items():
                        res.setdefault(k, {}).update(v)
                bridge.emit("update", dict(body, id=rid, type=rtype,
                                           owner=res.get("owner")))
                self._send(200, {"errors": [], "data": [{"rid": rid, "rtype": rtype}]})

            def _stream(self):
                q = queue.Queue()
                with bridge.lock:
                    bridge.subscribers.append(q)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def chunk(text):
                    data = text.encode()
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                    self.wfile.flush()

                try:
                    chunk(": hi\n\n")
                    while True:
                        item = q.get()
                        if item is None:
                            self.wfile.write(b"0\r\n\r\n")
                            return
                        chunk("id: %s\ndata: %s\n\n" % (uuid.uuid4(), json.dumps(item)))
                except OSError:
                    pass
                finally:
                    with bridge.lock:
                        if q in bridge.subscribers:
                            bridge.subscribers.remove(q)
                    self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        with open(cert) as f:
            der = ssl.PEM_cert_to_DER_cert(f.read())
        import hashlib
        self.fingerprint = hashlib.sha256(der).hexdigest()
        self.host = "127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        with self.lock:
            for q in self.subscribers:
                q.put(None)
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def emit(self, etype, *data):
        event = [{"creationtime": datetime.now(timezone.utc).strftime(
                      "%Y-%m-%dT%H:%M:%SZ"),
                  "id": str(uuid.uuid4()), "type": etype, "data": list(data)}]
        with self.lock:
            for q in self.subscribers:
                q.put(event)

    def find(self, rtype, name):
        """Return the resource of rtype owned by the device called name."""
        dev = next(r for r in self.resources
                   if r["type"] == "device" and r["metadata"]["name"] == name)
        rid = next(s["rid"] for s in dev["services"] if s["rtype"] == rtype)
        return next(r for r in self.resources if r["id"] == rid)

    def config(self):
        return {"bridge": self.host, "app_key": APP_KEY,
                "cert_sha256": self.fingerprint}


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8443
    fb = FakeBridge(port).start()
    fb.link_button_presses = 1
    print("fake bridge on https://%s" % fb.host)
    print("config:", json.dumps(fb.config()))
    try:
        fb.thread.join()
    except KeyboardInterrupt:
        fb.stop()
