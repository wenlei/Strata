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
import secrets
import shutil
import string
import socket
import subprocess
import sys
import threading
import time
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
            self._state = {"status": status, "models": models, "installed": installed, "lan": lan}
            self._state_at = time.time()
            return self._state

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
        if not self._token_ok():
            self._error(401, "open this page from the address it printed")
            return
        query = urllib.parse.parse_qs(u.query)
        try:
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
            elif path == "/api/connect":
                port = query.get("port", [None])[0]
                self._json(200, self._app().connect(int(port) if port else None))
            elif path == "/api/tasks":
                self._json(200, self._app().tasks())
            elif path == "/api/plugins":
                self._json(200, self._app().plugins())
            elif path == "/api/agent/sessions":
                self._json(200, self._app().agent_sessions())
            elif path == "/api/agent/session":
                self._json(200, self._app().agent_session(query.get("id", [""])[0]))
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
            elif path == "/api/tasks/stop":
                self._json(200, self._app().stop_task(body))
            elif path == "/api/tasks/clear":
                self._json(200, self._app().clear_tasks())
            elif path == "/api/plugins":
                self._json(200, self._app().save_plugins(body))
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
