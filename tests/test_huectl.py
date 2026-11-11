import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import huectl  # noqa: E402
from fake_bridge import FakeBridge  # noqa: E402

HUECTL = os.path.join(ROOT, "huectl.py")


@unittest.skipUnless(shutil.which("openssl"), "openssl is needed for the fake bridge")
class HuectlTest(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge().start()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = os.path.join(self.tmp.name, "config.json")
        with open(self.cfg, "w") as f:
            json.dump(self.bridge.config(), f)
        self.env = {"HUECTL_STATE_DIR": os.path.join(self.tmp.name, "state"),
                    "HUECTL_CONFIG": self.cfg}
        self._old_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        for k in ("HUE_BRIDGE", "HUE_APP_KEY", "HUE_CERT_SHA256"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.bridge.stop()
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = huectl.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def popen(self, *argv):
        env = dict(os.environ, **self.env)
        return subprocess.Popen([sys.executable, HUECTL] + list(argv), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)

    def plug_on(self, name):
        return self.bridge.find("light", name)["on"]["on"]

    def puts(self):
        return [b for m, p, b in self.bridge.requests if m == "PUT"]

    # -- listing and state ------------------------------------------------

    def test_list_plugs_only_by_default(self):
        rc, out, _ = self.run_cli("list")
        self.assertEqual(rc, 0)
        self.assertIn("NAS", out)
        self.assertIn("Router", out)
        self.assertNotIn("Kitchen", out)
        rc, out, _ = self.run_cli("list", "--all", "--json")
        names = {i["name"]: i for i in json.loads(out)}
        self.assertEqual(set(names), {"NAS", "Router", "Kitchen lamp"})
        self.assertEqual(names["Kitchen lamp"]["kind"], "light")
        self.assertFalse(names["Router"]["reachable"])

    def test_get(self):
        rc, out, _ = self.run_cli("get", "nas")
        self.assertEqual((rc, out), (0, "on\n"))
        rc, out, _ = self.run_cli("get", "kitchen")
        self.assertEqual((rc, out), (0, "off\n"))

    def test_name_not_found_and_ambiguous(self):
        rc, _, err = self.run_cli("get", "toaster")
        self.assertEqual(rc, huectl.EXIT_NOT_FOUND)
        self.assertIn("no light or plug named", err)
        rc, _, err = self.run_cli("get", "r")  # unique prefix of "Router"
        self.assertEqual(rc, 0)
        rc, _, err = self.run_cli("get", "a")  # substring of NAS and Kitchen lamp
        self.assertEqual(rc, huectl.EXIT_NOT_FOUND)
        self.assertIn("ambiguous", err)

    def test_get_by_id(self):
        lid = self.bridge.find("light", "Router")["id"]
        rc, out, _ = self.run_cli("get", lid)
        self.assertEqual((rc, out), (0, "on\n"))

    # -- switching ----------------------------------------------------------

    def test_on_off_toggle_multiple(self):
        self.assertEqual(self.run_cli("-q", "off", "NAS", "Router")[0], 0)
        self.assertFalse(self.plug_on("NAS"))
        self.assertFalse(self.plug_on("Router"))
        self.assertEqual(self.run_cli("-q", "toggle", "NAS")[0], 0)
        self.assertTrue(self.plug_on("NAS"))
        self.assertEqual(self.run_cli("-q", "on", "Router")[0], 0)
        self.assertTrue(self.plug_on("Router"))

    def test_retries_when_bridge_busy(self):
        self.bridge.fail_puts = 2
        rc, _, err = self.run_cli("off", "NAS")
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.plug_on("NAS"))
        self.assertIn("retrying", err)

    # -- power cycle --------------------------------------------------------

    def test_power_cycle(self):
        start = time.monotonic()
        rc, _, err = self.run_cli("power-cycle", "nas", "--off-time", "0.5")
        self.assertEqual(rc, 0, err)
        self.assertGreaterEqual(time.monotonic() - start, 0.5)
        self.assertEqual(self.puts(), [{"on": {"on": False}}, {"on": {"on": True}}])
        self.assertTrue(self.plug_on("NAS"))
        self.assertIn("power-cycle of 'NAS' complete", err)

    def test_power_cycle_when_already_off_turns_on(self):
        self.bridge.find("light", "NAS")["on"]["on"] = False
        rc, _, err = self.run_cli("cycle", "NAS", "--off-time", "0")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.puts(), [{"on": {"on": True}}])
        self.assertTrue(self.plug_on("NAS"))

    def test_power_cycle_cooldown(self):
        self.assertEqual(self.run_cli("-q", "cycle", "NAS", "--off-time", "0")[0], 0)
        rc, _, err = self.run_cli("cycle", "NAS", "--off-time", "0",
                                  "--cooldown", "600")
        self.assertEqual(rc, 0)
        self.assertIn("cooldown", err)
        self.assertEqual(len(self.puts()), 2)  # second run did nothing

    def test_power_cycle_turns_back_on_after_sigterm(self):
        p = self.popen("cycle", "NAS", "--off-time", "30")
        deadline = time.time() + 10
        while self.plug_on("NAS") and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.plug_on("NAS"))
        time.sleep(0.3)
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=15)
        self.assertTrue(self.plug_on("NAS"), err)
        self.assertEqual(p.returncode, 1)
        self.assertIn("turned back on", err)

    def test_concurrent_power_cycle_is_skipped(self):
        p = self.popen("cycle", "NAS", "--off-time", "2")
        deadline = time.time() + 10
        while self.plug_on("NAS") and time.time() < deadline:
            time.sleep(0.05)
        rc, _, err = self.run_cli("cycle", "NAS", "--off-time", "0")
        self.assertEqual(rc, 0)
        self.assertIn("already in progress", err)
        p.communicate(timeout=15)
        self.assertEqual(p.returncode, 0)
        self.assertTrue(self.plug_on("NAS"))

    # -- security / configuration ------------------------------------------

    def test_certificate_pinning(self):
        cfg = dict(self.bridge.config(), cert_sha256="00" * 32)
        with open(self.cfg, "w") as f:
            json.dump(cfg, f)
        rc, _, err = self.run_cli("list")
        self.assertEqual(rc, huectl.EXIT_BRIDGE)
        self.assertIn("fingerprint changed", err)
        self.assertEqual(self.bridge.requests, [])  # nothing was sent

    def test_not_configured(self):
        rc, _, err = self.run_cli("--config", os.path.join(self.tmp.name, "nope"),
                                  "list")
        self.assertEqual(rc, huectl.EXIT_CONFIG)
        self.assertIn("huectl pair", err)

    def test_bad_app_key(self):
        os.environ["HUE_APP_KEY"] = "wrong"
        rc, _, err = self.run_cli("list")
        self.assertEqual(rc, huectl.EXIT_BRIDGE)
        self.assertIn("application key", err)

    def test_pair(self):
        new_cfg = os.path.join(self.tmp.name, "new", "config.json")
        threading.Timer(0.5, lambda: setattr(self.bridge, "link_button_presses", 1)
                        ).start()
        rc, _, err = self.run_cli("--config", new_cfg, "pair", "--bridge",
                                  self.bridge.host, "--timeout", "20")
        self.assertEqual(rc, 0, err)
        with open(new_cfg) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["app_key"], "test-app-key")
        self.assertEqual(cfg["cert_sha256"], self.bridge.fingerprint)
        self.assertEqual(os.stat(new_cfg).st_mode & 0o777, 0o600)

    # -- events ------------------------------------------------------------

    def test_events_json_with_filters(self):
        p = self.popen("events", "--json", "--type", "light", "--name", "n*",
                       "--once", "--timeout", "20")
        time.sleep(1.0)  # let it connect
        self.bridge.emit("update", {"id": self.bridge.find("zigbee_connectivity",
                                                            "NAS")["id"],
                                    "type": "zigbee_connectivity",
                                    "status": "connectivity_issue"})
        self.run_cli("-q", "off", "Router")  # filtered out by name
        self.run_cli("-q", "off", "NAS")
        out, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 0, err)
        change = json.loads(out.strip())
        self.assertEqual(change["name"], "NAS")
        self.assertEqual(change["type"], "light")
        self.assertEqual(change["data"]["on"], {"on": False})

    def test_events_exec(self):
        marker = os.path.join(self.tmp.name, "marker")
        p = self.popen("events", "--type", "zigbee_connectivity", "--once",
                       "--timeout", "20", "--exec",
                       'echo "$HUE_NAME $HUE_EVENT $(cat)" > %s' % marker)
        time.sleep(1.0)
        self.bridge.emit("update", {"id": self.bridge.find("zigbee_connectivity",
                                                            "NAS")["id"],
                                    "type": "zigbee_connectivity",
                                    "status": "connectivity_issue"})
        _, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 0, err)
        with open(marker) as f:
            content = f.read()
        self.assertTrue(content.startswith("NAS update {"), content)
        self.assertIn('"status":"connectivity_issue"', content)

    def test_events_human_output_and_timeout(self):
        p = self.popen("events", "--timeout", "2")
        time.sleep(1.0)
        self.run_cli("-q", "on", "kitchen")
        out, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, huectl.EXIT_TIMEOUT, err)
        self.assertRegex(out, r'update +light +"Kitchen lamp" +on\.on=true')

    # -- raw API -----------------------------------------------------------

    def test_api(self):
        rc, out, _ = self.run_cli("api", "GET", "room")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["data"][0]["metadata"]["name"], "Office")
        lid = self.bridge.find("light", "Kitchen lamp")["id"]
        rc, _, _ = self.run_cli("api", "PUT", "light/" + lid, '{"on":{"on":true}}')
        self.assertEqual(rc, 0)
        self.assertTrue(self.plug_on("Kitchen lamp"))


class UnitTest(unittest.TestCase):
    def test_describe_change(self):
        self.assertEqual(
            huectl.describe_change({"id": "x", "type": "light", "on": {"on": True},
                                    "dimming": {"brightness": 50.5}}),
            "on.on=true dimming.brightness=50.5")


if __name__ == "__main__":
    unittest.main()
