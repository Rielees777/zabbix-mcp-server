#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#

"""The request gate (auth_mode=per_request_token): X-Request-By marker, scope handling, real server."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from tests.test_user_tokens import FakeZabbix
from zabbix_mcp.config import AppConfig, ConfigError, ServerConfig, ZabbixServerConfig, load_config
from zabbix_mcp.server import _make_request_context_middleware


def _cfg(**server) -> AppConfig:
    return AppConfig(
        server=ServerConfig(**server),
        zabbix_servers={"ut": ZabbixServerConfig(name="ut", url="http://a", token_header="X-MCP-Token-UT")},
    )


def _gated(**server):
    return _cfg(auth_mode="per_request_token", **server)


class _Harness:
    """Runs a scope through the middleware and records what reached the app / the client."""

    def __init__(self, config, trusted=()):
        self.reached = []
        self.sent = []

        async def inner(scope, receive, send):
            self.reached.append(scope["type"])
            if scope["type"] == "http":
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

        self.app = _make_request_context_middleware(inner, list(trusted), config)

    def run(self, scope):
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            self.sent.append(message)

        asyncio.run(self.app(scope, receive, send))
        return self

    @property
    def status(self):
        return next((m["status"] for m in self.sent if m["type"] == "http.response.start"), None)

    @property
    def body(self):
        return b"".join(m.get("body", b"") for m in self.sent if m["type"] == "http.response.body")


def _http(path="/mcp", headers=None):
    return {"type": "http", "path": path, "client": ("10.0.0.1", 1234),
            "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()]}


class TestGate(unittest.TestCase):
    def test_missing_header_is_401_and_never_reaches_the_app(self):
        h = _Harness(_gated()).run(_http())
        self.assertEqual(h.status, 401)
        self.assertEqual(h.reached, [])
        self.assertIn("X-Request-By", json.loads(h.body)["detail"])
        challenge = dict(next(m for m in h.sent if m["type"] == "http.response.start")["headers"])
        self.assertIn(b"www-authenticate", challenge)

    def test_wrong_value_is_401_and_the_right_one_passes(self):
        self.assertEqual(_Harness(_gated()).run(_http(headers={"X-Request-By": "robot"})).status, 401)
        ok = _Harness(_gated()).run(_http(headers={"X-Request-By": "personal"}))
        self.assertEqual(ok.status, 200)
        self.assertEqual(ok.reached, ["http"])

    def test_header_name_is_case_insensitive_the_value_is_not(self):
        self.assertEqual(_Harness(_gated()).run(_http(headers={"x-request-by": "personal"})).status, 200)
        self.assertEqual(_Harness(_gated()).run(_http(headers={"X-REQUEST-BY": "personal"})).status, 200)
        self.assertEqual(_Harness(_gated()).run(_http(headers={"X-Request-By": "Personal"})).status, 401)

    def test_configured_values_and_header_name(self):
        cfg = _gated(allowed_request_by="personal, service", request_by_header="X-Gateway-Tag")
        self.assertEqual(_Harness(cfg).run(_http(headers={"X-Gateway-Tag": "service"})).status, 200)
        self.assertEqual(_Harness(cfg).run(_http(headers={"X-Request-By": "personal"})).status, 401)

    def test_lifespan_is_never_judged_and_gets_no_http_response(self):
        """The regression: the gate answered the app-start lifespan scope with a 401 and crashed uvicorn."""
        h = _Harness(_gated()).run({"type": "lifespan"})
        self.assertEqual(h.reached, ["lifespan"])
        self.assertEqual(h.sent, [])

    def test_health_and_report_links_stay_open(self):
        for path in ("/health", "/reports/0123abcd.pdf"):
            with self.subTest(path=path):
                self.assertEqual(_Harness(_gated()).run(_http(path=path)).status, 200)
        # look-alikes are not exempt
        for path in ("/healthz", "/health/x", "/report", "/mcp/reports/x"):
            with self.subTest(path=path):
                self.assertEqual(_Harness(_gated()).run(_http(path=path)).status, 401)

    def test_websocket_is_closed(self):
        h = _Harness(_gated()).run({"type": "websocket", "path": "/mcp", "headers": [], "client": ("1.1.1.1", 1)})
        self.assertEqual(h.reached, [])
        self.assertEqual(h.sent, [{"type": "websocket.close", "code": 1008}])

    def test_service_mode_has_no_gate(self):
        for config in (_cfg(), None):
            h = _Harness(config).run(_http())
            self.assertEqual(h.status, 200)
            self.assertEqual(h.reached, ["http"])


class TestGateConfig(unittest.TestCase):
    ENV = {"ZABBIX_MCP_ZABBIX_SERVERS__UT__URL": "http://ut",
           "ZABBIX_MCP_ZABBIX_SERVERS__UT__TOKEN_HEADER": "X-MCP-Token-UT"}

    def _load(self, **extra):
        with patch.dict(os.environ, {**self.ENV, **extra}, clear=True):
            return load_config()

    def test_defaults(self):
        cfg = self._load()
        self.assertEqual(cfg.server.auth_mode, "service")
        self.assertEqual(cfg.server.allowed_request_by, ["personal"])
        self.assertEqual(cfg.server.request_by_header, "X-Request-By")

    def test_environment(self):
        cfg = self._load(ZABBIX_MCP_SERVER__AUTH_MODE="per_request_token",
                         ZABBIX_MCP_SERVER__ALLOWED_REQUEST_BY="personal,service")
        self.assertEqual(cfg.server.auth_mode, "per_request_token")
        self.assertEqual(cfg.server.allowed_request_by, ["personal", "service"])

    def test_empty_allowed_list_falls_back_instead_of_locking_everyone_out(self):
        self.assertEqual(ServerConfig(allowed_request_by="").allowed_request_by, ["personal"])

    def test_bad_mode_and_header_name(self):
        with self.assertRaises(ConfigError):
            self._load(ZABBIX_MCP_SERVER__AUTH_MODE="whatever")
        with self.assertRaises(ValidationError):
            ServerConfig(request_by_header="X Request By")

    def test_per_request_mode_needs_a_token_header_on_every_server(self):
        env = {"ZABBIX_MCP_ZABBIX_SERVERS__SVC__URL": "http://svc",
               "ZABBIX_MCP_ZABBIX_SERVERS__SVC__API_TOKEN": "service-token"}
        with self.assertRaises(ConfigError) as ctx:
            self._load(**env, ZABBIX_MCP_SERVER__AUTH_MODE="per_request_token")
        self.assertIn("svc", str(ctx.exception))
        # the same servers are fine in the default mode
        self.assertEqual(sorted(self._load(**env).zabbix_servers), ["svc", "ut"])


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(url, headers):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "gate", "version": "0"}}}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


class TestGatedServerOverHttp(unittest.TestCase):
    """A real server process in per_request_token mode: it must start and serve /mcp."""

    @classmethod
    def setUpClass(cls):
        cls.zbx = FakeZabbix("g")
        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}"
        env = {
            **os.environ,
            "ZABBIX_MCP_SERVER__TRANSPORT": "http", "ZABBIX_MCP_SERVER__PORT": str(port),
            "ZABBIX_MCP_SERVER__AUTH_MODE": "per_request_token",
            "ZABBIX_MCP_ZABBIX_SERVERS__UT__URL": cls.zbx.url,
            "ZABBIX_MCP_ZABBIX_SERVERS__UT__TOKEN_HEADER": "X-MCP-Token-UT",
        }
        cls._log_path = Path(tempfile.gettempdir()) / "zmcp-gate-e2e.log"
        cls._log = open(cls._log_path, "w")
        cls._proc = subprocess.Popen(
            [sys.executable, "-c", "from zabbix_mcp.cli import main; main()"],
            cwd=tempfile.gettempdir(), env=env, stdout=cls._log, stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        while time.time() < deadline:
            if cls._proc.poll() is not None:
                break
            try:
                urllib.request.urlopen(f"{cls.base}/health", timeout=2)
                return
            except Exception:
                time.sleep(0.3)
        cls._log.flush()
        raise RuntimeError("server did not start:\n" + cls._log_path.read_text()[-1500:])

    @classmethod
    def tearDownClass(cls):
        cls._proc.terminate()
        cls._proc.wait(timeout=10)
        cls._log.close()
        cls.zbx.stop()

    def test_the_server_started_its_lifespan(self):
        self._log.flush()
        text = self._log_path.read_text()
        self.assertIn("session manager started", text)
        self.assertNotIn("lifespan' protocol", text)
        self.assertNotIn("AssertionError", text)
        self.assertIn("Request gate ON", text)

    def test_health_needs_no_marker(self):
        with urllib.request.urlopen(f"{self.base}/health", timeout=5) as resp:
            self.assertEqual(resp.status, 200)

    def test_mcp_needs_the_marker(self):
        self.assertEqual(_post(f"{self.base}/mcp", {}), 401)
        self.assertEqual(_post(f"{self.base}/mcp", {"X-Request-By": "intruder"}), 401)
        self.assertEqual(_post(f"{self.base}/mcp", {"X-Request-By": "personal"}), 200)


if __name__ == "__main__":
    unittest.main()
