#!/usr/bin/env python3
"""A local web page for Strata's settings: install and switch models, network, start and stop.

    CONFIG-UI.bat          (Windows)
    ./config-ui.sh         (Linux)

It listens on 127.0.0.1 only and calls the checked actions of tools/strata_mcp.py; nothing here runs a shell.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import shutil
import string
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = Path(__file__).resolve().parent
for _p in (TOOLS, ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import strata_mcp as mcp
from serve import runconfig

DEFAULT_PORT = 8090
MAX_BODY = 1 << 20
HOSTS = ("127.0.0.1", "0.0.0.0")
ENGINE_DEFAULTS = {"spec": 4, "spec_min_p": 0.5}
DREAM_MAX_ITEMS = 400
DREAM_MAX_CHARS = 160_000
DREAM_MAX_VALUE_CHARS = 2_000
DREAM_MAX_KEY_CHARS = 200
DREAM_MAX_CHANGES = 400
DREAM_MAX_OUTPUT_TOKENS = 16_384
MERGE_MAX_VALUE_CHARS = 1_200
MEMORY_INDEX_KEY = "index:memory-categories"
MEMORY_CATEGORIES = (
    "profile", "preference", "project", "environment",
    "reference", "issue", "contact", "general",
)
MEMORY_CATEGORY_TITLES = {
    "profile": "个人与家庭",
    "preference": "工作偏好",
    "project": "项目与任务",
    "environment": "主机/网络/工具",
    "reference": "外部资料",
    "issue": "问题与修复",
    "contact": "联系人",
    "general": "其他",
}
DREAM_SYSTEM_PROMPT = (
    "You maintain an assistant's memory. You receive session memory (term "
    "\"short\", scope \"session:<id>\") and long-term memory (term \"long\", "
    "scope \"global\").\n"
    "Do four jobs.\n"
    "1. Review every session memory. Promote one to long-term only when the "
    "user asked to remember it for later, when it is a stable preference, "
    "fact, decision, or constraint the user will need again, or when the same "
    "fact appears in several sessions or was recalled repeatedly. Do not "
    "promote transient task details, one-off errors, chat text, or facts "
    "useful only in that conversation.\n"
    "2. Make every long-term entry short and focused: one fact or one small "
    "topic per entry, as short as possible while keeping every fact, number, "
    "date, path, URL, command, name, error, uncertainty, and constraint. Every "
    "long-term value longer than 400 characters MUST be split into several "
    "short entries with their own keys; do not leave such an entry unchanged. "
    "An entry may stay unchanged only when its value is 400 characters or "
    "less. Do not merge unrelated facts into one entry.\n"
    "3. Classify every entry with exactly one category: profile (identity, "
    "family, personal facts, preferences and habits), preference (how the "
    "assistant should work), project (ongoing projects, code, tasks and plans), "
    "environment (hardware, OS, network, paths and installed tools), reference "
    "(external sources, URLs and documents), issue (bugs, incidents, root "
    "causes and fixes), contact (people and contact details), general (anything "
    "else). Use a short, stable key, for example language or nas-address. "
    "Personal facts about the user belong in profile; store one fact per "
    "entry.\n"
    "4. Merge exact duplicates, delete entries only when another operation "
    "keeps every fact from them, and return only the operations that change "
    "something. Omit a memory that stays the same. Never add a fact. Write in "
    "the original language; Chinese input stays Chinese. Never translate.\n"
    "Return JSON only, with this shape:\n"
    '{"summary":"one sentence","operations":[{"op":"update","id":"existing id",'
    '"key":"new key","value":"new value","term":"long","category":"profile",'
    '"reason":"why"},{"op":"create","key":"new key","value":"new value",'
    '"term":"long","category":"profile","session_id":"only for a short create",'
    '"reason":"why"},{"op":"delete","id":"existing id","reason":"why"}]}\n'
    "To promote, use update with term \"long\"; it becomes global. To merge, "
    "update the entry to keep, then delete the others only if every fact from "
    "them is kept. To split, update the original with one part, then create the "
    "other parts. A short create needs the session_id of an existing session "
    "memory."
)
MERGE_SYSTEM_PROMPT = (
    "You merge an assistant's long-term memories. Find groups of entries that "
    "describe the same topic, issue, project, preference, or person. Merge each "
    "group into ONE short entry. Keep every fact, number, date, path, URL, "
    "command, name, error, uncertainty, and constraint. Never add a fact. Do "
    "not merge unrelated topics. The merged value must be 1,200 characters or "
    "less; if a group cannot fit, leave it unmerged. Use the group's category "
    "and a short, stable key.\n"
    "The first id in each group is the entry to keep; the other ids in that "
    "group will be deleted after the merge, so the merged value must keep every "
    "fact from all of them. Return JSON only, with this shape:\n"
    '{"operations":[{"op":"merge","ids":["id-1","id-2"],"key":"merged-key",'
    '"value":"merged value","category":"issue","reason":"same issue"}]}\n'
    "Include every group you can merge. Do not include a group of one entry."
)
CONCISE_SYSTEM_PROMPT = (
    "You shorten long saved memories. For each memory you receive, split it "
    "into one or more short entries. Each new value must be 400 characters or "
    "less and must contain one fact or one small topic. Keep every fact, "
    "number, date, path, URL, command, name, error, uncertainty, and "
    "constraint. Never add a fact. Keep the same category and language. Use a "
    "short, stable key; when splitting, add a short suffix to the original key.\n"
    "Do not delete the original entry: update it with the first part, then "
    "create the other parts. If an entry is already 400 characters or less, "
    "omit it. Return JSON only, with this shape:\n"
    '{"operations":[{"op":"update","id":"existing id","key":"part-1",'
    '"value":"part 1","category":"profile","reason":"split"},'
    '{"op":"create","key":"part-2","value":"part 2","category":"profile",'
    '"term":"long","reason":"split"}]}'
)
CLASSIFY_SYSTEM_PROMPT = (
    "You classify an assistant's saved memories. Assign exactly one category "
    "to every memory you receive.\n"
    "Categories: profile (identity, family, personal facts, preferences and "
    "habits), preference (how the assistant should work), project (ongoing "
    "projects, code, tasks and plans), environment (hardware, OS, network, "
    "paths and installed tools), reference (external sources, URLs and "
    "documents), issue (bugs, incidents, root causes and fixes), contact "
    "(people and contact details), general (anything else).\n"
    "Personal facts about the user belong in profile. Do not change a key or a "
    "value. Return JSON only, with this shape:\n"
    '{"operations":[{"op":"update","id":"existing id","category":"profile"}]}'
)
_FOLDER_LOCK = threading.Lock()


def lan_addresses() -> list[str]:
    found = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            found.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    out = []
    for ip in found:
        if ip.startswith(("127.", "169.254.", "0.")):
            continue
        try:
            socket.inet_aton(ip)
        except OSError:
            continue
        out.append(ip)
    return sorted(out, key=lambda x: tuple(int(p) for p in x.split(".")))


def lan_urls(port: int) -> list[str]:
    return [f"http://{ip}:{port}" for ip in lan_addresses()]


class App:
    def __init__(self, root: Path, port: int):
        self.root = root.resolve()
        self.port = port
        self.strata = mcp.Strata(self.root)
        self.tools = mcp.Tools(self.strata)
        self.token = secrets.token_urlsafe(32)
        self._state_lock = threading.Lock()
        self._state = None
        self._state_at = 0.0
        base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
                    or (Path.home() / ".local" / "share"))
        self.store_dir = base / "Strata" / "config-ui"
        self.store_dir.mkdir(parents=True, exist_ok=True)
        # Live console: only a server this page started has its stdout attached here.
        self._console_lines: list[str] = []
        self._console_seq = 0
        self._console_base = 0
        self._console_lock = threading.Lock()
        self._console_proc: subprocess.Popen | None = None
        self._console_log = self.root / ".strata-mcp" / "server.log"

    def state(self, fresh: bool = False) -> dict:
        with self._state_lock:
            if not fresh and self._state and time.time() - self._state_at < 4:
                return self._state
            status = self.tools.strata_status(hardware=True)
            models = self.tools.strata_models()
            installed = status.get("installed") or self.strata.installed()
            lan = {}
            ports = {m.get("port") for m in installed.get("models", []) if isinstance(m.get("port"), int)}
            for p in sorted(ports):
                lan[p] = lan_urls(p)
            self._state = {"status": status, "models": models, "installed": installed,
                           "image_models": self.image_models(), "lan": lan}
            self._state_at = time.time()
            return self._state

    @staticmethod
    def _image_role(name: str) -> str:
        lower = name.lower()
        if "vae" in lower:
            return "VAE"
        if "text" in lower or "clip" in lower or "encoder" in lower:
            return "Text encoder"
        return "Support"

    @staticmethod
    def _is_image_model_dir(path: Path) -> bool:
        try:
            names = {child.name.lower() for child in path.iterdir() if child.is_dir()}
        except OSError:
            return False
        return any(path.glob("*.gguf")) or bool(
            names & {"diffusion_models", "text_encoders", "vae", "unet", "checkpoints"}
        )

    @staticmethod
    def _comfy_status(probe: bool = True) -> dict:
        url = os.environ.get("STRATA_COMFYUI_URL", "http://127.0.0.1:8188").strip().rstrip("/")
        out = {"url": url, "running": False, "queue": None}
        if not probe:
            return out
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((host, port), timeout=0.35):
                pass
        except OSError:
            return out
        status, body = mcp.http_json(url + "/system_stats", timeout=1.5)
        if status == 200 and isinstance(body, dict):
            out["running"] = True
            info = body.get("exec_info") if isinstance(body.get("exec_info"), dict) else {}
            queue = info.get("queue_remaining")
            if isinstance(queue, int):
                out["queue"] = queue
        return out

    def image_models(self, probe: bool = True) -> dict:
        """Qwen-Image-2.1 files on disk and the ComfyUI server that runs them."""
        configured = os.environ.get("STRATA_IMAGE_MODELS_DIR", "").strip()
        root = Path(configured).expanduser() if configured else Path.home() / "lumWorkspace" / "models"
        comfy = self._comfy_status(probe)
        found = []
        if root.is_dir():
            if self._is_image_model_dir(root):
                directories = [root]
            else:
                directories = [p for p in root.iterdir()
                               if p.is_dir() and self._is_image_model_dir(p)]
            for directory in directories:
                variants, shared, total = [], [], 0
                for path in sorted(directory.rglob("*"), key=lambda item: str(item).lower()):
                    if not path.is_file() or path.suffix.lower() not in (".gguf", ".safetensors", ".pt", ".pth", ".bin"):
                        continue
                    try:
                        size = path.stat().st_size
                    except OSError:
                        continue
                    total += size
                    relative = str(path.relative_to(directory)).lower()
                    item = {"file": path.name, "gb": round(size / 1e9, 2)}
                    if path.suffix.lower() == ".gguf":
                        label = path.stem
                        for prefix in (directory.name.lower() + "-", "qwen-image-2.1-uc-", "qwen-image-2.1-"):
                            if label.lower().startswith(prefix):
                                label = label[len(prefix):]
                                break
                        item.update({"label": label or path.stem,
                                     "default": "q4" in label.lower() and "q8" not in label.lower()})
                        variants.append(item)
                    else:
                        if "vae" in relative:
                            role = "VAE"
                        elif "text" in relative or "clip" in relative or "encoder" in relative:
                            role = "Text encoder"
                        else:
                            role = self._image_role(path.name)
                        item.update({"role": role})
                        shared.append(item)
                if variants:
                    found.append({
                        "name": "Qwen-Image-2.1" if "qwen-image" in directory.name.lower() else directory.name,
                        "path": str(directory),
                        "variants": variants,
                        "shared": shared,
                        "total_gb": round(total / 1e9, 2),
                        "ready": bool(variants) and any(x.get("role") == "Text encoder" for x in shared)
                                 and any(x.get("role") == "VAE" for x in shared),
                        "comfyui": comfy,
                    })
        return {"directory": str(root), "models": found, "comfyui": comfy}

    @staticmethod
    def _keys(cfg: dict) -> list[str]:
        value = cfg.get("api_key")
        if isinstance(value, (list, tuple)):
            return [str(k).strip() for k in value if str(k).strip()]
        if value is None or value == "":
            return []
        return [k.strip() for k in str(value).split(",") if k.strip()]

    @staticmethod
    def _mask(key: str) -> str:
        key = str(key)
        if len(key) <= 8:
            return key[:2] + "…" + key[-2:] if len(key) > 4 else "…"
        return key[:4] + "…" + key[-4:]

    @staticmethod
    def _store_keys(cfg: dict, keys: list[str]) -> None:
        if not keys:
            cfg.pop("api_key", None)
        elif len(keys) == 1:
            cfg["api_key"] = keys[0]
        else:
            cfg["api_key"] = keys

    def config(self, model: str) -> dict:
        path = self.strata.find_config(model)
        cfg = self.strata.read_config(path)
        desc = self.strata.describe_config(path)
        port = int(cfg.get("port") or mcp.DEFAULT_PORT)
        keys = self._keys(cfg)
        return {"model": desc["model"], "file": path.name, "model_name": cfg.get("model_name"),
                "host": cfg.get("host") or "127.0.0.1", "port": port,
                "api_key_set": bool(keys), "api_key_count": len(keys),
                "api_keys": [{"index": i, "label": f"Key {i + 1}", "masked": self._mask(k)}
                             for i, k in enumerate(keys)],
                "context": desc.get("context"),
                "images": desc.get("images"), "kv": desc.get("kv"), "gpu": desc.get("gpu"),
                "low_ram": desc.get("low_ram"), "ready": desc.get("ready"),
                "missing_files": desc.get("missing_files"), "start_script": desc.get("start_script"),
                "chat_url": f"http://127.0.0.1:{port}/", "lan_urls": lan_urls(port)}

    def key(self, model: str, index: int) -> dict:
        path = self.strata.find_config(model)
        keys = self._keys(self.strata.read_config(path))
        if not 0 <= index < len(keys):
            raise ValueError("no key at that index")
        return {"key": keys[index]}

    def save_config(self, body: dict) -> dict:
        model = str(body.get("model") or "").strip()
        if not model:
            raise ValueError("model is required")
        path = self.strata.find_config(model)
        cfg = self.strata.read_config(path)
        if body.get("host") is not None:
            host = str(body["host"]).strip()
            if host not in HOSTS:
                raise ValueError("host must be 127.0.0.1 (this PC only) or 0.0.0.0 (the local network)")
            cfg["host"] = host
        port = body.get("port")
        if port not in (None, ""):
            try:
                port = int(port)
            except (TypeError, ValueError):
                raise ValueError("port must be a whole number") from None
            if not 1 <= port <= 65535:
                raise ValueError("port must be 1..65535")
            cfg["port"] = port
        keys = self._keys(cfg)
        if body.get("generate_key"):
            keys = [secrets.token_urlsafe(32)]
        elif body.get("clear_key"):
            keys = []
        elif body.get("api_key") is not None:
            key = str(body["api_key"]).strip()
            if len(key) < 16:
                raise ValueError("an API key needs at least 16 characters (use Generate)")
            if any(ord(c) < 32 for c in key):
                raise ValueError("an API key must be one line")
            keys = [key]
        if body.get("add_api_key") is not None:
            key = str(body["add_api_key"]).strip()
            if len(key) < 16:
                raise ValueError("an API key needs at least 16 characters (use Generate)")
            if any(ord(c) < 32 for c in key):
                raise ValueError("an API key must be one line")
            if key in keys:
                raise ValueError("that key is already configured")
            keys.append(key)
        if body.get("remove_api_key") is not None:
            try:
                index = int(body["remove_api_key"])
            except (TypeError, ValueError):
                raise ValueError("remove_api_key must be a key index") from None
            if not 0 <= index < len(keys):
                raise ValueError("remove_api_key: no key at that index")
            del keys[index]
        self._store_keys(cfg, keys)
        self._write_config(path, cfg)
        return self.config(model)

    @staticmethod
    def _write_config(path: Path, cfg: dict) -> None:
        key = cfg.get("api_key")
        if isinstance(key, (list, tuple)):
            if not key or any(not str(k).strip() or any(ord(c) < 32 for c in str(k)) for k in key):
                raise ValueError("an API key list needs non-empty keys without control characters")
        elif key is not None and (not str(key).strip() or any(ord(c) < 32 for c in str(key))):
            raise ValueError("an API key must be one line and not empty")
        if (cfg.get("host") or "127.0.0.1") != "127.0.0.1" and not key:
            raise ValueError("a key is required when other PCs can reach the server")
        backup = path.with_name(path.name + ".bak")
        backup.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _arg(cfg: dict, flag: str):
        args = [str(x) for x in (cfg.get("args") if isinstance(cfg.get("args"), list) else [])]
        return args[args.index(flag) + 1] if flag in args[:-1] else None

    @staticmethod
    def _set_arg(cfg: dict, flag: str, value) -> None:
        args = [str(x) for x in (cfg.get("args") if isinstance(cfg.get("args"), list) else [])]
        if flag in args[:-1]:
            i = args.index(flag)
            if value is None:
                del args[i:i + 2]
            else:
                args[i + 1] = str(value)
        elif value is not None:
            args += [flag, str(value)]
        cfg["args"] = args

    def model_config(self, model: str) -> dict:
        path = self.strata.find_config(model)
        cfg = self.strata.read_config(path)
        raw = json.loads(json.dumps(cfg))
        if raw.get("api_key"):
            raw["api_key"] = "__KEEP__"
        engine = {
            "context": self._arg(cfg, "--max-context"),
            "kv": self._arg(cfg, "--kv"),
            "kv_resident": self._arg(cfg, "--kv-resident"),
            "gpu": cfg.get("gpu"),
            "gpus": cfg.get("gpus"),
            "mtp": self._arg(cfg, "--mtp") is not None,
            "spec": self._arg(cfg, "--spec"),
            "spec_min_p": self._arg(cfg, "--spec-min-p"),
        }
        for k in ("context", "kv_resident", "spec"):
            if isinstance(engine[k], str) and engine[k].isdigit():
                engine[k] = int(engine[k])
        try:
            if engine["spec_min_p"] is not None:
                engine["spec_min_p"] = float(engine["spec_min_p"])
        except (TypeError, ValueError):
            engine["spec_min_p"] = None
        return {"model": self.strata.describe_config(path)["model"], "file": path.name,
                "editable": runconfig.view(cfg, path), "engine": engine,
                "engine_flags": {"kv": ["int8", "q4_0", "k8v4"]},
                "raw": json.dumps(raw, indent=1, ensure_ascii=False)}

    def save_model_config(self, body: dict) -> dict:
        model = str(body.get("model") or "").strip()
        if not model:
            raise ValueError("model is required")
        path = self.strata.find_config(model)
        cfg = self.strata.read_config(path)
        old_key = cfg.get("api_key")
        if body.get("raw") not in (None, ""):
            raw = body["raw"]
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raise ValueError("the JSON is not valid") from None
            if not isinstance(raw, dict):
                raise ValueError("the JSON must be an object")
            if raw.get("api_key") in ("__KEEP__", None):
                if old_key:
                    raw["api_key"] = old_key
                else:
                    raw.pop("api_key", None)
            cfg = raw
        if body.get("values"):
            if not isinstance(body["values"], dict):
                raise ValueError("values must be a JSON object")
            cfg, _changed = runconfig.apply(cfg, body["values"])
        engine = body.get("engine") or {}
        if engine:
            if not isinstance(engine, dict):
                raise ValueError("engine must be a JSON object")
            mtp_off = engine.get("mtp") is False
            if "context" in engine and engine["context"] not in (None, ""):
                try:
                    context = int(engine["context"])
                except (TypeError, ValueError):
                    raise ValueError("context must be a whole number") from None
                if not 8192 <= context <= 262144:
                    raise ValueError("context must be 8192..262144 (run setup for a longer context)")
                self._set_arg(cfg, "--max-context", context)
            if "kv" in engine:
                kv = engine["kv"] or None
                if kv is not None and kv not in ("int8", "q4_0", "k8v4"):
                    raise ValueError("kv must be int8, q4_0 or k8v4")
                self._set_arg(cfg, "--kv", kv)
            if "gpu" in engine:
                gpu = engine["gpu"]
                if gpu in (None, ""):
                    cfg.pop("gpu", None)
                else:
                    try:
                        cfg["gpu"] = int(gpu)
                    except (TypeError, ValueError):
                        raise ValueError("gpu must be a whole number") from None
            if engine.get("mtp") is False:
                for flag in ("--mtp", "--spec", "--spec-min-p"):
                    self._set_arg(cfg, flag, None)
            elif engine.get("mtp") is True:
                mtp = self._arg(cfg, "--mtp") or str(self.strata.data_dir() / "mtp" / "rt")
                if not Path(mtp).exists():
                    raise ValueError(f"the MTP files are missing at {mtp}: run setup again")
                self._set_arg(cfg, "--mtp", mtp)
                self._set_arg(cfg, "--spec", self._arg(cfg, "--spec") or ENGINE_DEFAULTS["spec"])
                self._set_arg(cfg, "--spec-min-p", self._arg(cfg, "--spec-min-p") or ENGINE_DEFAULTS["spec_min_p"])
            if "spec" in engine and not mtp_off and engine["spec"] not in (None, ""):
                try:
                    spec = int(engine["spec"])
                except (TypeError, ValueError):
                    raise ValueError("spec must be a whole number") from None
                if not 1 <= spec <= 8:
                    raise ValueError("spec must be 1..8")
                self._set_arg(cfg, "--spec", spec)
            if "spec_min_p" in engine and not mtp_off and engine["spec_min_p"] not in (None, ""):
                try:
                    p = float(engine["spec_min_p"])
                except (TypeError, ValueError):
                    raise ValueError("spec_min_p must be a number") from None
                if not 0 <= p <= 1:
                    raise ValueError("spec_min_p must be 0..1")
                self._set_arg(cfg, "--spec-min-p", p)
        self._write_config(path, cfg)
        return self.model_config(model)

    def _checked(self, name: str, body: dict) -> dict:
        defs = {d["name"]: d for d in self.tools.defs()}
        return mcp.check_schema(defs[name]["inputSchema"], body or {})

    def plan(self, body: dict) -> dict:
        args = self._checked("strata_install", {**body, "confirm": False})
        return self.tools.strata_install(**args)

    def install(self, body: dict) -> dict:
        status = self.tools.strata_status(hardware=False)
        if status.get("running") or status.get("loading"):
            raise mcp.ToolError("stop the running model first: setup may replace the engine files it uses")
        args = self._checked("strata_install", {**body, "confirm": True})
        return self.tools.strata_install(**args)

    def cancel_install(self) -> dict:
        return self.tools.strata_install(cancel=True)

    def start(self, body: dict) -> dict:
        return self.tools.strata_start(**self._checked("strata_start", body))

    def stop(self, body: dict) -> dict:
        return self.tools.strata_stop(**self._checked("strata_stop", body))

    def headless(self, body: dict) -> dict:
        """Start or stop the model server exactly like _run_headless.bat does.

        Stop finds whoever listens on 8080 and ends it; start launches the batch file
        detached so its output keeps going to .strata-mcp/server.log.
        """
        action = (body.get("action") or "").strip().lower()
        if action == "stop":
            pid = self._port_pid(8080)
            if not pid:
                return {"summary": "nothing is listening on 8080", "pid": None}
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=20)
            if self._wait_port_free(8080, 5):
                return {"summary": f"stopped pid {pid}", "pid": pid}
            # A server started from another console can refuse taskkill; the CIM provider runs as SYSTEM.
            self._wmi_terminate(pid)
            if self._wait_port_free(8080, 8):
                return {"summary": f"stopped pid {pid} (WMI fallback)", "pid": pid}
            raise mcp.ToolError(f"could not stop pid {pid} on port 8080")
        if action == "start":
            if self._port_pid(8080):
                return {"summary": "8080 is already listening"}
            bat = ROOT / "_run_console.bat"
            if not bat.is_file():
                raise mcp.ToolError("_run_console.bat is missing next to setup.py")
            flags = 0
            startup = None
            if os.name == "nt":
                # Strata only prints its progress when it has a console; keep one, minimized.
                flags = subprocess.CREATE_NEW_CONSOLE
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 7  # SW_SHOWMINNOACTIVE
            with self._console_lock:
                self._console_lines = []
                self._console_base = 0
            # Never hand the child a pipe owned by this page: if the page is
            # restarted, writes to a pipe with no reader block the server while
            # it holds its status lock. The log file is safe.
            self._console_log.parent.mkdir(parents=True, exist_ok=True)
            log_path = self._console_log
            try:
                log_handle = log_path.open("ab", buffering=0)
            except PermissionError:
                # A previous server may still hold the old file open.
                log_path = log_path.with_name(
                    f"server-{time.strftime('%Y%m%d-%H%M%S')}.log"
                )
                log_handle = log_path.open("ab", buffering=0)
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONUTF8="1")
            try:
                proc = subprocess.Popen(
                    ["cmd", "/c", str(bat)] if os.name == "nt" else ["/bin/sh", str(bat)],
                    cwd=str(ROOT), creationflags=flags, startupinfo=startup, env=env,
                    stdin=subprocess.DEVNULL, stdout=log_handle,
                    stderr=subprocess.STDOUT, close_fds=True,
                )
            finally:
                log_handle.close()
            self._console_proc = proc
            return {"summary": f"starting; output goes to {log_path}"}
        raise mcp.ToolError("action must be start or stop")

    def _pump_console(self, proc: subprocess.Popen) -> None:
        log = self._console_log
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("w", encoding="utf-8", errors="replace") as handle:
                for line in proc.stdout:
                    body = line.rstrip("\n")
                    text = (time.strftime("%H:%M:%S ") + body) if body.strip() else ""
                    handle.write(text + "\n")
                    handle.flush()
                    with self._console_lock:
                        self._console_lines.append(text)
                        if len(self._console_lines) > 5000:
                            drop = len(self._console_lines) - 4000
                            del self._console_lines[:drop]
                            self._console_base += drop
        except (OSError, ValueError):
            pass

    def console(self, query: dict) -> dict:
        since = int(query.get("since") or 0)
        with self._console_lock:
            start = max(since, self._console_base)
            skip = start - self._console_base
            lines = self._console_lines[skip:]
            seq = self._console_base + len(self._console_lines)
            alive = self._console_proc is not None and self._console_proc.poll() is None
        return {"seq": seq, "lines": lines, "attached": alive}

    @staticmethod
    def _port_pid(port: int) -> int | None:
        try:
            out = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=15
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
                try:
                    return int(parts[-1])
                except ValueError:
                    return None
        return None

    @staticmethod
    def _wait_port_free(port: int, seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if App._port_pid(port) is None:
                return True
            time.sleep(0.5)
        return False

    @staticmethod
    def _wmi_terminate(pid: int) -> None:
        ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if not ps.is_file():
            return
        subprocess.run(
            [str(ps), "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' | "
             f"Invoke-CimMethod -MethodName Terminate | Out-Null"],
            capture_output=True, timeout=40,
        )

    def logs(self, query: dict) -> dict:
        body = {k: query.get(k) for k in ("source", "model", "lines") if query.get(k) not in (None, "")}
        if "lines" in body:
            body["lines"] = int(body["lines"])
        return self.tools.strata_logs(**self._checked("strata_logs", body))

    def connect(self, port: int | None) -> dict:
        return self.tools.strata_connect_info(port=port)

    def _running_target(self):
        status = self.tools.strata_status(hardware=False)
        running = status.get("running") or []
        if not running:
            loading = status.get("loading") or {}
            port = loading.get("port")
            if port is None:
                return None, None
            port = int(port)
        else:
            port = int(running[0]["port"])
        key = self.strata.api_key_for(port)
        if isinstance(key, (list, tuple)):
            key = next((k for k in key if isinstance(k, str) and k.strip()), None)
        return port, key

    def tasks(self) -> dict:
        port, key = self._running_target()
        if port is None:
            return {"tasks": [], "finished": [], "port": None, "summary": "no running model"}
        status, body = mcp.http_json(f"http://127.0.0.1:{port}/v1/tasks", api_key=key, timeout=5)
        if status != 200 or not isinstance(body, dict):
            raise ValueError(f"cannot read tasks from port {port} (HTTP {status})")
        body["port"] = port
        return body

    def stop_task(self, body: dict) -> dict:
        task_id = str(body.get("id") or "").strip()
        if not task_id:
            raise ValueError("id is required")
        port, key = self._running_target()
        if port is None:
            raise ValueError("no running model")
        status, result = mcp.http_json(f"http://127.0.0.1:{port}/v1/tasks/{task_id}/stop",
                                       method="POST", body={}, api_key=key, timeout=10)
        if status not in (200, 404):
            raise ValueError(f"stop failed (HTTP {status})")
        return result if isinstance(result, dict) else {"id": task_id, "state": "unknown"}

    def clear_tasks(self) -> dict:
        port, key = self._running_target()
        if port is None:
            return {"cleared": 0}
        status, body = mcp.http_json(f"http://127.0.0.1:{port}/v1/tasks/clear",
                                     method="POST", body={}, api_key=key, timeout=5)
        if status != 200:
            raise ValueError(f"clear failed (HTTP {status})")
        return body if isinstance(body, dict) else {"cleared": 0}

    def _agent_data_dir(self) -> Path:
        explicit = os.environ.get("STRATA_AGENT_DATA_DIR", "").strip()
        if explicit:
            return Path(explicit).expanduser()
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            return Path(base) / "lite-strata-agent"
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return Path(base) / "lite-strata-agent"

    def _local_plugins(self) -> dict:
        path = self._agent_data_dir() / "plugins.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        raw_web = data.get("web") if isinstance(data.get("web"), dict) else {}
        web = {key: value for key, value in raw_web.items() if key != "api_key"}
        web["api_key_set"] = bool(str(raw_web.get("api_key") or "").strip())
        return {
            "plugins": [],
            "web": web,
            "agent": data.get("agent") if isinstance(data.get("agent"), dict) else {},
            "sandbox": data.get("sandbox") if isinstance(data.get("sandbox"), dict) else {},
            "host_commands": data.get("host_commands") if isinstance(data.get("host_commands"), list) else [],
            "directory_refs": data.get("directory_refs") if isinstance(data.get("directory_refs"), dict) else {},
            "scopes": data.get("scopes") if isinstance(data.get("scopes"), list) else [],
            "port": None,
        }

    def _save_local_plugins(self, body: dict) -> dict:
        path = self._agent_data_dir() / "plugins.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if isinstance(body.get("disabled"), list):
            data["disabled"] = [str(item) for item in body["disabled"]]
        web = body.get("web")
        if isinstance(web, dict):
            merged = dict(data.get("web") or {})
            for key in ("provider", "url", "allow_fake_ip", "fake_ip_networks"):
                if key in web:
                    merged[key] = web[key]
            if web.get("clear_api_key"):
                merged["api_key"] = ""
            elif isinstance(web.get("api_key"), str) and web["api_key"].strip():
                merged["api_key"] = web["api_key"].strip()
            data["web"] = merged
        for key in ("agent", "sandbox"):
            value = body.get(key)
            if isinstance(value, dict):
                data[key] = value
        if isinstance(body.get("host_commands"), list):
            data["host_commands"] = [
                str(item).strip() for item in body["host_commands"] if str(item).strip()
            ]
        if isinstance(body.get("directory_refs"), dict):
            data["directory_refs"] = {
                str(name).strip(): str(path).strip()
                for name, path in body["directory_refs"].items()
                if str(name).strip() and str(path).strip()
            }
        if isinstance(body.get("scopes"), list):
            data["scopes"] = body["scopes"]
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        temp.replace(path)
        return self._local_plugins()

    def _agent_target(self) -> tuple[int, str]:
        port, key = self._running_target()
        if port is None:
            raise ValueError("no running model")
        if not key:
            raise ValueError("no API key configured for the running model")
        return port, key

    def plugins(self) -> dict:
        try:
            port, key = self._agent_target()
            status, body = mcp.http_json(
                f"http://127.0.0.1:{port}/v1/agent/plugins", api_key=key, timeout=5
            )
            if status != 200 or not isinstance(body, dict):
                raise ValueError(f"cannot read agent plugins from port {port} (HTTP {status})")
            body["port"] = port
            return body
        except ValueError as error:
            if "no running model" not in str(error):
                raise
            return self._local_plugins()

    def save_plugins(self, body: dict) -> dict:
        try:
            port, key = self._agent_target()
            status, result = mcp.http_json(
                f"http://127.0.0.1:{port}/v1/agent/plugins",
                method="POST", body=body, api_key=key, timeout=10,
            )
            if status != 200 or not isinstance(result, dict):
                raise ValueError(f"cannot save agent plugins (HTTP {status})")
            result["port"] = port
            return result
        except ValueError as error:
            if "no running model" not in str(error):
                raise
            return self._save_local_plugins(body)

    def schedules(self) -> dict:
        port, key = self._agent_target()
        status, body = mcp.http_json(
            f"http://127.0.0.1:{port}/v1/agent/schedules",
            api_key=key, timeout=5,
        )
        if status != 200 or not isinstance(body, dict):
            raise ValueError(f"cannot read schedules from port {port} (HTTP {status})")
        body["port"] = port
        return body

    def create_schedule(self, body: dict) -> dict:
        content = str(body.get("content") or "").strip()
        if not content:
            raise ValueError("content is required")
        payload: dict = {"content": content}
        for field in ("delay_s", "interval_s", "run_at", "session_id"):
            value = body.get(field)
            if value not in (None, ""):
                payload[field] = value
        port, key = self._agent_target()
        status, result = mcp.http_json(
            f"http://127.0.0.1:{port}/v1/agent/schedules",
            method="POST", body=payload, api_key=key, timeout=10,
        )
        if status != 200 or not isinstance(result, dict):
            raise ValueError(f"cannot create schedule (HTTP {status})")
        result["port"] = port
        return result

    def delete_schedule(self, schedule_id: str) -> dict:
        schedule_id = str(schedule_id or "").strip()
        if not schedule_id:
            raise ValueError("schedule id is required")
        port, key = self._agent_target()
        encoded = urllib.parse.quote(schedule_id, safe="")
        status, result = mcp.http_json(
            f"http://127.0.0.1:{port}/v1/agent/schedules/{encoded}",
            method="DELETE", api_key=key, timeout=10,
        )
        if status not in (200, 404):
            raise ValueError(f"cannot delete schedule (HTTP {status})")
        return {"deleted": schedule_id}

    def agent_sessions(self) -> dict:
        port, key = self._agent_target()
        status, body = mcp.http_json(
            f"http://127.0.0.1:{port}/v1/agent/sessions?limit=100",
            api_key=key, timeout=5,
        )
        if status != 200 or not isinstance(body, dict):
            raise ValueError(f"cannot read agent sessions from port {port} (HTTP {status})")
        body["port"] = port
        return body

    def agent_session(self, session_id: str) -> dict:
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("id is required")
        port, key = self._agent_target()
        encoded = urllib.parse.quote(session_id, safe="")
        status, body = mcp.http_json(
            f"http://127.0.0.1:{port}/v1/agent/sessions/{encoded}",
            api_key=key, timeout=5,
        )
        if status != 200 or not isinstance(body, dict):
            raise ValueError(f"cannot read that agent session (HTTP {status})")
        body["port"] = port
        return body

    def agent_memories(self, query: str = "", category: str = "", *,
                       scope: str = "", limit: int = 500) -> dict:
        port, key = self._agent_target()
        url = f"http://127.0.0.1:{port}/v1/agent/memories?limit={limit}"
        if scope:
            url += "&scope=" + urllib.parse.quote(scope, safe="")
        if query.strip():
            url += "&query=" + urllib.parse.quote(query.strip(), safe="")
        if category.strip():
            url += "&category=" + urllib.parse.quote(category.strip(), safe="")
        status, body = mcp.http_json(url, api_key=key, timeout=5)
        if status != 200 or not isinstance(body, dict):
            raise ValueError(f"cannot read agent memories from port {port} (HTTP {status})")
        body["port"] = port
        return body

    def create_agent_memory(self, body: dict) -> dict:
        port, key = self._agent_target()
        payload = {
            "key": str(body.get("key") or "").strip(),
            "value": str(body.get("value") or "").strip(),
            "scope": str(body.get("scope") or "global").strip(),
            "term": str(body.get("term") or "long").strip(),
        }
        session_id = str(body.get("session_id") or "").strip()
        if session_id:
            payload["session_id"] = session_id
        status, result = mcp.http_json(
            f"http://127.0.0.1:{port}/v1/agent/memories",
            method="POST", body=payload, api_key=key, timeout=10,
        )
        if status != 200 or not isinstance(result, dict):
            raise ValueError(f"cannot save agent memory (HTTP {status})")
        return result

    def update_agent_memory(self, body: dict) -> dict:
        memory_id = str(body.get("id") or "").strip()
        if not memory_id:
            raise ValueError("id is required")
        port, key = self._agent_target()
        encoded = urllib.parse.quote(memory_id, safe="")
        payload = {}
        for name in ("key", "value", "scope", "term", "session_id"):
            value = body.get(name)
            if value is not None:
                payload[name] = str(value).strip()
        status, result = mcp.http_json(
            f"http://127.0.0.1:{port}/v1/agent/memories/{encoded}/update",
            method="POST", body=payload, api_key=key, timeout=10,
        )
        if status != 200 or not isinstance(result, dict):
            raise ValueError(f"cannot update agent memory (HTTP {status})")
        return result

    def delete_agent_memory(self, memory_id: str) -> dict:
        memory_id = str(memory_id or "").strip()
        if not memory_id:
            raise ValueError("id is required")
        port, key = self._agent_target()
        encoded = urllib.parse.quote(memory_id, safe="")
        status, body = mcp.http_json(
            f"http://127.0.0.1:{port}/v1/agent/memories/{encoded}",
            method="DELETE", api_key=key, timeout=10,
        )
        if status not in (200, 404):
            raise ValueError(f"cannot delete that agent memory (HTTP {status})")
        return {"deleted": memory_id}

    def _running_model(self) -> str:
        status = self.tools.strata_status(hardware=False)
        running = status.get("running") or []
        model = running[0].get("model") if running else None
        return str(model or "local").strip() or "local"

    @staticmethod
    def _dream_operations(content: str) -> dict:
        text = str(content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise ValueError("the model did not return a JSON object")
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            raise ValueError("the model returned invalid JSON") from None
        if not isinstance(data, dict):
            raise ValueError("the model returned no operations")
        operations = data.get("operations")
        if operations is None and isinstance(data.get("entries"), list):
            operations = [
                {"op": "update", "id": item.get("id"), "value": item.get("value")}
                for item in data["entries"]
                if isinstance(item, dict)
            ]
        if not isinstance(operations, list):
            raise ValueError("the model returned no operations")
        return {
            "summary": str(data.get("summary") or "").strip(),
            "operations": [item for item in operations if isinstance(item, dict)],
        }

    def _dream_completion(
        self, prompt: str, system_prompt: str = DREAM_SYSTEM_PROMPT
    ) -> str:
        port, key = self._agent_target()
        status, body = mcp.http_json(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            method="POST",
            body={
                "model": self._running_model(),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "max_tokens": DREAM_MAX_OUTPUT_TOKENS,
                "stream": False,
                "reasoning_effort": "none",
            },
            api_key=key,
            timeout=180,
        )
        if status != 200 or not isinstance(body, dict):
            raise ValueError(f"the model could not dream the memories (HTTP {status})")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("the model returned no answer")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ValueError("the model returned an empty answer")
        return content

    def _dream_completion_retry(
        self, prompt: str, system_prompt: str = DREAM_SYSTEM_PROMPT
    ) -> dict:
        last_error = None
        for _ in range(2):
            content = self._dream_completion(prompt, system_prompt)
            try:
                return self._dream_operations(content)
            except ValueError as error:
                last_error = error
                if "JSON" not in str(error) and "operations" not in str(error):
                    raise
        raise ValueError(str(last_error or "the model returned invalid JSON"))

    @staticmethod
    def _memory_snapshot(row: dict) -> dict:
        snapshot = {
            "key": str(row.get("key") or ""),
            "value": str(row.get("value") or ""),
            "term": str(row.get("term") or "long"),
            "scope": str(row.get("scope") or ""),
            "category": str(row.get("category") or "general"),
            "session_id": str(row.get("session_id") or ""),
        }
        if row.get("updated_at"):
            snapshot["updated_at"] = row["updated_at"]
        return snapshot

    @staticmethod
    def _is_memory_index(row: dict) -> bool:
        return (row.get("key") == MEMORY_INDEX_KEY and row.get("scope") == "global"
                and row.get("term", "long") == "long")

    @classmethod
    def _dream_changes(cls, operations: list[dict], memories: dict[str, dict]) -> list[dict]:
        changes: list[dict] = []
        updated: set[str] = set()
        deleted: set[str] = set()
        known_sessions = {
            str(row.get("session_id") or "").strip()
            for row in memories.values()
            if str(row.get("session_id") or "").strip()
        }
        touched = {
            str(item.get("id") or "").strip()
            for item in operations
            if str(item.get("op") or "").strip().lower() in ("update", "promote", "delete")
            and str(item.get("id") or "").strip()
        }
        for item in operations:
            op = str(item.get("op") or "").strip().lower()
            if not op:
                op = "update" if str(item.get("id") or "").strip() else "create"
            if op == "promote":
                op = "update"
            memory_id = str(item.get("id") or "").strip()
            reason = str(item.get("reason") or "").strip()[:400]
            if op == "update":
                current = memories.get(memory_id)
                if current is None:
                    raise ValueError(f"memory not found: {memory_id}")
                if memory_id in updated:
                    raise ValueError(f"memory is changed twice: {memory_id}")
                if memory_id in deleted:
                    raise ValueError(f"memory is updated and deleted: {memory_id}")
                key = item.get("key")
                value = item.get("value")
                term = item.get("term")
                key = (str(current.get("key") or "").strip()
                       if key is None else str(key).strip())
                value = (str(current.get("value") or "")
                         if value is None else str(value))
                if not key:
                    raise ValueError("a memory key cannot be empty")
                if len(key) > DREAM_MAX_KEY_CHARS:
                    raise ValueError("a memory key is too long")
                if not value.strip():
                    raise ValueError("a memory value cannot be empty")
                if len(value) > DREAM_MAX_VALUE_CHARS:
                    raise ValueError("a memory value is too long")
                term = str(term or current.get("term") or "long").strip()
                if term not in ("long", "short"):
                    raise ValueError("term must be long or short")
                category = str(
                    item.get("category") or current.get("category") or "general"
                ).strip().lower()
                if category not in MEMORY_CATEGORIES:
                    raise ValueError(
                        "category must be one of " + ", ".join(MEMORY_CATEGORIES)
                    )
                session_id = str(
                    item.get("session_id") or current.get("session_id") or ""
                ).strip()
                if term == "short":
                    if not session_id or session_id not in known_sessions:
                        raise ValueError(
                            f"a short-term memory needs an existing session: {memory_id}"
                        )
                    scope = f"session:{session_id}"
                else:
                    scope = "global"
                    session_id = ""
                before_key = str(current.get("key") or "")
                before_value = str(current.get("value") or "")
                before_term = str(current.get("term") or "long")
                before_scope = str(current.get("scope") or "")
                before_category = str(current.get("category") or "general")
                if (
                    key == before_key
                    and value == before_value
                    and term == before_term
                    and scope == before_scope
                    and category == before_category
                ):
                    updated.add(memory_id)
                    continue
                change = {
                    "op": "update",
                    "id": memory_id,
                    "key": key,
                    "value": value,
                    "term": term,
                    "scope": scope,
                    "category": category,
                    "before": before_value,
                    "before_key": before_key,
                    "before_category": before_category,
                    "expected": cls._memory_snapshot(current),
                    "reason": reason,
                }
                if session_id:
                    change["session_id"] = session_id
                changes.append(change)
                updated.add(memory_id)
            elif op == "create":
                key = str(item.get("key") or "").strip()
                value = item.get("value")
                if not key:
                    raise ValueError("a new memory needs a key")
                if len(key) > DREAM_MAX_KEY_CHARS:
                    raise ValueError("a memory key is too long")
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("a memory value cannot be empty")
                if len(value) > DREAM_MAX_VALUE_CHARS:
                    raise ValueError("a memory value is too long")
                term = str(item.get("term") or "long").strip()
                if term not in ("long", "short"):
                    raise ValueError("term must be long or short")
                category = str(item.get("category") or "general").strip().lower()
                if category not in MEMORY_CATEGORIES:
                    raise ValueError(
                        "category must be one of " + ", ".join(MEMORY_CATEGORIES)
                    )
                session_id = str(item.get("session_id") or "").strip()
                if term == "short" and (
                    not session_id or session_id not in known_sessions
                ):
                    raise ValueError("a new short-term memory needs an existing session")
                scope = "global" if term == "long" else f"session:{session_id}"
                duplicate = any(
                    row_id not in touched
                    and str(row.get("key") or "") == key
                    and str(row.get("value") or "") == value.strip()
                    and str(row.get("term") or "long") == term
                    and str(row.get("scope") or "") == scope
                    and str(row.get("category") or "general") == category
                    for row_id, row in memories.items()
                )
                if duplicate:
                    continue
                change = {
                    "op": "create",
                    "key": key,
                    "value": value.strip(),
                    "term": term,
                    "scope": scope,
                    "category": category,
                    "reason": reason,
                }
                if term == "short":
                    change["session_id"] = session_id
                changes.append(change)
            elif op == "delete":
                current = memories.get(memory_id)
                if current is None:
                    raise ValueError(f"memory not found: {memory_id}")
                if memory_id in updated:
                    raise ValueError(f"memory is updated and deleted: {memory_id}")
                if memory_id in deleted:
                    continue
                changes.append(
                    {
                        "op": "delete",
                        "id": memory_id,
                        "key": str(current.get("key") or ""),
                        "value": str(current.get("value") or ""),
                        "before": str(current.get("value") or ""),
                        "expected": cls._memory_snapshot(current),
                        "term": str(current.get("term") or "long"),
                        "scope": str(current.get("scope") or ""),
                        "category": str(current.get("category") or "general"),
                        "reason": reason,
                    }
                )
                deleted.add(memory_id)
            else:
                raise ValueError(f"unknown memory operation: {op}")
        if len(changes) > DREAM_MAX_CHANGES:
            raise ValueError(f"too many changes (the limit is {DREAM_MAX_CHANGES})")
        return changes

    def dream_agent_memory(self) -> dict:
        memories = self.agent_memories().get("memories") or []
        counts = {"create": 0, "update": 0, "delete": 0}
        if not memories:
            return {
                "summary": "No memories to dream.",
                "scanned": 0,
                "changes": [],
                "counts": counts,
            }
        if len(memories) > DREAM_MAX_ITEMS:
            raise ValueError(
                f"too many memories to dream at once ({len(memories)}; "
                f"the limit is {DREAM_MAX_ITEMS})"
            )
        known: dict[str, dict] = {}
        entries: list[dict] = []
        for item in memories:
            memory_id = str(item.get("id") or "").strip()
            if not memory_id:
                continue
            row = {
                "id": memory_id,
                **self._memory_snapshot(item),
                "created_at": str(item.get("created_at") or ""),
                "last_used_at": str(item.get("last_used_at") or ""),
            }
            known[memory_id] = row
            entries.append(row)
        payload = json.dumps(entries, ensure_ascii=False)
        if len(payload) > DREAM_MAX_CHARS:
            raise ValueError(
                "the selected memories are too large to dream at once "
                f"({len(payload)} characters; the limit is {DREAM_MAX_CHARS})"
            )
        data = self._dream_completion_retry(
            "Review and consolidate the memories below. Promote durable session "
            "memory, merge duplicates, split mixed entries, and rewrite long-term "
            "entries for clarity. Return JSON only with the operations that change "
            "something.\n\n" + payload
        )
        changes = self._dream_changes(data["operations"], known)
        counts = {
            name: sum(1 for change in changes if change["op"] == name)
            for name in ("create", "update", "delete")
        }
        promoted = sum(
            1
            for change in changes
            if change["op"] == "update"
            and str(known.get(change["id"], {}).get("term") or "long") != "long"
            and change["term"] == "long"
        )
        parts = []
        if counts["update"]:
            parts.append(f"{counts['update']} updated")
        if counts["create"]:
            parts.append(f"{counts['create']} new")
        if counts["delete"]:
            parts.append(f"{counts['delete']} removed")
        detail = ", ".join(parts)
        summary = data["summary"] or (
            f"Dreaming proposed {len(changes)} changes across {len(entries)} "
            "memories"
            + (f" ({detail})" if detail else "")
            + ". Nothing is saved until you apply them."
        )
        return {
            "summary": summary,
            "scanned": len(entries),
            "changes": changes,
            "counts": counts,
            "promoted": promoted,
            "request_id": secrets.token_hex(16),
        }

    def classify_agent_memory(self) -> dict:
        memories = self.agent_memories().get("memories") or []
        counts = {"create": 0, "update": 0, "delete": 0}
        if not memories:
            return {
                "summary": "No memories to classify.",
                "scanned": 0,
                "changes": [],
                "counts": counts,
            }
        if len(memories) > DREAM_MAX_ITEMS:
            raise ValueError(
                f"too many memories to classify at once ({len(memories)}; "
                f"the limit is {DREAM_MAX_ITEMS})"
            )
        known: dict[str, dict] = {}
        entries: list[dict] = []
        for item in memories:
            memory_id = str(item.get("id") or "").strip()
            if not memory_id:
                continue
            row = {"id": memory_id, **self._memory_snapshot(item)}
            known[memory_id] = row
            entries.append({**row, "value": row["value"][:500]})
        payload = json.dumps(entries, ensure_ascii=False)
        if len(payload) > DREAM_MAX_CHARS:
            raise ValueError(
                "the selected memories are too large to classify at once "
                f"({len(payload)} characters; the limit is {DREAM_MAX_CHARS})"
            )
        data = self._dream_completion_retry(
            "Assign exactly one category to every memory below. Do not change a "
            "key or a value.\n\n" + payload,
            CLASSIFY_SYSTEM_PROMPT,
        )
        operations: list[dict] = []
        for item in data["operations"]:
            memory_id = str(item.get("id") or "").strip()
            category = str(item.get("category") or "").strip().lower()
            if memory_id in known and category in MEMORY_CATEGORIES:
                operations.append(
                    {"op": "update", "id": memory_id, "category": category}
                )
        index_id = next(
            (
                memory_id
                for memory_id, row in known.items()
                if self._is_memory_index(row)
            ),
            "",
        )
        if index_id:
            operations = [op for op in operations if op["id"] != index_id]
        projected = {op["id"]: op["category"] for op in operations}
        index_operation, _ = self._category_index(known.values(), projected)
        changes = self._dream_changes(operations + [index_operation], known)
        counts = {
            name: sum(1 for change in changes if change["op"] == name)
            for name in ("create", "update", "delete")
        }
        return {
            "summary": (
                f"Classified {len(operations)} of {len(entries)} memories and "
                f"rebuilt the category index. Nothing is saved until you apply."
            ),
            "scanned": len(entries),
            "classified": len(operations),
            "request_id": secrets.token_hex(16),
            "changes": changes,
            "counts": counts,
        }

    def merge_agent_memory(self) -> dict:
        memories = self.agent_memories().get("memories") or []
        counts = {"create": 0, "update": 0, "delete": 0}
        if not memories:
            return {
                "summary": "No memories to merge.",
                "scanned": 0,
                "changes": [],
                "counts": counts,
            }
        known: dict[str, dict] = {}
        entries: list[dict] = []
        for item in memories:
            memory_id = str(item.get("id") or "").strip()
            if not memory_id:
                continue
            if (
                str(item.get("term") or "long") == "short"
                or str(item.get("scope") or "") != "global"
            ):
                continue
            row = {"id": memory_id, **self._memory_snapshot(item)}
            known[memory_id] = row
            entries.append(row)
        if len(entries) < 2:
            return {
                "summary": "Nothing to merge.",
                "scanned": len(entries),
                "changes": [],
                "counts": counts,
            }
        def group_key(row: dict) -> tuple[str, str]:
            key = str(row.get("key") or "")
            base = re.sub(r"(?i)[\s_-]*part[\s_-]*\d+\s*$", "", key)
            base = re.sub(r"\s+-\s+.*$", "", base)
            return (str(row.get("category") or "general"), base.lower()[:80])

        entries.sort(key=group_key)
        batches: list[list[dict]] = []
        batch: list[dict] = []
        batch_chars = 0
        for row in entries:
            size = len(row["key"]) + len(row["value"])
            if batch and (len(batch) >= 40 or batch_chars + size > 12_000):
                batches.append(batch)
                batch = []
                batch_chars = 0
            batch.append(row)
            batch_chars += size
        if batch:
            batches.append(batch)
        operations: list[dict] = []
        merged_groups = 0
        for batch in batches:
            batch_operations, batch_groups = self._merge_batch(batch, known)
            operations.extend(batch_operations)
            merged_groups += batch_groups
        if not operations:
            return {
                "summary": "No related entries to merge.",
                "scanned": len(entries),
                "changes": [],
                "counts": counts,
                "merged": 0,
            }
        changes = self._dream_changes(operations, known)
        updated = {change["id"] for change in changes if change["op"] == "update"}
        # A merge also depends on survivors that already have the desired content.
        guards = [
            {"id": op["id"], "expected": self._memory_snapshot(known[op["id"]])}
            for op in operations if op["op"] == "update" and op["id"] not in updated
        ]
        counts = {
            name: sum(1 for change in changes if change["op"] == name)
            for name in ("create", "update", "delete")
        }
        return {
            "summary": (
                f"Merged {merged_groups} groups: {merged_groups} kept "
                f"and {counts['delete']} old entries deleted. Nothing is saved "
                "until you apply."
            ),
            "scanned": len(entries),
            "merged": merged_groups,
            "guards": guards,
            "request_id": secrets.token_hex(16),
            "changes": changes,
            "counts": counts,
        }

    def _merge_batch(
        self, batch: list[dict], known: dict[str, dict]
    ) -> tuple[list[dict], int]:
        batch_ids = {row["id"] for row in batch}
        try:
            payload = json.dumps(batch, ensure_ascii=False)
            data = self._dream_completion_retry(
                "Find groups of long-term memories that describe the same topic "
                "and merge each group into one short entry. The first id in a "
                "group is kept; the others will be deleted.\n\n" + payload,
                MERGE_SYSTEM_PROMPT,
            )
            operations: list[dict] = []
            used: set[str] = set()
            groups = 0
            for item in data["operations"]:
                if str(item.get("op") or "").strip().lower() != "merge":
                    continue
                ids = item.get("ids")
                if not isinstance(ids, list):
                    continue
                ids = [
                    str(memory_id).strip()
                    for memory_id in ids
                    if str(memory_id).strip()
                ]
                ids = list(dict.fromkeys(ids))
                if len(ids) < 2 or any(
                    memory_id not in batch_ids for memory_id in ids
                ):
                    continue
                if any(memory_id in used for memory_id in ids):
                    continue
                survivor = known[ids[0]]
                key = str(item.get("key") or survivor.get("key") or "").strip()
                value = item.get("value")
                category = str(
                    item.get("category")
                    or survivor.get("category")
                    or "general"
                ).strip().lower()
                if not key or len(key) > DREAM_MAX_KEY_CHARS:
                    continue
                if not isinstance(value, str) or not value.strip():
                    continue
                if len(value) > MERGE_MAX_VALUE_CHARS:
                    continue
                if category not in MEMORY_CATEGORIES:
                    continue
                merged_fact = (key, value.strip(), category)
                facts = [
                    (known[mid]["key"], known[mid]["value"], known[mid]["category"])
                    for mid in ids
                ]
                if merged_fact == facts[0] and any(fact != merged_fact for fact in facts[1:]):
                    continue
                operations.append({
                    "op": "update",
                    "id": ids[0],
                    "key": key,
                    "value": value.strip(),
                    "term": "long",
                    "scope": "global",
                    "category": category,
                    "reason": str(item.get("reason") or "").strip()[:400],
                })
                for memory_id in ids[1:]:
                    operations.append({"op": "delete", "id": memory_id})
                used.update(ids)
                groups += 1
            return operations, groups
        except ValueError:
            if len(batch) <= 1:
                raise
            middle = len(batch) // 2
            left, left_groups = self._merge_batch(batch[:middle], known)
            right, right_groups = self._merge_batch(batch[middle:], known)
            return left + right, left_groups + right_groups

    def _condense_batch(self, batch: list[dict], known: dict[str, dict]) -> list[dict]:
        batch_ids = {row["id"] for row in batch}
        try:
            payload = json.dumps(batch, ensure_ascii=False)
            data = self._dream_completion_retry(
                "Split each long memory below into short entries. Keep every "
                "fact. Do not delete the original; update it with the first "
                "part.\n\n" + payload,
                CONCISE_SYSTEM_PROMPT,
            )
            operations = []
            for item in data["operations"]:
                op = str(item.get("op") or "").strip().lower()
                if op == "delete":
                    continue
                memory_id = str(item.get("id") or "").strip()
                if op == "update" and memory_id not in batch_ids:
                    continue
                operations.append(item)
            return self._dream_changes(operations, known)
        except ValueError:
            if len(batch) <= 1:
                raise
            middle = len(batch) // 2
            return (
                self._condense_batch(batch[:middle], known)
                + self._condense_batch(batch[middle:], known)
            )

    def concise_agent_memory(self) -> dict:
        memories = self.agent_memories().get("memories") or []
        counts = {"create": 0, "update": 0, "delete": 0}
        if not memories:
            return {
                "summary": "No memories to condense.",
                "scanned": 0,
                "changes": [],
                "counts": counts,
            }
        known: dict[str, dict] = {}
        long_entries: list[dict] = []
        for item in memories:
            memory_id = str(item.get("id") or "").strip()
            if not memory_id:
                continue
            row = {"id": memory_id, **self._memory_snapshot(item)}
            known[memory_id] = row
            if (
                row["term"] != "short"
                and row["scope"] == "global"
                and len(row["value"]) > 400
            ):
                long_entries.append(row)
        if not long_entries:
            return {
                "summary": "Every long-term memory is already 400 characters or less.",
                "scanned": len(memories),
                "changes": [],
                "counts": counts,
            }
        long_entries.sort(key=lambda row: len(row["value"]), reverse=True)
        batches: list[list[dict]] = []
        batch: list[dict] = []
        batch_chars = 0
        for row in long_entries:
            size = len(row["key"]) + len(row["value"])
            if batch and (len(batch) >= 6 or batch_chars + size > 12_000):
                batches.append(batch)
                batch = []
                batch_chars = 0
            batch.append(row)
            batch_chars += size
        if batch:
            batches.append(batch)
        changes: list[dict] = []
        for batch in batches:
            changes.extend(self._condense_batch(batch, known))
        if len(changes) > DREAM_MAX_CHANGES:
            raise ValueError(f"too many changes (the limit is {DREAM_MAX_CHANGES})")
        counts = {
            name: sum(1 for change in changes if change["op"] == name)
            for name in ("create", "update", "delete")
        }
        return {
            "summary": (
                f"Condensed {len(long_entries)} long memories into "
                f"{len(changes)} changes. Nothing is saved until you apply."
            ),
            "scanned": len(long_entries),
            "request_id": secrets.token_hex(16),
            "changes": changes,
            "counts": counts,
        }

    def _category_index(self, memories, projected: dict) -> tuple[dict, dict]:
        long_term = [row for row in memories
                     if row.get("scope") == "global" and row.get("term", "long") == "long"]
        index = next((row for row in long_term if self._is_memory_index(row)), None)
        counts = {name: 0 for name in MEMORY_CATEGORIES}
        for row in long_term:
            category = projected.get(row["id"], row.get("category") or "general")
            if row is index:
                category = "reference"
            counts[category] = counts.get(category, 0) + 1
        if index is None:
            counts["reference"] += 1
        total = sum(counts.values())
        today = time.strftime("%Y-%m-%d")
        summary = "；".join(
            f"{MEMORY_CATEGORY_TITLES[name]} {counts[name]}"
            for name in MEMORY_CATEGORIES if counts[name]
        )
        legend = "；".join(
            f"{name}={MEMORY_CATEGORY_TITLES[name]}" for name in MEMORY_CATEGORIES
        )
        change = {
            "op": "create" if index is None else "update",
            "key": MEMORY_INDEX_KEY,
            "value": (f"记忆库分类索引（{today}）：长期记忆 {total} 条。{summary}。"
                      f"分类：{legend}。按分类筛选或搜索 key。"),
            "term": "long",
            "scope": "global",
            "category": "reference",
            "reason": "Rebuild the memory category index.",
        }
        if index is not None:
            change["id"] = index["id"]
        return change, {"total": total, "counts": counts}

    def refresh_memory_index(self) -> dict:
        # The management list is paged; the index counts every global memory.
        memories = self.agent_memories(scope="global", limit=-1).get("memories") or []
        known = {row["id"]: row for row in memories}
        operation, result = self._category_index(memories, {})
        changes = self._dream_changes([operation], known)
        if changes:
            self._apply_memory_batch(secrets.token_hex(16), changes)
        return result

    def _apply_memory_batch(self, request_id: str, changes: list, guards: list | None = None) -> dict:
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("generate a new memory preview before applying")
        operations = []
        fields = ("key", "value", "category", "term", "scope", "session_id")
        for change in changes:
            if not isinstance(change, dict):
                raise ValueError("each change must be an object")
            op = change.get("op")
            if op not in ("create", "update", "delete"):
                raise ValueError(f"unknown memory operation: {op}")
            operation = {"op": op}
            if op != "create":
                expected = change.get("expected")
                if not isinstance(expected, dict) or not all(k in expected for k in fields):
                    raise ValueError("generate a new memory preview before applying")
                operation.update(id=change.get("id"), expected=expected)
            if op != "delete":
                operation.update({key: change[key] for key in fields if key in change})
            if op == "create" and self._is_memory_index(operation):
                operation["if_absent"] = True
            operations.append(operation)
        payload = {"request_id": request_id, "changes": operations}
        if guards:
            payload["guards"] = guards
        port, key = self._agent_target()
        status, result = mcp.http_json(
            f"http://127.0.0.1:{port}/v1/agent/memories/batch",
            method="POST", body=payload, api_key=key, timeout=60,
        )
        if status == 409:
            raise ValueError("memory changed since preview; generate a new preview")
        if status in (404, 405):
            raise ValueError("update the agent sidecar to apply memory previews")
        if status != 200 or not isinstance(result, dict):
            raise ValueError(f"cannot apply the memory batch (HTTP {status})")
        return result

    def apply_agent_memory_dream(self, body: dict) -> dict:
        changes = body.get("changes")
        if not isinstance(changes, list):
            raise ValueError("changes must be a list")
        if len(changes) > DREAM_MAX_CHANGES:
            raise ValueError(f"too many changes (the limit is {DREAM_MAX_CHANGES})")
        result = self._apply_memory_batch(body.get("request_id"), changes, body.get("guards"))
        if body.get("refresh_index"):
            try:
                result["index"] = self.refresh_memory_index()
            except ValueError as error:
                raise ValueError(
                    f"memory changes were applied; retry this preview to refresh the index ({error})"
                ) from error
        return result

    def rename_agent_session(self, session_id: str, title: str) -> dict:
        session_id = str(session_id or "").strip()
        title = str(title or "").strip()
        if not session_id:
            raise ValueError("id is required")
        if not title:
            raise ValueError("title is required")
        port, key = self._agent_target()
        encoded = urllib.parse.quote(session_id, safe="")
        status, body = mcp.http_json(
            f"http://127.0.0.1:{port}/v1/agent/sessions/{encoded}/rename",
            method="POST", body={"title": title[:200]}, api_key=key, timeout=10,
        )
        if status != 200 or not isinstance(body, dict):
            raise ValueError(f"cannot rename that agent session (HTTP {status})")
        return body

    def delete_agent_session(self, session_id: str) -> dict:
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("id is required")
        port, key = self._agent_target()
        encoded = urllib.parse.quote(session_id, safe="")
        status, body = mcp.http_json(
            f"http://127.0.0.1:{port}/v1/agent/sessions/{encoded}",
            method="DELETE", api_key=key, timeout=10,
        )
        if status not in (200, 404):
            raise ValueError(f"cannot delete that agent session (HTTP {status})")
        return {"deleted": session_id}

    def agent_attachment(self, path: str) -> tuple[str, bytes]:
        value = str(path or "").strip()
        if not value:
            raise ValueError("path is required")
        port, key = self._agent_target()
        url = (f"http://127.0.0.1:{port}/v1/agent/attachments?path="
               + urllib.parse.quote(value, safe=""))
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=20) as response:
                data = response.read(32 * 1024 * 1024 + 1)
                if len(data) > 32 * 1024 * 1024:
                    raise ValueError("attachment is larger than 32 MB")
                return response.headers.get_content_type(), data
        except urllib.error.HTTPError as error:
            raise ValueError(f"cannot read attachment (HTTP {error.code})") from None
        except (OSError, ValueError) as error:
            raise ValueError(f"cannot read attachment: {error}") from None

    def pick_folder(self, initial: str = "") -> dict:
        """Open the host's native folder picker (Windows). The page runs locally, so this is the same dialog Explorer uses."""
        if os.name != "nt":
            raise ValueError("the native folder picker is available on Windows; type the folder path")
        if not _FOLDER_LOCK.acquire(blocking=False):
            raise ValueError("a folder picker is already open")
        try:
            powershell = shutil.which("powershell.exe") or shutil.which("powershell")
            if not powershell:
                bundled = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
                if bundled.is_file():
                    powershell = str(bundled)
            if not powershell:
                raise ValueError("PowerShell was not found; type the folder path")
            script = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$owner = New-Object System.Windows.Forms.Form
