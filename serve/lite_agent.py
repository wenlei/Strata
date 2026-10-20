"""serve/lite_agent.py - run the lite-strata-agent sidecar and proxy /v1/agent/* on Strata's own port.

The public service stays Strata's port (8080 by default).  The sidecar listens on an ephemeral loopback port, so
there is no second address to open, firewall or configure.  `serve/server.py`'s main() starts it after Strata is
listening and stops it with Strata.  If the sidecar cannot start, /v1/agent/* answers 502 and Strata itself keeps
working.

Environment:
  STRATA_AGENT_PYTHON       python.exe that can import lite_strata_agent (default: this python, else the sibling venv)
  STRATA_AGENT_WORKSPACE    workspace root (default: %USERPROFILE%/lumWorkspace)
  STRATA_AGENT_DATA_DIR     agent SQLite/artifact directory (default: the agent's platform default)
  STRATA_AGENT_LOG          sidecar log file (default: <data dir>/agent.log or the temp directory)
  STRATA_AGENT_APP_APK      APK served at /v1/agent/app/apk
  STRATA_AGENT_APP_VERSION, STRATA_AGENT_APP_VERSION_CODE, STRATA_AGENT_APP_CHANNEL, STRATA_AGENT_APP_NOTES
  STRATA_AGENT_MCP_CONFIG   MCP JSON config
  STRATA_AGENT_DISABLE=1    do not start the sidecar
"""
from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Sidecar:
    process: subprocess.Popen
    port: int
    log: Any


_active: Sidecar | None = None


def _agent_python() -> str | None:
    explicit = os.environ.get("STRATA_AGENT_PYTHON", "").strip()
    if explicit:
        return explicit
    try:
        if importlib.util.find_spec("lite_strata_agent") is not None:
            return sys.executable
    except (ImportError, ValueError):
        pass
    candidates = [
        ROOT.parent / "lite-strata-agent" / "pc-agent" / ".venv" / "Scripts" / "python.exe",
        ROOT.parent / "lite-strata-agent" / "pc-agent" / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _keys(svc: Any) -> list[str]:
    value = getattr(svc, "api_key", "")
    if isinstance(value, list):
        return [str(key).strip() for key in value if str(key).strip()]
    return [key.strip() for key in str(value or "").split(",") if key.strip()]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _log_path() -> Path:
    explicit = os.environ.get("STRATA_AGENT_LOG", "").strip()
    if explicit:
        path = Path(explicit)
    else:
        data_dir = os.environ.get("STRATA_AGENT_DATA_DIR", "").strip()
        path = Path(data_dir) / "agent.log" if data_dir else Path(tempfile.gettempdir()) / "strata-agent.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def start(svc: Any, host: str, port: int) -> Sidecar | None:
    """Start the sidecar after Strata is listening; None when it cannot start."""
    global _active
    if os.environ.get("STRATA_AGENT_DISABLE", "").strip() in ("1", "true", "yes"):
        return None
    python = _agent_python()
    if python is None:
        print("[strata] lite agent: install the agent package or set STRATA_AGENT_PYTHON; "
              "/v1/agent/* stays 502", flush=True)
        return None

    agent_port = _free_port()
    host_for_agent = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    keys = _keys(svc)
    workspace = os.environ.get("STRATA_AGENT_WORKSPACE", "").strip() or str(Path.home() / "lumWorkspace")
    command = [
        python, "-m", "lite_strata_agent", "serve",
        "--host", "127.0.0.1", "--port", str(agent_port),
        "--strata", f"http://{host_for_agent}:{port}/v1",
        "--workspace", workspace,
    ]
    if keys:
        command += ["--api-key", ",".join(keys), "--strata-api-key", keys[0]]
    for env, flag in (
        ("STRATA_AGENT_DATA_DIR", "--data-dir"),
        ("STRATA_AGENT_APP_APK", "--app-apk"),
        ("STRATA_AGENT_APP_VERSION", "--app-version"),
        ("STRATA_AGENT_APP_VERSION_CODE", "--app-version-code"),
        ("STRATA_AGENT_APP_CHANNEL", "--app-channel"),
        ("STRATA_AGENT_APP_NOTES", "--app-notes"),
        ("STRATA_AGENT_MCP_CONFIG", "--mcp-config"),
    ):
        value = os.environ.get(env, "").strip()
        if value:
            command += [flag, value]

    log = open(_log_path(), "ab", buffering=0)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    except OSError as error:
        log.close()
        print(f"[strata] lite agent could not start: {error}; /v1/agent/* stays 502", flush=True)
        return None

    deadline = time.time() + 30
    while time.time() < deadline:
        if process.poll() is not None:
            break
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{agent_port}/healthz", timeout=1) as response:
                if response.status == 200:
                    _active = Sidecar(process, agent_port, log)
                    print(f"[strata] lite agent on 127.0.0.1:{agent_port}; public at /v1/agent/* on port {port}",
                          flush=True)
                    return _active
        except Exception:  # noqa: BLE001 - starting; any failure means try again
            pass
        time.sleep(0.2)

    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
    log.close()
    print(f"[strata] lite agent did not become ready; see {_log_path()}; /v1/agent/* stays 502", flush=True)
    return None


def stop() -> None:
    global _active
    sidecar, _active = _active, None
    if sidecar is None:
        return
    if sidecar.process.poll() is None:
        sidecar.process.terminate()
        try:
            sidecar.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            sidecar.process.kill()
            sidecar.process.wait()
    try:
        sidecar.log.close()
    except OSError:
        pass


def proxy(handler: Any, method: str) -> None:
    """Forward one /v1/agent/* request to the sidecar, streaming SSE and the APK."""
    if _active is None:
        handler._json(502, {"error": {"type": "server_error",
                                      "message": "the lite agent sidecar is not running; see the Strata console"}})
        return

    body = handler._body() if method == "POST" else None
    headers = {}
    for name in ("Authorization", "x-api-key", "Accept", "Last-Event-ID", "Content-Type", "X-Lum-Filename", "Content-Disposition"):
        value = handler.headers.get(name)
        if value:
            headers[name] = value
    if "Content-Type" not in headers and body is not None:
        headers["Content-Type"] = "application/json"

    connection = HTTPConnection("127.0.0.1", _active.port, timeout=600)
    try:
        connection.request(method, handler.path, body=body, headers=headers)
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "")

        if "text/event-stream" in content_type.lower():
            handler.send_response(response.status)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("X-Accel-Buffering", "no")
            handler.send_header("Connection", "close")
            handler._cors()
            handler.close_connection = True
            handler.end_headers()
            try:
                while True:
                    chunk = response.read1(4096) if hasattr(response, "read1") else response.read(4096)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        data = response.read()
        handler.send_response(response.status)
        for name in ("Content-Type", "Content-Length", "Content-Disposition", "Cache-Control", "Retry-After"):
            value = response.getheader(name)
            if value:
                handler.send_header(name, value)
        if not response.getheader("Content-Length"):
            handler.send_header("Content-Length", str(len(data)))
        handler._cors()
        handler.end_headers()
        handler.wfile.write(data)
    except (ConnectionError, HTTPException, OSError) as error:
        handler._json(502, {"error": {"type": "server_error",
                                      "message": f"the lite agent sidecar did not answer: {error}"}})
    finally:
        connection.close()
