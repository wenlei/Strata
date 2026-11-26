"""Recovery uses only a temporary listener, log and disposable HTTP child processes."""
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from serve import lite_agent


CHILD = """
import os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'OK')
    def do_POST(self):
        with open(sys.argv[2], 'ab') as out:
            out.write(b'1')
        os._exit(31)
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""


def handler(port):
    return Mock(server=SimpleNamespace(server_port=port), path="/v1/agent/health",
                headers={}, wfile=io.BytesIO(), _body=Mock(return_value=b"{}"))


class LiteAgentRecovery(unittest.TestCase):
    def test_owner_recovers_an_exited_child_without_replaying_the_write(self):
        real_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            hits = Path(tmp) / "hits"
            processes = []

            def popen(argv, **kwargs):
                port = argv[argv.index("--port") + 1]
                child = real_popen([sys.executable, "-u", "-c", CHILD, port, str(hits)], **kwargs)
                processes.append(child)
                return child

            stack.enter_context(patch.object(lite_agent, "_external_port", return_value=None))
            stack.enter_context(patch.object(lite_agent, "_agent_python", return_value=sys.executable))
            stack.enter_context(patch.object(lite_agent, "_retry_delay", .05))
            stack.enter_context(patch.object(lite_agent.subprocess, "Popen", side_effect=popen))
            stack.enter_context(patch.dict(os.environ, {"STRATA_AGENT_DISABLE": "", "STRATA_AGENT_LOG": str(Path(tmp)/"test.log")}))
            stack.callback(lite_agent.stop)
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: lite_agent.start(SimpleNamespace(), "127.0.0.1", 19731), range(6)))
            self.assertEqual(len(processes), 1)
            self.assertTrue(all(result is results[0] for result in results))
            failed = handler(19731)
            lite_agent.proxy(failed, "POST")
            self.assertEqual(failed._json.call_args.args[0], 502)
            self.assertEqual(processes[0].wait(3), 31)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if lite_agent._active and lite_agent._active is not results[0]:
                    break
                time.sleep(.02)
            recovered = handler(19731)
            lite_agent.proxy(recovered, "GET")
            recovered.send_response.assert_called_once_with(200)
            self.assertEqual(recovered.wfile.getvalue(), b"OK")
            self.assertEqual(hits.read_bytes(), b"1")
            self.assertEqual(len(processes), 2)
            lite_agent.stop()
            self.assertIsNotNone(processes[1].poll())
            self.assertFalse(lite_agent._owner.is_alive())
            self.assertTrue(results[0].log.closed)

    def test_external_is_neither_launched_nor_stopped_by_the_listener(self):
        port = lite_agent._free_port()
        with subprocess.Popen([sys.executable, "-u", "-c", CHILD, str(port), "unused"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as external, ExitStack() as stack:
            stack.callback(external.terminate)
            stack.enter_context(patch.object(lite_agent, "_external_port", return_value=port))
            launch = stack.enter_context(patch.object(lite_agent, "_launch"))
            stack.callback(lite_agent.stop)
            with patch.dict(os.environ, {"STRATA_AGENT_DISABLE": ""}):
                self.assertIsNone(lite_agent.start(SimpleNamespace(), "127.0.0.1", 19731))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                reached = handler(19731)
                lite_agent.proxy(reached, "GET")
                if reached.send_response.called:
                    break
                time.sleep(.02)
            reached.send_response.assert_called_once_with(200)
            lite_agent.stop()
            launch.assert_not_called()
            self.assertIsNone(lite_agent._active)
            self.assertIsNone(external.poll())

    def test_failed_start_backs_off_and_stop_interrupts_retries(self):
        attempts = []
        third = threading.Event()

        def fail(*args):
            attempts.append(time.monotonic())
            if len(attempts) == 3:
                third.set()

        with patch.dict(os.environ, {"STRATA_AGENT_DISABLE": ""}), patch.object(lite_agent, "_retry_delay", .05), \
             patch.object(lite_agent, "_external_port", return_value=None), patch.object(lite_agent, "_launch", side_effect=fail):
            self.addCleanup(lite_agent.stop)
            lite_agent.start(SimpleNamespace(), "127.0.0.1", 19731)
            self.assertTrue(third.wait(2))
            lite_agent.stop()
            self.assertEqual(len(attempts), 3)
            self.assertGreaterEqual(attempts[1] - attempts[0], .045)
            self.assertGreaterEqual(attempts[2] - attempts[1], .09)
            self.assertFalse(lite_agent._owner.is_alive())

    def test_cached_miss_retry_known_port_and_refused_write(self):
        with patch.object(lite_agent, "_external", (0, None)), patch.object(lite_agent.time, "monotonic") as now, \
             patch.object(lite_agent, "_discover_external_port", side_effect=[None, 19734, 19735]) as discover:
            now.return_value = 100
            for _ in range(100):
                self.assertIsNone(lite_agent._external_port(19731))
            self.assertEqual(discover.call_count, 1)
            now.return_value += 2.01
            self.assertEqual(lite_agent._external_port(19731), 19734)
            now.return_value += 20
            self.assertEqual(lite_agent._external_port(19731), 19734)
            with patch.object(lite_agent, "HTTPConnection") as connection:
                connection.return_value.request.side_effect = ConnectionRefusedError()
                failed = handler(19731)
                lite_agent.proxy(failed, "POST")
                connection.return_value.request.assert_called_once()
                self.assertEqual(failed._json.call_args.args[0], 502)
            self.assertEqual(lite_agent._external_port(19731), 19735)
            self.assertEqual(discover.call_count, 3)

    def test_only_one_scan_for_a_burst(self):
        began = threading.Event()
        release = threading.Event()

        def discover(port):
            began.set()
            release.wait(3)
            return 19734

        with patch.object(lite_agent, "_external", (0, None)), \
             patch.object(lite_agent, "_discover_external_port", side_effect=discover) as scan, ThreadPoolExecutor(8) as pool:
            futures = [pool.submit(lite_agent._external_port, 19731) for _ in range(16)]
            self.assertTrue(began.wait(1))
            release.set()
            self.assertTrue(all(f.result(3) == 19734 for f in futures))
            self.assertEqual(scan.call_count, 1)
