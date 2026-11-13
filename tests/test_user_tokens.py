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

"""Per-user Zabbix tokens: a server with ``token_header`` runs every request with the
token that request brings, and refuses one that brings none.

Runs against a fake Zabbix JSON-RPC endpoint that records the token it was called with.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from zabbix_mcp.auth import current_user_tokens
from zabbix_mcp.client import ClientManager, MissingTokenError
from zabbix_mcp.config import AppConfig, ConfigError, ServerConfig, ZabbixServerConfig, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
BEARER = "e2e-user-token-bearer-0123456789abcdef"


class FakeZabbix:
    """Answers just enough of the Zabbix API and remembers who called what."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[tuple[str, str]] = []     # (method, token)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                auth = self.headers.get("Authorization", "")
                token = auth[7:] if auth.startswith("Bearer ") else (body.get("auth") or "")
                method = body.get("method", "")
                params = body.get("params") or {}
                if method == "user.checkAuthentication":
                    token = params.get("token") or params.get("sessionid") or token
                fake.calls.append((method, token))
                if method == "apiinfo.version":
                    result = "7.0.30"
                elif method == "user.checkAuthentication":
                    result = {"userid": "1", "username": "u", "timezone": "default"}
                elif method == "settings.get":
                    result = {"default_timezone": "UTC"}
                elif method == "host.get":
                    result = [{"hostid": "10", "host": f"{fake.label}:{token}", "name": "x", "status": "0"}]
                else:
                    result = []
                payload = json.dumps({"jsonrpc": "2.0", "result": result, "id": body.get("id")}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def tokens_for(self, method: str) -> list[str]:
        return [t for m, t in self.calls if m == method]


def _config(url_a: str, url_b: str | None = None) -> AppConfig:
    servers = {"a": ZabbixServerConfig(name="a", url=url_a, token_header="X-MCP-Token-UT")}
    if url_b:
        servers["b"] = ZabbixServerConfig(name="b", url=url_b, token_header="x-mcp-token-uts")
    return AppConfig(server=ServerConfig(), zabbix_servers=servers)


class TestConfig(unittest.TestCase):
    def test_header_is_normalised_and_api_token_not_required(self):
        srv = ZabbixServerConfig(name="a", url="http://z", token_header=" X-MCP-Token-UT ")
        self.assertEqual(srv.token_header, "x-mcp-token-ut")

    def test_service_token_still_required_without_a_header(self):
        with self.assertRaises(ValidationError):
            ZabbixServerConfig(name="a", url="http://z")

    def test_header_and_service_token_together_are_refused(self):
        with self.assertRaises(ValidationError) as ctx:
            ZabbixServerConfig(name="a", url="http://z", token_header="x-mcp-token-ut", api_token="t")
        self.assertIn("token_header", str(ctx.exception))

    def test_header_with_password_login_is_refused(self):
        with self.assertRaises(ValidationError):
            ZabbixServerConfig(name="a", url="http://z", token_header="x-t", legacy_auth=True,
                               username="u", password="p")

    def test_bad_or_reserved_header_names(self):
        for bad in ("Authorization", "cookie", "X Token", "x/token", "host"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                ZabbixServerConfig(name="a", url="http://z", token_header=bad)

    def test_two_servers_may_share_a_header_but_not_a_key(self):
        env = {
            "ZABBIX_MCP_ZABBIX_SERVERS__A__URL": "http://a", "ZABBIX_MCP_ZABBIX_SERVERS__A__TOKEN_HEADER": "X-T",
            "ZABBIX_MCP_ZABBIX_SERVERS__B__URL": "http://b", "ZABBIX_MCP_ZABBIX_SERVERS__B__TOKEN_HEADER": "x-t",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(sorted(load_config().zabbix_servers), ["a", "b"])
        env["ZABBIX_MCP_ZABBIX_SERVERS__B__TOKEN_KEY"] = "A"
        with patch.dict(os.environ, env, clear=True), self.assertRaises(ConfigError) as ctx:
            load_config()
        self.assertIn("TOKEN_KEY", str(ctx.exception))

    def test_environment_loading(self):
        env = {
            "ZABBIX_MCP_ZABBIX_SERVERS__A__URL": "http://a", "ZABBIX_MCP_ZABBIX_SERVERS__A__TOKEN_HEADER": "X-MCP-Token-UT",
            "ZABBIX_MCP_ZABBIX_SERVERS__B__URL": "http://b", "ZABBIX_MCP_ZABBIX_SERVERS__B__API_TOKEN": "svc",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertEqual(cfg.zabbix_servers["a"].token_header, "x-mcp-token-ut")
        self.assertIsNone(cfg.zabbix_servers["b"].token_header)


class TestClientManager(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zbx_a = FakeZabbix("a")
        cls.zbx_b = FakeZabbix("b")

    @classmethod
    def tearDownClass(cls):
        cls.zbx_a.stop()
        cls.zbx_b.stop()

    def _call_as(self, mgr, server, tokens, method="host.get", params=None):
        reset = current_user_tokens.set(tokens)
        try:
            return mgr.call(server, method, params or {"limit": 1})
        finally:
            current_user_tokens.reset(reset)

    def test_each_request_uses_its_own_token_and_client(self):
        mgr = ClientManager(_config(self.zbx_a.url))
        r1 = self._call_as(mgr, "a", {"x-mcp-token-ut": "alice-token"})
        r2 = self._call_as(mgr, "a", {"x-mcp-token-ut": "bob-token"})
        r3 = self._call_as(mgr, "a", {"x-mcp-token-ut": "alice-token"})
        self.assertEqual(r1[0]["host"], "a:alice-token")
        self.assertEqual(r2[0]["host"], "a:bob-token")
        self.assertEqual(r3[0]["host"], "a:alice-token")
        self.assertEqual(len(mgr._clients), 2, "one client per token, reused for the same token")

    def test_no_token_is_refused_and_nothing_reaches_zabbix(self):
        zbx = FakeZabbix("n")
        try:
            mgr = ClientManager(_config(zbx.url))
            for tokens in (None, {}, {"x-mcp-token-ut": "   "}, {"x-mcp-token-uts": "other-header"}):
                with self.subTest(tokens=tokens), self.assertRaises(MissingTokenError) as ctx:
                    self._call_as(mgr, "a", tokens)
                self.assertIn("X-MCP-Token-UT".lower(), str(ctx.exception).lower())
            self.assertEqual(zbx.calls, [])
        finally:
            zbx.stop()

    def test_two_servers_take_their_own_headers(self):
        mgr = ClientManager(_config(self.zbx_a.url, self.zbx_b.url))
        tokens = {"x-mcp-token-ut": "token-for-a", "x-mcp-token-uts": "token-for-b"}
        self.assertEqual(self._call_as(mgr, "a", tokens)[0]["host"], "a:token-for-a")
        self.assertEqual(self._call_as(mgr, "b", tokens)[0]["host"], "b:token-for-b")
        # Only B's header present: A is refused, B works.
        only_b = {"x-mcp-token-uts": "token-for-b"}
        with self.assertRaises(MissingTokenError):
            self._call_as(mgr, "a", only_b)
        self.assertEqual(self._call_as(mgr, "b", only_b)[0]["host"], "b:token-for-b")

    def test_version_is_known_without_any_token(self):
        mgr = ClientManager(_config(self.zbx_a.url))
        self.assertEqual(mgr.get_version("a"), "7.0.30")
        mgr.warm_versions().join(5)
        self.assertEqual(mgr.cached_versions()["a"], "7.0.30")

    def test_effective_token(self):
        mgr = ClientManager(_config(self.zbx_a.url))
        reset = current_user_tokens.set({"x-mcp-token-ut": "tok-1"})
        try:
            self.assertEqual(mgr.effective_token("a"), "tok-1")
        finally:
            current_user_tokens.reset(reset)

    def test_stray_zabbix_utils_environment_is_ignored(self):
        """ZABBIX_PASSWORD & co. of the process must not become credentials for every connection."""
        stray = {"ZABBIX_USER": "someone", "ZABBIX_PASSWORD": "secret-password-1",
                 "ZABBIX_TOKEN": "stray-global-token", "ZABBIX_URL": "http://elsewhere.invalid"}
        for variables in (stray, {"ZABBIX_PASSWORD": "secret-password-1"}):
            with self.subTest(variables=sorted(variables)), patch.dict(os.environ, variables):
                with self.assertLogs("zabbix_mcp.client", level="WARNING") as logs:
                    mgr = ClientManager(_config(self.zbx_a.url))
                for name in variables:
                    self.assertNotIn(name, os.environ)
                self.assertTrue(any("ZABBIX_PASSWORD" in line for line in logs.output))
                self.assertNotIn("secret-password-1", "\n".join(logs.output), "values are never logged")
                rows = self._call_as(mgr, "a", {"x-mcp-token-ut": "alice-after-stray"})
                self.assertEqual(rows[0]["host"], "a:alice-after-stray")

    def test_service_token_server_is_not_hijacked_by_stray_environment(self):
        zbx = FakeZabbix("svc")
        try:
            cfg = AppConfig(server=ServerConfig(), zabbix_servers={
                "svc": ZabbixServerConfig(name="svc", url=zbx.url, api_token="service-token")})
            with patch.dict(os.environ, {"ZABBIX_PASSWORD": "x", "ZABBIX_TOKEN": "stray"}):
                mgr = ClientManager(cfg)
                self.assertEqual(mgr.call("svc", "host.get", {"limit": 1})[0]["host"], "svc:service-token")
        finally:
            zbx.stop()

    def test_user_clients_are_bounded(self):
        mgr = ClientManager(_config(self.zbx_a.url))
        mgr._MAX_USER_CLIENTS = 3
        for i in range(6):
            self._call_as(mgr, "a", {"x-mcp-token-ut": f"user-{i}"})
        self.assertEqual(len(mgr._clients), 3)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {BEARER}",
        **headers,
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw, status, hdrs = resp.read().decode(), resp.status, resp.headers
    except urllib.error.HTTPError as exc:
        raw, status, hdrs = exc.read().decode(), exc.code, exc.headers
    try:
        return status, (json.loads(raw) if raw.strip() else None), hdrs
    except json.JSONDecodeError:
        for line in raw.splitlines():
            if line.startswith("data: "):
                return status, json.loads(line[6:]), hdrs
    return status, None, hdrs


_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "e2e-tokens", "version": "0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


class TestOverHttp(unittest.TestCase):
    """A real server process: tokens must follow each request, in both protocol generations."""

    @classmethod
    def setUpClass(cls):
        cls.zbx_a = FakeZabbix("a")
        cls.zbx_b = FakeZabbix("b")
        port = _free_port()
        cls.url = f"http://127.0.0.1:{port}/mcp"
        env = {
            **os.environ,
            "ZABBIX_MCP_SERVER__TRANSPORT": "http", "ZABBIX_MCP_SERVER__PORT": str(port),
            "ZABBIX_MCP_SERVER__AUTH_TOKEN": BEARER,
            "ZABBIX_MCP_ZABBIX_SERVERS__ZBX_A__URL": cls.zbx_a.url,
            "ZABBIX_MCP_ZABBIX_SERVERS__ZBX_A__TOKEN_HEADER": "X-MCP-Token-UT",
            "ZABBIX_MCP_ZABBIX_SERVERS__ZBX_B__URL": cls.zbx_b.url,
            "ZABBIX_MCP_ZABBIX_SERVERS__ZBX_B__TOKEN_HEADER": "X-MCP-Token-UTS",
        }
        cls._log = open(Path(tempfile.gettempdir()) / "zmcp-user-tokens-e2e.log", "w")
        cls._proc = subprocess.Popen(
            [sys.executable, "-c", "from zabbix_mcp.cli import main; main()"],
            cwd=tempfile.gettempdir(), env=env, stdout=cls._log, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
                break
            except Exception:
                time.sleep(0.3)
        else:
            cls._proc.terminate()
            raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls._proc.terminate()
        cls._proc.wait(timeout=10)
        cls._log.close()
        cls.zbx_a.stop()
        cls.zbx_b.stop()

    # -- helpers -----------------------------------------------------------

    def _session(self):
        status, resp, hdrs = _post(self.url, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "e2e", "version": "0"}},
        }, {})
        sid = hdrs.get("Mcp-Session-Id")
        _post(self.url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"Mcp-Session-Id": sid})
        return sid

    def _legacy_call(self, sid, server, extra_headers, req_id=2):
        status, resp, _ = _post(self.url, {
            "jsonrpc": "2.0", "id": req_id, "method": "tools/call",
            "params": {"name": "host_get", "arguments": {"server": server, "limit": 1}},
        }, {"Mcp-Session-Id": sid, "MCP-Protocol-Version": "2025-11-25", **extra_headers})
        return resp["result"]

    def _stateless_call(self, server, extra_headers):
        status, resp, _ = _post(self.url, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"_meta": _META, "name": "host_get", "arguments": {"server": server, "limit": 1}},
        }, {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": "host_get",
            **extra_headers})
        return resp["result"]

    @staticmethod
    def _text(result) -> str:
        return "\n".join(c.get("text", "") for c in result.get("content", []))

    # -- tests -------------------------------------------------------------

    def test_legacy_session_uses_the_token_of_each_request(self):
        sid = self._session()
        first = self._legacy_call(sid, "zbx_a", {"X-MCP-Token-UT": "alice-legacy"})
        second = self._legacy_call(sid, "zbx_a", {"X-MCP-Token-UT": "bob-legacy"}, req_id=3)
        self.assertIn("a:alice-legacy", self._text(first))
        self.assertIn("a:bob-legacy", self._text(second))
        self.assertNotIn("alice-legacy", self._text(second), "a later request must not reuse the session's first token")

    def test_legacy_missing_header_is_rejected_without_calling_zabbix(self):
        sid = self._session()
        before = len(self.zbx_a.calls)
        result = self._legacy_call(sid, "zbx_a", {})
        self.assertTrue(result.get("isError"))
        self.assertIn("x-mcp-token-ut", self._text(result).lower())
        host_calls = [t for m, t in self.zbx_a.calls[before:] if m == "host.get"]
        self.assertEqual(host_calls, [])

    def test_legacy_each_server_reads_its_own_header(self):
        sid = self._session()
        headers = {"X-MCP-Token-UT": "tok-for-a", "X-MCP-Token-UTS": "tok-for-b"}
        self.assertIn("a:tok-for-a", self._text(self._legacy_call(sid, "zbx_a", headers)))
        self.assertIn("b:tok-for-b", self._text(self._legacy_call(sid, "zbx_b", headers, req_id=3)))
        # a header meant for the other server never authenticates this one
        wrong = self._legacy_call(sid, "zbx_a", {"X-MCP-Token-UTS": "tok-for-b"}, req_id=4)
        self.assertTrue(wrong.get("isError"))

    def test_stateless_requests_use_their_own_token(self):
        one = self._stateless_call("zbx_a", {"X-MCP-Token-UT": "alice-stateless"})
        two = self._stateless_call("zbx_a", {"X-MCP-Token-UT": "bob-stateless"})
        self.assertIn("a:alice-stateless", self._text(one))
        self.assertIn("a:bob-stateless", self._text(two))

    def test_raw_json_is_not_advertised_while_it_is_refused(self):
        sid = self._session()
        status, resp, _ = _post(self.url, {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}},
                                {"Mcp-Session-Id": sid, "MCP-Protocol-Version": "2025-11-25"})
        tools = resp["result"]["tools"]
        self.assertGreater(len(tools), 50)
        offenders = [t["name"] for t in tools if "raw_json" in t["inputSchema"].get("properties", {})]
        self.assertEqual(offenders, [])
        # asking for it anyway is still refused
        result = self._legacy_call_args(sid, {"server": "zbx_a", "limit": 1, "raw_json": True},
                                        {"X-MCP-Token-UT": "tok"})
        self.assertTrue(result.get("isError"))
        self.assertIn("raw_json", self._text(result))

    def _legacy_call_args(self, sid, arguments, extra_headers):
        status, resp, _ = _post(self.url, {
            "jsonrpc": "2.0", "id": 11, "method": "tools/call",
            "params": {"name": "host_get", "arguments": arguments},
        }, {"Mcp-Session-Id": sid, "MCP-Protocol-Version": "2025-11-25", **extra_headers})
        return resp["result"]

    def test_stateless_missing_header_is_rejected(self):
        result = self._stateless_call("zbx_b", {"X-MCP-Token-UT": "only-the-other-header"})
        self.assertTrue(result.get("isError"))


if __name__ == "__main__":
    unittest.main()
