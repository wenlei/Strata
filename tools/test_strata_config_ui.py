"""tools/test_strata_config_ui.py - the settings page's config editing and its local-only HTTP surface.

    python -m unittest tools/test_strata_config_ui.py -v

No GPU, no model and no download: the Strata folder is a temporary one with a stand-in setup.py and serve/server.py.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import strata_config_ui as U  # noqa: E402

CFG = {"exe": "engine/strata.exe",
       "args": ["--native", "model.gguf", "--max-context", "32768", "--kv", "int8"],
       "port": 8080, "host": "127.0.0.1", "model_name": "qwen3.8-flash-next-iq3_s",
       "log": "strata-iq3_s.log", "keep_me": {"nested": True}}


class Folder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "setup.py").write_text("", encoding="utf-8")
        (self.root / "serve").mkdir()
        (self.root / "serve" / "server.py").write_text("", encoding="utf-8")
        (self.root / "strata-iq3_s.json").write_text(json.dumps(CFG), encoding="utf-8")
        self.app = U.App(self.root, 8090)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self):
        return json.loads((self.root / "strata-iq3_s.json").read_text(encoding="utf-8"))

    def test_lan_without_a_key_is_refused(self):
        with self.assertRaises(ValueError):
            self.app.save_config({"model": "iq3_s", "host": "0.0.0.0"})
        self.assertEqual(self.cfg(), CFG)

    def test_generate_key_for_the_lan(self):
        out = self.app.save_config({"model": "iq3_s", "host": "0.0.0.0", "generate_key": True})
        self.assertEqual(out["host"], "0.0.0.0")
        self.assertTrue(out["api_key_set"])
        self.assertNotIn("api_key", out)
        cfg = self.cfg()
        self.assertGreaterEqual(len(cfg["api_key"]), 32)
        self.assertEqual(cfg["keep_me"], {"nested": True})
        self.assertEqual(cfg["args"], CFG["args"])
        self.assertTrue((self.root / "strata-iq3_s.json.bak").is_file())
        self.assertEqual(json.loads((self.root / "strata-iq3_s.json.bak").read_text(encoding="utf-8")), CFG)

    def test_the_key_cannot_be_removed_while_the_lan_is_open(self):
        self.app.save_config({"model": "iq3_s", "host": "0.0.0.0", "generate_key": True})
        before = self.cfg()
        with self.assertRaises(ValueError):
            self.app.save_config({"model": "iq3_s", "clear_key": True})
        self.assertEqual(self.cfg(), before)

    def test_keys_can_be_added_and_removed(self):
        first, second = "a" * 20, "b" * 20
        out = self.app.save_config({"model": "iq3_s", "add_api_key": first})
        self.assertEqual(out["api_key_count"], 1)
        out = self.app.save_config({"model": "iq3_s", "add_api_key": second})
        self.assertEqual(out["api_key_count"], 2)
        self.assertEqual(self.cfg()["api_key"], [first, second])
        out = self.app.save_config({"model": "iq3_s", "remove_api_key": 0})
        self.assertEqual(out["api_key_count"], 1)
        self.assertEqual(self.cfg()["api_key"], second)

    def test_a_short_or_duplicate_added_key_is_refused(self):
        self.app.save_config({"model": "iq3_s", "add_api_key": "a" * 20})
        changed = self.cfg()
        with self.assertRaises(ValueError):
            self.app.save_config({"model": "iq3_s", "add_api_key": "short"})
        with self.assertRaises(ValueError):
            self.app.save_config({"model": "iq3_s", "add_api_key": "a" * 20})
        self.assertEqual(self.cfg(), changed)

    def test_the_last_key_cannot_be_removed_while_the_lan_is_open(self):
        self.app.save_config({"model": "iq3_s", "host": "0.0.0.0", "add_api_key": "a" * 20})
        before = self.cfg()
        with self.assertRaises(ValueError):
            self.app.save_config({"model": "iq3_s", "remove_api_key": 0})
        self.assertEqual(self.cfg(), before)

    def test_the_copy_endpoint_returns_the_full_key(self):
        key = "a" * 20
        self.app.save_config({"model": "iq3_s", "add_api_key": key})
        self.assertEqual(self.app.key("iq3_s", 0), {"key": key})
        with self.assertRaises(ValueError):
            self.app.key("iq3_s", 1)

    def test_config_never_returns_key_values(self):
        key = "a" * 20
        self.app.save_config({"model": "iq3_s", "add_api_key": key})
        out = self.app.config("iq3_s")
        self.assertEqual(out["api_key_count"], 1)
        self.assertEqual(out["api_keys"][0]["label"], "Key 1")
        self.assertEqual(out["api_keys"][0]["masked"], key[:4] + "…" + key[-4:])
        self.assertNotIn(key, json.dumps(out))
        self.assertNotIn("api_key", out)

    def test_a_short_key_is_refused(self):
        with self.assertRaises(ValueError):
            self.app.save_config({"model": "iq3_s", "api_key": "short"})

    def test_a_bad_port_changes_nothing(self):
        before = self.cfg()
        for value in (0, 70000, "eighty"):
            with self.assertRaises(ValueError):
                self.app.save_config({"model": "iq3_s", "port": value})
            self.assertEqual(self.cfg(), before)

    def test_only_the_named_keys_change(self):
        out = self.app.save_config({"model": "iq3_s", "port": 8081, "host": "127.0.0.1"})
        self.assertEqual(out["port"], 8081)
        cfg = self.cfg()
        for k in ("exe", "args", "model_name", "log", "keep_me"):
            self.assertEqual(cfg[k], CFG[k], k)
        self.assertNotIn("api_key", cfg)

    def test_an_unknown_model_is_refused(self):
        with self.assertRaises(Exception):
            self.app.save_config({"model": "not-installed", "port": 8081})

    def mtp_config(self):
        (self.root / "mtp" / "rt").mkdir(parents=True)
        cfg = dict(CFG)
        cfg["args"] = CFG["args"] + ["--mtp", str(self.root / "mtp" / "rt"), "--spec", "4",
                                     "--spec-min-p", "0.5"]
        (self.root / "strata-iq3_s.json").write_text(json.dumps(cfg), encoding="utf-8")
        return cfg

    def test_model_config_reads_engine_and_editable_keys(self):
        self.mtp_config()
        mc = self.app.model_config("iq3_s")
        self.assertTrue(mc["engine"]["mtp"])
        self.assertEqual(mc["engine"]["context"], 32768)
        self.assertEqual(mc["engine"]["kv"], "int8")
        self.assertEqual(mc["engine"]["spec"], 4)
        self.assertIn("sampling.temperature", [k["key"] for k in mc["editable"]["keys"]])
        self.assertIn("editable", mc)
        self.assertIn('"exe"', mc["raw"])

    def test_mtp_can_be_turned_off_and_on(self):
        self.mtp_config()
        self.app.save_model_config({"model": "iq3_s", "engine": {"mtp": False}})
        cfg = self.cfg()
        self.assertNotIn("--mtp", cfg["args"])
        self.assertNotIn("--spec", cfg["args"])
        self.assertNotIn("--spec-min-p", cfg["args"])
        self.app.save_model_config({"model": "iq3_s", "engine": {"mtp": True, "spec": 3, "spec_min_p": 0.4}})
        cfg = self.cfg()
        self.assertIn("--mtp", cfg["args"])
        self.assertEqual(cfg["args"][cfg["args"].index("--spec") + 1], "3")
        self.assertEqual(cfg["args"][cfg["args"].index("--spec-min-p") + 1], "0.4")

    def test_context_is_checked(self):
        with self.assertRaises(ValueError):
            self.app.save_model_config({"model": "iq3_s", "engine": {"context": 1024}})
        with self.assertRaises(ValueError):
            self.app.save_model_config({"model": "iq3_s", "engine": {"context": 999999}})

    def test_documented_keys_can_be_saved(self):
        self.app.save_model_config({"model": "iq3_s", "values": {"sampling.temperature": 0.6,
                                                                  "aliases": "one,two"}})
        cfg = self.cfg()
        self.assertEqual(cfg["sampling"]["temperature"], 0.6)
        self.assertEqual(cfg["aliases"], ["one", "two"])

    def test_raw_json_keeps_the_key(self):
        self.app.save_config({"model": "iq3_s", "host": "0.0.0.0", "generate_key": True})
        key = self.cfg()["api_key"]
        mc = self.app.model_config("iq3_s")
        raw = json.loads(mc["raw"])
        self.assertEqual(raw["api_key"], "__KEEP__")
        raw["port"] = 8081
        self.app.save_model_config({"model": "iq3_s", "raw": json.dumps(raw)})
        cfg = self.cfg()
        self.assertEqual(cfg["port"], 8081)
        self.assertEqual(cfg["api_key"], key)


class Http(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "setup.py").write_text("", encoding="utf-8")
        (self.root / "serve").mkdir()
        (self.root / "serve" / "server.py").write_text("", encoding="utf-8")
        (self.root / "strata-iq3_s.json").write_text(json.dumps({**CFG, "api_key": "a" * 20}),
                                                     encoding="utf-8")
        self.app = U.App(self.root, 0)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), U.Handler)
        self.port = self.server.server_address[1]
        self.server.app = self.app
        self.server.html = b"<script>const TOKEN = \"" + self.app.token.encode() + b"\";</script>"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_the_page_and_healthz_need_no_token(self):
        self.assertEqual(self.get("/")[0], 200)
        self.assertEqual(self.get("/healthz")[0], 200)

    def test_the_api_needs_the_page_token(self):
        self.assertEqual(self.get("/api/state")[0], 401)
        self.assertEqual(self.get("/api/state", {"X-Strata-Token": "wrong"})[0], 401)

    def test_a_foreign_host_is_refused(self):
        self.assertEqual(self.get("/", {"Host": "evil.example"})[0], 403)

    def test_the_copy_endpoint_needs_the_page_token(self):
        self.assertEqual(self.get("/api/key?model=iq3_s&index=0")[0], 401)
        status, body = self.get("/api/key?model=iq3_s&index=0", {"X-Strata-Token": self.app.token})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"key": "a" * 20})

    def test_a_post_needs_json_and_the_token(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/config", data=b"{}", method="POST",
                                     headers={"X-Strata-Token": self.app.token})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("a POST without a JSON content type was accepted")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 415)


if __name__ == "__main__":
    unittest.main()