$owner.Text = 'Lum'
$owner.ShowInTaskbar = $false
$owner.TopMost = $true
$owner.WindowState = 'Minimized'
$owner.Show()
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择模型可访问的本地文件夹'
$dialog.ShowNewFolderButton = $true
if ($env:LUM_FOLDER_INITIAL) { $dialog.SelectedPath = $env:LUM_FOLDER_INITIAL }
$result = $dialog.ShowDialog($owner)
$owner.Close()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($dialog.SelectedPath) }
"""
            env = dict(os.environ, LUM_FOLDER_INITIAL=initial or "")
            proc = subprocess.run(
                [powershell, "-NoProfile", "-STA", "-Command", script],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "the folder picker failed").strip()
                raise ValueError(detail[:300])
            return {"path": (proc.stdout or "").strip()}
        finally:
            _FOLDER_LOCK.release()

    def browse(self, path: str = "") -> dict:
        path = str(path or "").strip()
        if not path:
            roots = []
            for letter in string.ascii_uppercase:
                root = f"{letter}:\\"
                if os.path.isdir(root):
                    roots.append({"label": root, "path": root})
            for share in ("Family", "web", "home", "photo", "video", "music"):
                root = rf"\\192.168.50.45\{share}"
                if os.path.isdir(root):
                    roots.append({"label": root, "path": root})
            home = str(Path.home())
            if home not in {item["path"] for item in roots}:
                roots.append({"label": f"Home ({home})", "path": home})
            return {"path": "", "parent": None, "roots": roots, "dirs": []}

        normalized = path.rstrip("\\/") or path
        if not os.path.isdir(normalized):
            raise ValueError(f"not a folder: {path}")
        dirs = []
        try:
            with os.scandir(normalized) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir():
                            dirs.append({"name": entry.name, "path": entry.path})
                    except OSError:
                        continue
        except OSError as error:
            raise ValueError(f"cannot read folder: {error}") from None
        dirs.sort(key=lambda item: item["name"].lower())
        parent = os.path.dirname(normalized) or None
        if parent and parent == normalized:
            parent = None
        if normalized.startswith("\\\\") and len(
            [part for part in normalized.strip("\\").split("\\") if part]
        ) <= 2:
            parent = None
        return {"path": normalized, "parent": parent, "roots": [], "dirs": dirs}

    def _read_store(self, name: str, default):
        path = self.store_dir / name
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write_store(self, name: str, value) -> None:
        path = self.store_dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def sessions(self) -> dict:
        try:
            live = self.tasks()
        except ValueError as e:
            live = {"tasks": [], "finished": [], "summary": str(e)}
        saved = self._read_store("sessions.json", [])
        return {"live": live, "saved": saved if isinstance(saved, list) else []}

    def save_session(self, body: dict) -> dict:
        session_id = str(body.get("id") or "").strip()
        if not session_id:
            raise ValueError("id is required")
        data = self.tasks()
        requests = [t for t in (data.get("tasks", []) + data.get("finished", []))
                    if t.get("session") == session_id]
        if not requests:
            raise ValueError("no requests in that session to save")
        saved = self._read_store("sessions.json", [])
        if not isinstance(saved, list):
            saved = []
        entry = {"id": session_id, "saved_at": time.time(),
                 "model": requests[-1].get("model"), "requests": requests}
        saved = [s for s in saved if s.get("id") != session_id]
        saved.insert(0, entry)
        self._write_store("sessions.json", saved[:100])
        return {"saved": entry, "sessions": saved[:100]}

    def delete_session(self, body: dict) -> dict:
        session_id = str(body.get("id") or "").strip()
        if not session_id:
            raise ValueError("id is required")
        saved = self._read_store("sessions.json", [])
        saved = [s for s in saved if isinstance(s, dict) and s.get("id") != session_id]
        self._write_store("sessions.json", saved)
        return {"sessions": saved}

    def memory(self) -> dict:
        items = self._read_store("memory.json", [])
        return {"items": items if isinstance(items, list) else []}

    def save_memory(self, body: dict) -> dict:
        title = str(body.get("title") or "").strip()
        content = str(body.get("content") or "").strip()
        if not title and not content:
            raise ValueError("title or content is required")
        tags = body.get("tags")
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        if not isinstance(tags, list):
            tags = []
        items = self._read_store("memory.json", [])
        if not isinstance(items, list):
            items = []
        entry_id = str(body.get("id") or "").strip()
        now = time.time()
        if entry_id:
            entry = next((x for x in items if x.get("id") == entry_id), None)
            if entry is None:
                raise ValueError("no memory with that id")
            entry.update({"title": title, "content": content, "tags": tags, "updated": now})
        else:
            entry = {"id": secrets.token_hex(6), "title": title, "content": content,
                     "tags": tags, "created": now, "updated": now}
            items.insert(0, entry)
        self._write_store("memory.json", items[:200])
        return {"item": entry, "items": items[:200]}

    def delete_memory(self, body: dict) -> dict:
        entry_id = str(body.get("id") or "").strip()
        if not entry_id:
            raise ValueError("id is required")
        items = self._read_store("memory.json", [])
        items = [x for x in items if isinstance(x, dict) and x.get("id") != entry_id]
        self._write_store("memory.json", items)
        return {"items": items}


class Handler(BaseHTTPRequestHandler):
    server_version = "StrataConfig/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[strata-config] " + (fmt % args) + "\n")

    def _app(self) -> App:
        return self.server.app

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, value) -> None:
        self._send(code, json.dumps(value, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, code: int, message) -> None:
        self._json(code, {"error": {"message": str(message)}})

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        return host in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _token_ok(self) -> bool:
        given = self.headers.get("X-Strata-Token") or ""
        return hmac.compare_digest(given, self._app().token)

    def _origin_ok(self) -> bool:
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if not origin:
            return True
        port = self.server.server_address[1]
        return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}",
                          f"http://[::1]:{port}"}

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw or b"{}")
        except ValueError:
            raise ValueError("the request body is not JSON") from None
        if not isinstance(value, dict):
            raise ValueError("the request body must be a JSON object")
        return value

    def do_GET(self):
        if not self._host_ok():
            self._error(403, "wrong Host header")
            return
        u = urllib.parse.urlsplit(self.path)
        path = u.path.rstrip("/") or "/"
        if path == "/":
            self._send(200, self.server.html, "text/html; charset=utf-8")
            return
        if path == "/healthz":
            self._json(200, {"ok": True})
            return
        if path == "/tdesign.css":
            self._send(200, (TOOLS / "tdesign.css").read_bytes(), "text/css; charset=utf-8")
            return
        if not self._token_ok():
            self._error(401, "open this page from the address it printed")
            return
        query = urllib.parse.parse_qs(u.query)
        try:
            if path == "/api/agent/attachment":
                ctype, data = self._app().agent_attachment(query.get("path", [""])[0])
                self._send(200, data, ctype)
                return
            if path == "/api/state":
                self._json(200, self._app().state(fresh=query.get("fresh", ["0"])[0] == "1"))
            elif path == "/api/models":
                self._json(200, self._app().tools.strata_models())
            elif path == "/api/config":
                self._json(200, self._app().config(query.get("model", [""])[0]))
            elif path == "/api/key":
                index = query.get("index", ["-1"])[0]
                self._json(200, self._app().key(query.get("model", [""])[0], int(index)))
            elif path == "/api/modelconfig":
                self._json(200, self._app().model_config(query.get("model", [""])[0]))
            elif path == "/api/logs":
                self._json(200, self._app().logs({k: v[0] for k, v in query.items()}))
            elif path == "/api/console":
                self._json(200, self._app().console({k: v[0] for k, v in query.items()}))
            elif path == "/api/connect":
                port = query.get("port", [None])[0]
                self._json(200, self._app().connect(int(port) if port else None))
            elif path == "/api/tasks":
                self._json(200, self._app().tasks())
            elif path == "/api/plugins":
                self._json(200, self._app().plugins())
            elif path == "/api/schedules":
                self._json(200, self._app().schedules())
            elif path == "/api/agent/sessions":
                self._json(200, self._app().agent_sessions())
            elif path == "/api/agent/session":
                self._json(200, self._app().agent_session(query.get("id", [""])[0]))
            elif path == "/api/agent/memory":
                self._json(200, self._app().agent_memories(query.get("query", [""])[0]))
            elif path == "/api/browse":
                self._json(200, self._app().browse(query.get("path", [""])[0]))
            elif path == "/api/pick-folder":
                self._json(200, self._app().pick_folder(query.get("initial", [""])[0]))
            elif path == "/api/sessions":
                self._json(200, self._app().sessions())
            elif path == "/api/memory":
                self._json(200, self._app().memory())
            else:
                self._error(404, "not found")
        except (mcp.ToolError, ValueError) as e:
            self._error(400, e)
        except Exception as e:                                  # noqa: BLE001
            self._error(500, e)

    def do_POST(self):
        if not self._host_ok():
            self._error(403, "wrong Host header")
            return
        if not self._token_ok() or not self._origin_ok():
            self._error(403, "this request did not come from the settings page")
            return
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._error(415, "send application/json")
            return
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        try:
            body = self._body()
            if path == "/api/config":
                self._json(200, self._app().save_config(body))
            elif path == "/api/modelconfig":
                self._json(200, self._app().save_model_config(body))
            elif path == "/api/install/plan":
                self._json(200, self._app().plan(body))
            elif path == "/api/install":
                self._json(200, self._app().install(body))
            elif path == "/api/install/cancel":
                self._json(200, self._app().cancel_install())
            elif path == "/api/start":
                self._json(200, self._app().start(body))
            elif path == "/api/stop":
                self._json(200, self._app().stop(body))
            elif path == "/api/headless":
                self._json(200, self._app().headless(body))
            elif path == "/api/tasks/stop":
                self._json(200, self._app().stop_task(body))
            elif path == "/api/tasks/clear":
                self._json(200, self._app().clear_tasks())
            elif path == "/api/plugins":
                self._json(200, self._app().save_plugins(body))
            elif path == "/api/schedules":
                self._json(200, self._app().create_schedule(body))
            elif path == "/api/schedules/delete":
                self._json(200, self._app().delete_schedule(body.get("id", "")))
            elif path == "/api/agent/session/rename":
                self._json(200, self._app().rename_agent_session(
                    body.get("id", ""), body.get("title", "")
                ))
            elif path == "/api/agent/session/delete":
                self._json(200, self._app().delete_agent_session(body.get("id", "")))
            elif path == "/api/agent/memory":
                self._json(200, self._app().create_agent_memory(body))
            elif path == "/api/agent/memory/update":
                self._json(200, self._app().update_agent_memory(body))
            elif path == "/api/agent/memory/delete":
                self._json(200, self._app().delete_agent_memory(body.get("id", "")))
            elif path == "/api/agent/memory/dream":
                self._json(200, self._app().dream_agent_memory())
            elif path == "/api/agent/memory/merge":
                self._json(200, self._app().merge_agent_memory())
            elif path == "/api/agent/memory/classify":
                self._json(200, self._app().classify_agent_memory())
            elif path == "/api/agent/memory/condense":
                self._json(200, self._app().concise_agent_memory())
            elif path == "/api/agent/memory/dream/apply":
                self._json(200, self._app().apply_agent_memory_dream(body))
            elif path == "/api/sessions/save":
                self._json(200, self._app().save_session(body))
            elif path == "/api/sessions/delete":
                self._json(200, self._app().delete_session(body))
            elif path == "/api/memory":
                self._json(200, self._app().save_memory(body))
            elif path == "/api/memory/delete":
                self._json(200, self._app().delete_memory(body))
            else:
                self._error(404, "not found")
        except (mcp.ToolError, ValueError) as e:
            self._error(400, e)
        except Exception as e:                                  # noqa: BLE001
            self._error(500, e)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Strata's local settings page (models, network, start/stop).")
    ap.add_argument("--root", default=str(ROOT), help="the Strata folder (default: the one this file is in)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"the page's port (default {DEFAULT_PORT})")
    ap.add_argument("--no-browser", action="store_true", help="do not open the page in the browser")
    a = ap.parse_args(argv)
    root = Path(a.root).expanduser().resolve()
    if not (root / "setup.py").is_file() or not (root / "serve" / "server.py").is_file():
        print(f"[strata-config] {root} is not a Strata folder", file=sys.stderr)
        return 2
    if not 1 <= a.port <= 65535:
        print("[strata-config] the port must be 1..65535", file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{a.port}/"
    try:
        with urllib.request.urlopen(url + "healthz", timeout=1.5) as r:
            if r.status == 200:
                print(f"[strata-config] already running: {url}", flush=True)
                if not a.no_browser:
                    webbrowser.open(url)
                return 0
    except OSError:
        pass
    app = App(root, a.port)
    template = (TOOLS / "config_ui.html").read_text(encoding="utf-8")
    html = template.replace("__CONFIG_TOKEN__", app.token).encode("utf-8")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        print(f"[strata-config] cannot listen on 127.0.0.1:{a.port} ({e}); use --port another number",
              file=sys.stderr)
        return 1
    server.app = app
    server.html = html
    print(f"[strata-config] {url}", flush=True)
    if not a.no_browser:
        threading.Timer(0.4, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
