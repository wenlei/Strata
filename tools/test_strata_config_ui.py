"""tools/test_strata_config_ui.py - the settings page's config editing and its local-only HTTP surface.

    python -m unittest tools/test_strata_config_ui.py -v

No GPU, no model and no download: the Strata folder is a temporary one with a stand-in setup.py and serve/server.py.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from unittest import mock
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

    def test_local_host_commands_roundtrip(self):
        data_dir = self.root / "agent-data"
        with mock.patch.dict(os.environ, {"STRATA_AGENT_DATA_DIR": str(data_dir)}):
            self.app._save_local_plugins({"host_commands": ["echo hi", "  "]})
            self.assertEqual(self.app._local_plugins()["host_commands"], ["echo hi"])

    def test_local_directory_refs_roundtrip(self):
        data_dir = self.root / "agent-data"
        with mock.patch.dict(os.environ, {"STRATA_AGENT_DATA_DIR": str(data_dir)}):
            self.app._save_local_plugins({"directory_refs": {"FAMILY": r"C:\Temp"}})
            self.assertEqual(
                self.app._local_plugins()["directory_refs"], {"FAMILY": r"C:\Temp"}
            )

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

    def test_image_models_lists_qwen_files(self):
        model = self.root / "models" / "qwen-image-2.1-uc"
        for folder in ("diffusion_models", "text_encoders", "vae"):
            (model / folder).mkdir(parents=True)
        (model / "diffusion_models" / "qwen-image-2.1-UC-Q4_K_M.gguf").write_bytes(b"q4")
        (model / "diffusion_models" / "qwen-image-2.1-UC-Q8_0.gguf").write_bytes(b"q8")
        (model / "text_encoders" / "qwen3vl_8b_int8_convrot.safetensors").write_bytes(b"te")
        (model / "vae" / "qwen_image_2.1_vae_bf16.safetensors").write_bytes(b"vae")
        with mock.patch.dict("os.environ", {"STRATA_IMAGE_MODELS_DIR": str(self.root / "models")}):
            out = self.app.image_models(probe=False)
        self.assertEqual(len(out["models"]), 1)
        entry = out["models"][0]
        self.assertEqual(entry["name"], "Qwen-Image-2.1")
        self.assertTrue(entry["ready"])
        self.assertEqual([v["label"] for v in entry["variants"]], ["Q4_K_M", "Q8_0"])
        self.assertTrue(entry["variants"][0]["default"])
        self.assertEqual({v["role"] for v in entry["shared"]}, {"Text encoder", "VAE"})

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


class Dreaming(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "setup.py").write_text("", encoding="utf-8")
        (self.root / "serve").mkdir()
        (self.root / "serve" / "server.py").write_text("", encoding="utf-8")
        (self.root / "strata-iq3_s.json").write_text(json.dumps(CFG), encoding="utf-8")
        self.app = U.App(self.root, 8090)
        self.memories = [
            {"id": "one", "key": "language", "value": "Please use Chinese for every answer.",
             "scope": "global", "term": "long"},
            {"id": "two", "key": "paths", "value": "Use the folder C:\\Work\\Strata.",
             "scope": "global", "term": "long"},
            {"id": "session-one", "key": "reply style",
             "value": "The user wants short answers in this task.",
             "scope": "session:s1", "term": "short", "session_id": "s1"},
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def dream(self, operations, memories=None):
        answer = json.dumps({"summary": "organized", "operations": operations})
        with mock.patch.object(self.app, "agent_memories",
                               return_value={"memories": memories or self.memories}), \
             mock.patch.object(self.app, "_dream_completion", return_value=answer) as completion:
            result = self.app.dream_agent_memory()
        return result, completion

    def test_dreaming_promotes_a_repeated_session_memory(self):
        result, completion = self.dream([
            {"op": "update", "id": "session-one", "term": "long",
             "reason": "The same preference appears in several sessions."},
        ])
        self.assertEqual(result["scanned"], 3)
        self.assertEqual(len(result["changes"]), 1)
        change = result["changes"][0]
        self.assertEqual(change["op"], "update")
        self.assertEqual(change["id"], "session-one")
        self.assertEqual(change["term"], "long")
        self.assertEqual(change["scope"], "global")
        self.assertEqual(result["promoted"], 1)
        self.assertIn('"session-one"', completion.call_args.args[0])

    def test_dreaming_merges_splits_and_removes(self):
        memories = self.memories + [
            {"id": "dup", "key": "language", "value": "Answer in Chinese.",
             "scope": "global", "term": "long"},
        ]
        result, _ = self.dream([
            {"op": "update", "id": "one", "value": "Use Chinese for every answer.",
             "reason": "Merge the duplicate."},
            {"op": "delete", "id": "dup", "reason": "Merged into one."},
            {"op": "update", "id": "session-one", "value": "Short answers in this task.",
             "reason": "Keep the session part."},
            {"op": "create", "key": "reply-style", "value": "Short answers by default.",
             "term": "long", "reason": "Split the durable part out."},
        ], memories=memories)
        self.assertEqual(result["counts"], {"create": 1, "update": 2, "delete": 1})
        self.assertEqual(len(result["changes"]), 4)
        self.assertEqual({change["op"] for change in result["changes"]},
                         {"create", "update", "delete"})

    def test_dreaming_classifies_entries(self):
        result, _ = self.dream([
            {"op": "update", "id": "one", "category": "profile",
             "reason": "This is a personal preference."},
            {"op": "create", "key": "child", "value": "The user has one child.",
             "term": "long", "category": "profile", "reason": "A personal fact."},
        ])
        self.assertEqual(len(result["changes"]), 2)
        self.assertTrue(all(change["category"] == "profile"
                            for change in result["changes"]))

    def test_classify_assigns_categories_and_builds_an_index(self):
        answer = json.dumps({"operations": [
            {"op": "update", "id": "one", "category": "profile"},
            {"op": "update", "id": "two", "category": "environment"},
            {"op": "update", "id": "session-one", "category": "preference"},
        ]})
        with mock.patch.object(self.app, "agent_memories",
                               return_value={"memories": self.memories}), \
             mock.patch.object(self.app, "_dream_completion", return_value=answer):
            result = self.app.classify_agent_memory()
        self.assertEqual(result["classified"], 3)
        self.assertEqual(result["counts"], {"create": 1, "update": 3, "delete": 0})
        categories = {change["id"]: change["category"] for change in result["changes"]
                      if change["op"] == "update"}
        self.assertEqual(categories["one"], "profile")
        self.assertEqual(categories["two"], "environment")
        index = [change for change in result["changes"] if change["op"] == "create"]
        self.assertEqual(len(index), 1)
        self.assertEqual(index[0]["key"], "index:memory-categories")
        self.assertEqual(index[0]["category"], "reference")

    def test_merge_keeps_one_entry_and_deletes_the_old_ones(self):
        answer = json.dumps({"operations": [{
            "op": "merge", "ids": ["one", "two"],
            "key": "language-and-paths",
            "value": "Use Chinese. Use the folder C:\\Work\\Strata.",
            "category": "preference", "reason": "same setup",
        }]})
        with mock.patch.object(self.app, "agent_memories",
                               return_value={"memories": self.memories}), \
             mock.patch.object(self.app, "_dream_completion", return_value=answer):
            result = self.app.merge_agent_memory()
        self.assertEqual(result["merged"], 1)
        self.assertEqual(result["counts"], {"create": 0, "update": 1, "delete": 1})
        deleted = [change for change in result["changes"] if change["op"] == "delete"]
        self.assertEqual([change["id"] for change in deleted], ["two"])
        kept = [change for change in result["changes"] if change["op"] == "update"]
        self.assertEqual(kept[0]["id"], "one")
        self.assertEqual(kept[0]["key"], "language-and-paths")

    def test_condense_splits_a_long_memory(self):
        long_memory = {
            "id": "long", "key": "big", "value": "x" * 900,
            "scope": "global", "term": "long", "category": "project",
        }
        answer = json.dumps({"operations": [
            {"op": "update", "id": "long", "key": "big-1",
             "value": "x" * 300, "category": "project", "reason": "split"},
            {"op": "create", "key": "big-2", "value": "x" * 300,
             "term": "long", "category": "project", "reason": "split"},
        ]})
        with mock.patch.object(self.app, "agent_memories",
                               return_value={"memories": [long_memory]}), \
             mock.patch.object(self.app, "_dream_completion", return_value=answer):
            result = self.app.concise_agent_memory()
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["counts"], {"create": 1, "update": 1, "delete": 0})
        self.assertEqual(result["changes"][0]["id"], "long")
        self.assertEqual(result["changes"][1]["key"], "big-2")

    def test_identical_merge_guards_survivor_and_only_deletes_duplicate(self):
        original = {**self.memories[0], "session_id": "origin", "updated_at": "earlier"}
        duplicate = {**original, "id": "duplicate"}
        answer = json.dumps({"operations": [{
            "op": "merge", "ids": ["one", "duplicate"], "value": original["value"]
        }]})
        with mock.patch.object(self.app, "agent_memories",
                               return_value={"memories": [original, duplicate]}), \
             mock.patch.object(self.app, "_dream_completion", return_value=answer):
            preview = self.app.merge_agent_memory()
        self.assertEqual(preview["counts"], {"update": 0, "create": 0, "delete": 1})
        self.assertEqual(preview["changes"][0]["id"], "duplicate")
        guard, = preview["guards"]
        self.assertEqual(guard["id"], "one")
        self.assertEqual(guard["expected"]["session_id"], "origin")
        self.assertEqual(guard["expected"]["updated_at"], "earlier")
        with mock.patch.object(self.app, "_agent_target", return_value=(8080, "")), \
             mock.patch.object(U.mcp, "http_json", return_value=(409, {})) as http, \
             self.assertRaisesRegex(ValueError, "changed since preview"):
            self.app.apply_agent_memory_dream(preview)
        self.assertEqual(http.call_args.kwargs["body"]["guards"], [guard])

    def test_unchanged_merge_cannot_drop_a_distinct_fact(self):
        answer = json.dumps({"operations": [{
            "op": "merge", "ids": ["one", "two"], "value": self.memories[0]["value"]
        }]})
        with mock.patch.object(self.app, "agent_memories", return_value={"memories": self.memories}), \
             mock.patch.object(self.app, "_dream_completion", return_value=answer):
            self.assertEqual(self.app.merge_agent_memory()["changes"], [])

    def test_classify_keeps_the_full_value(self):
        long_value = "x" * 900
        memory = {"id": "long", "key": "big", "value": long_value,
                  "scope": "global", "term": "long", "category": "general"}
        answer = json.dumps({"operations": [
            {"op": "update", "id": "long", "category": "project"},
        ]})
        with mock.patch.object(self.app, "agent_memories",
                               return_value={"memories": [memory]}), \
             mock.patch.object(self.app, "_dream_completion", return_value=answer):
            result = self.app.classify_agent_memory()
        change = next(item for item in result["changes"]
                      if item.get("id") == "long")
        self.assertEqual(change["value"], long_value)

    def test_apply_refreshes_the_index_when_asked(self):
        preview, _ = self.dream([{"op": "update", "id": "one", "value": "Use Chinese."}])
        with mock.patch.object(self.app, "_agent_target", return_value=(8080, "")), \
             mock.patch.object(U.mcp, "http_json", return_value=(200, {"updated": 1})), \
             mock.patch.object(self.app, "refresh_memory_index",
                               return_value={"total": 3, "counts": {}}) as refresh:
            result = self.app.apply_agent_memory_dream({**preview, "refresh_index": True})
        refresh.assert_called_once()
        self.assertEqual(result["index"]["total"], 3)

    def test_user_indexes_are_preserved_by_refresh_and_classification(self):
        personal = {**self.memories[0], "key": "index:reading", "value": "Book list"}
        session = {**self.memories[2], "key": "index:memory-categories"}
        existing = {**self.memories[1], "key": "index:memory-categories"}
        for index in (None, existing):
            rows = [personal, session] + ([index] if index else [])
            with self.subTest(existing=bool(index)), \
                 mock.patch.object(self.app, "agent_memories", return_value={"memories": rows}), \
                 mock.patch.object(self.app, "_dream_completion",
                                   return_value='{"operations": []}'), \
                 mock.patch.object(self.app, "_agent_target", return_value=(8080, "")), \
                 mock.patch.object(U.mcp, "http_json", return_value=(200, {})) as http:
                preview = self.app.classify_agent_memory()
                refreshed = self.app.refresh_memory_index()
                self.assertEqual(refreshed["total"], 2)
                change, = preview["changes"]
                applied, = http.call_args.kwargs["body"]["changes"]
                self.assertEqual(change["value"], applied["value"])
                self.assertEqual(change["key"], "index:memory-categories")
                self.assertEqual(change["op"], "update" if index else "create")
                if index:
                    self.assertEqual(applied["id"], existing["id"])
                    self.assertEqual(applied["expected"]["value"], existing["value"])
                else:
                    self.assertTrue(applied["if_absent"])

    def test_stale_delete_uses_preview_snapshot(self):
        row = {**self.memories[0], "updated_at": "2026-01-01T00:00:00+00:00"}
        preview, _ = self.dream([{"op": "delete", "id": row["id"]}], [row])
        row["value"] = "newer content"
        def compare(url, **kwargs):
            operation, = kwargs["body"]["changes"]
            expected = operation["expected"]
            self.assertEqual(expected["value"], self.memories[0]["value"])
            self.assertEqual(expected["updated_at"], row["updated_at"])
            return (409, {}) if expected["value"] != row["value"] else (200, {"deleted": 1})
        with mock.patch.object(self.app, "_agent_target", return_value=(8080, "")), \
             mock.patch.object(U.mcp, "http_json", side_effect=compare), \
             self.assertRaisesRegex(ValueError, "changed since preview"):
            self.app.apply_agent_memory_dream(preview)
        self.assertEqual(row["value"], "newer content")

    def test_old_preview_cannot_delete_without_its_snapshot(self):
        with mock.patch.object(U.mcp, "http_json") as http, \
             self.assertRaisesRegex(ValueError, "generate a new"):
            self.app.apply_agent_memory_dream({
                "request_id": "old-preview",
                "changes": [{"op": "delete", "id": "one", "before": "old"}],
            })
        http.assert_not_called()

    def test_same_preview_replays_batch_after_index_failure(self):
        preview, _ = self.dream([{"op": "create", "key": "keep", "value": "new"},
                                 {"op": "delete", "id": "two"}])
        preview["refresh_index"] = True
        counts = {"updated": 0, "deleted": 1, "created": 1}
        with mock.patch.object(self.app, "_agent_target", return_value=(8080, "")), \
             mock.patch.object(U.mcp, "http_json", side_effect=[(200, counts.copy()),
                                                                (200, counts.copy())]) as http, \
             mock.patch.object(self.app, "refresh_memory_index",
                               side_effect=[ValueError("offline"), {"total": 2}]):
            with self.assertRaisesRegex(ValueError, "were applied; retry this preview"):
                self.app.apply_agent_memory_dream(preview)
            self.assertEqual(self.app.apply_agent_memory_dream(preview),
                             {**counts, "index": {"total": 2}})
        first, retry = http.call_args_list
        self.assertEqual(first, retry)

    def test_dreaming_skips_an_unchanged_memory(self):
        result, _ = self.dream([{"op": "update", "id": "one"}])
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["counts"], {"create": 0, "update": 0, "delete": 0})

    def test_dreaming_rejects_unknown_memory_ids(self):
        with self.assertRaisesRegex(ValueError, "memory not found"):
            self.dream([{"op": "update", "id": "missing", "value": "short"}])

    def test_dreaming_rejects_invalid_model_json(self):
        with mock.patch.object(self.app, "agent_memories",
                               return_value={"memories": self.memories}), \
             mock.patch.object(self.app, "_dream_completion", return_value="not json"), \
             self.assertRaises(ValueError):
            self.app.dream_agent_memory()

    def test_apply_dream_creates_updates_and_deletes(self):
        preview, _ = self.dream([
            {"op": "create", "key": "workspace", "value": "Strata workspace", "term": "long"},
            {"op": "update", "id": "session-one", "value": "Short answers.", "term": "long"},
            {"op": "delete", "id": "two"},
        ])
        counts = {"created": 1, "updated": 1, "deleted": 1}
        with mock.patch.object(self.app, "_agent_target", return_value=(8080, "secret")), \
             mock.patch.object(U.mcp, "http_json", return_value=(200, counts)) as http:
            result = self.app.apply_agent_memory_dream(preview)
        self.assertEqual(result, counts)
        http.assert_called_once()
        self.assertTrue(http.call_args.args[0].endswith("/v1/agent/memories/batch"))
        payload = http.call_args.kwargs["body"]
        self.assertEqual(payload["request_id"], preview["request_id"])
        create, update, delete = payload["changes"]
        self.assertEqual(create["op"], "create")
        self.assertEqual(update["expected"]["term"], "short")
        self.assertEqual(update["expected"]["session_id"], "s1")
        self.assertEqual(delete["expected"]["value"], self.memories[1]["value"])

    def test_preview_rejects_an_update_and_delete_of_one_memory(self):
        with self.assertRaisesRegex(ValueError, "updated and deleted"):
            self.dream([
                {"op": "update", "id": "one", "term": "long"},
                {"op": "delete", "id": "one"},
            ])


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
