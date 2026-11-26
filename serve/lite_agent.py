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
import re
import socket
import subprocess
import sys
import tempfile
import threading
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
_owner_lock = threading.Lock()
_owner: threading.Thread | None = None
_halt = threading.Event()
_first_attempt = threading.Event()
_retry_delay = 2.0


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
    """The listener owns at most one supervisor. External sidecars keep their own lifecycle."""
    global _owner
    if os.environ.get("STRATA_AGENT_DISABLE", "").strip() in ("1", "true", "yes"):
        return None
    with _owner_lock:
        if _owner is None or not _owner.is_alive():
            _halt.clear()
            _first_attempt.clear()
            _owner = threading.Thread(target=_supervise, args=(svc, host, port),
                                      name="lite-agent-supervisor", daemon=True)
            _owner.start()
    _first_attempt.wait()
    return _active


def _supervise(svc: Any, host: str, port: int) -> None:
    global _active
    delay = _retry_delay
    try:
        while not _halt.is_set():
            if _external_port(port) is not None:
                # An independently managed process already owns this backend. Its supervisor
                # alone decides when to restart it; the proxy continues discovering its port.
                return
            if _halt.is_set():
                break
            try:
                _active = _launch(svc, host, port)
            except OSError as error:
                print(f"[strata] lite agent launch failed ({type(error).__name__})", flush=True)
            _first_attempt.set()
            if _active is not None:
                started = time.monotonic()
                while _active.process.poll() is None and not _halt.wait(0.25):
                    pass
                if _halt.is_set():
                    break
                uptime = time.monotonic() - started
                print(f"[strata] lite agent pid {_active.process.pid} exited ({_active.process.returncode}) "
                      f"after {uptime:.1f}s", flush=True)
                if uptime >= 60:
                    delay = _retry_delay
                _forget_external(_active.port)
                _close_sidecar(_active)
                _active = None
            if not _halt.is_set():
                print(f"[strata] lite agent retry in {delay:g}s", flush=True)
                _halt.wait(delay)
                delay = min(delay * 2, 60)
    finally:
        _first_attempt.set()
        if _active is not None:
            _close_sidecar(_active)
            _active = None


def _launch(svc: Any, host: str, port: int) -> Sidecar | None:
    python = _agent_python()
    if python is None:
        print("[strata] lite agent: install the agent package or set STRATA_AGENT_PYTHON; "
              "/v1/agent/* stays 502", flush=True)
        return None

    host_for_agent = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    agent_port = _free_port()
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

    sidecar = Sidecar(process, agent_port, log)
    deadline = time.monotonic() + 30
    while not _halt.is_set() and time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{agent_port}/healthz", timeout=1) as response:
                if response.status == 200:
                    print(f"[strata] lite agent pid {process.pid} on 127.0.0.1:{agent_port}; public at /v1/agent/* on port {port}",
                          flush=True)
                    return sidecar
        except Exception:  # noqa: BLE001 - starting; any failure means try again
            pass
        _halt.wait(0.2)

    _close_sidecar(sidecar)
    print(f"[strata] lite agent did not become ready; see {_log_path()}; /v1/agent/* stays 502", flush=True)
    return None


_external: tuple[float, int | None] = (0.0, None)
_discovery_lock = threading.Lock()


def _external_port(public_port: int) -> int | None:
    """A lite agent started outside this server (its own supervisor) for the same Strata port."""
    global _external
    expires, cached = _external
    if time.monotonic() < expires:
        return cached
    if cached is not None and _discovery_lock.locked():
        # Another request is already refreshing this known address.
        return cached
    with _discovery_lock:
        expires, cached = _external
        if time.monotonic() < expires:
            return cached
        port = _discover_external_port(public_port)
        # Cache a live endpoint, but let a just-started sidecar recover promptly.
        _external = (time.monotonic() + (30 if port is not None else 2), port)
        return port


def _discover_external_port(public_port: int) -> int | None:
    if os.name != "nt":
        return None
    ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not ps.is_file():
        return None
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and "
        "$_.CommandLine -like '*lite_strata_agent*serve*' } | "
        "Sort-Object CreationDate -Descending | ForEach-Object { $_.CommandLine }"
    )
    try:
        out = subprocess.run(
            [str(ps), "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if f"--strata http://127.0.0.1:{public_port}/v1" not in line:
            continue
        match = re.search(r"--port\s+(\d+)", line)
        if match:
            return int(match.group(1))
    return None


def _forget_external(port: int) -> None:
    global _external
    with _discovery_lock:
        if _external[1] == port:
            _external = (0.0, None)


def stop() -> None:
    with _owner_lock:
        if _owner is not None:
            _halt.set()
            _owner.join()


def _close_sidecar(sidecar: Sidecar) -> None:
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
    sidecar = _active
    target = None
    if sidecar is not None and sidecar.process.poll() is None:
        target = sidecar.port
    else:
        public_port = getattr(handler.server, "server_port", None) or handler.server.server_address[1]
        target = _external_port(int(public_port))
    if target is None:
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

    connection = HTTPConnection("127.0.0.1", target, timeout=600)
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
        if isinstance(error, ConnectionRefusedError):
            _forget_external(target)
        handler._json(502, {"error": {"type": "server_error",
                                      "message": f"the lite agent sidecar did not answer: {error}"}})
    finally:
        connection.close()
