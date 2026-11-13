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

"""Several Zabbix servers' tokens in one header ("ut=<token>;uts=<token>").

For a gateway that decrypts a single header only (Higress Token Unseal on X-MCP-Token):
the user encrypts the whole string, the gateway forwards it decrypted, the server splits it.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from pydantic import ValidationError

from tests.test_request_gate import _post
from tests.test_user_tokens import FakeZabbix
from zabbix_mcp.auth import current_user_tokens, split_keyed_tokens
from zabbix_mcp.client import ClientManager, MissingTokenError
from zabbix_mcp.config import AppConfig, ServerConfig, ZabbixServerConfig

HEADER = "x-mcp-token"


def _shared(url_ut, url_uts, **keys) -> AppConfig:
    return AppConfig(server=ServerConfig(), zabbix_servers={
        "ut": ZabbixServerConfig(name="ut", url=url_ut, token_header="X-MCP-Token", token_key=keys.get("ut")),
        "uts": ZabbixServerConfig(name="uts", url=url_uts, token_header="X-MCP-Token", token_key=keys.get("uts")),
    })


class TestSplit(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(split_keyed_tokens("ut=a1;uts=b2"), {"ut": "a1", "uts": "b2"})
        self.assertEqual(split_keyed_tokens(" UT = a1 , uts=b2 ; "), {"ut": "a1", "uts": "b2"})
        self.assertEqual(split_keyed_tokens("uts=b2"), {"uts": "b2"})

    def test_plain_tokens_are_not_split(self):
        for raw in ("0123456789abcdef", "abc==", "a=b=c", "ut=", "=x", "ut=a b"):
            with self.subTest(raw=raw):
                self.assertIsNone(split_keyed_tokens(raw))

    def test_repeated_key_is_an_error(self):
        with self.assertRaises(ValueError):
            split_keyed_tokens("ut=a;UT=b")

    def test_key_validation(self):
        self.assertEqual(ZabbixServerConfig(name="UT", url="http://a", token_header="x", token_key=" Prod ").effective_token_key, "prod")
        self.assertEqual(ZabbixServerConfig(name="UT", url="http://a", token_header="x").effective_token_key, "ut")
        with self.assertRaises(ValidationError):
            ZabbixServerConfig(name="ut", url="http://a", token_header="x", token_key="u t")


class TestClientManager(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ut = FakeZabbix("ut")
        cls.uts = FakeZabbix("uts")

    @classmethod
    def tearDownClass(cls):
        cls.ut.stop()
        cls.uts.stop()

    def _call(self, mgr, server, value):
        reset = current_user_tokens.set({HEADER: value} if value is not None else {})
        try:
            return mgr.call(server, "host.get", {"limit": 1})[0]["host"]
        finally:
            current_user_tokens.reset(reset)

    def test_each_server_gets_its_own_part(self):
        mgr = ClientManager(_shared(self.ut.url, self.uts.url))
        value = "ut=token-for-ut;uts=token-for-uts"
        self.assertEqual(self._call(mgr, "ut", value), "ut:token-for-ut")
        self.assertEqual(self._call(mgr, "uts", value), "uts:token-for-uts")
        self.assertNotIn("token-for-uts", [t for _, t in self.ut.calls], "a token never reaches the other server")
        self.assertNotIn("token-for-ut", [t for _, t in self.uts.calls])

    def test_one_part_only_serves_that_server(self):
        mgr = ClientManager(_shared(self.ut.url, self.uts.url))
        self.assertEqual(self._call(mgr, "uts", "uts=only-uts"), "uts:only-uts")
        with self.assertRaises(MissingTokenError) as ctx:
            self._call(mgr, "ut", "uts=only-uts")
        self.assertIn("'ut'", str(ctx.exception))
        self.assertIn("ut=<token>;uts=<token>", str(ctx.exception))

    def test_a_plain_token_is_ambiguous_when_the_header_is_shared(self):
        mgr = ClientManager(_shared(self.ut.url, self.uts.url))
        with self.assertRaises(MissingTokenError) as ctx:
            self._call(mgr, "ut", "0123456789abcdef")
        self.assertIn("share", str(ctx.exception))

    def test_malformed_and_missing(self):
        mgr = ClientManager(_shared(self.ut.url, self.uts.url))
        for value in ("ut=a;ut=b", None, "  "):
            with self.subTest(value=value), self.assertRaises(MissingTokenError):
                self._call(mgr, "ut", value)

    def test_custom_keys(self):
        mgr = ClientManager(_shared(self.ut.url, self.uts.url, ut="prod", uts="stage"))
        self.assertEqual(self._call(mgr, "uts", "prod=p1;stage=s1"), "uts:s1")

    def test_a_header_used_by_one_server_still_takes_a_plain_token(self):
        cfg = AppConfig(server=ServerConfig(), zabbix_servers={
            "ut": ZabbixServerConfig(name="ut", url=self.ut.url, token_header="X-MCP-Token")})
        mgr = ClientManager(cfg)
        self.assertEqual(self._call(mgr, "ut", "plain-token"), "ut:plain-token")
        self.assertEqual(self._call(mgr, "ut", "ut=keyed-token"), "ut:keyed-token")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestOneRouteOverHttp(unittest.TestCase):
    """The deployment as published: one route, one X-MCP-Token, two Zabbix servers."""

    @classmethod
    def setUpClass(cls):
        cls.ut = FakeZabbix("ut")
        cls.uts = FakeZabbix("uts")
        port = _free_port()
        cls.url = f"http://127.0.0.1:{port}/mcp"
        env = {
            **os.environ,
            "ZABBIX_MCP_SERVER__TRANSPORT": "http", "ZABBIX_MCP_SERVER__PORT": str(port),
            "ZABBIX_MCP_SERVER__AUTH_MODE": "per_request_token",
            "ZABBIX_MCP_ZABBIX_SERVERS__UT__URL": cls.ut.url,
            "ZABBIX_MCP_ZABBIX_SERVERS__UT__TOKEN_HEADER": "X-MCP-Token",
            "ZABBIX_MCP_ZABBIX_SERVERS__UTS__URL": cls.uts.url,
            "ZABBIX_MCP_ZABBIX_SERVERS__UTS__TOKEN_HEADER": "X-MCP-Token",
        }
        cls._log = open(Path(tempfile.gettempdir()) / "zmcp-shared-header-e2e.log", "w")
        cls._proc = subprocess.Popen([sys.executable, "-c", "from zabbix_mcp.cli import main; main()"],
                                     cwd=tempfile.gettempdir(), env=env, stdout=cls._log, stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
                return
            except Exception:
                time.sleep(0.3)
        cls._proc.terminate()
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls._proc.terminate()
        cls._proc.wait(timeout=10)
        cls._log.close()
        cls.ut.stop()
        cls.uts.stop()

    def _host_get(self, server, token_value):
        headers = {"X-Request-By": "personal", "X-MCP-Token": token_value, "MCP-Protocol-Version": "2025-11-25"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "host_get", "arguments": {"server": server, "limit": 1}}}
        status, _, payload = _post(self.url, headers, body=body, full=True)
        self.assertEqual(status, 200)
        result = payload["result"]
        return result.get("isError", False), "".join(c.get("text", "") for c in result["content"])

    def test_both_servers_through_one_header(self):
        value = "ut=e2e-token-ut;uts=e2e-token-uts"
        err, text = self._host_get("ut", value)
        self.assertFalse(err, text)
        self.assertIn("ut:e2e-token-ut", text)
        err, text = self._host_get("uts", value)
        self.assertFalse(err, text)
        self.assertIn("uts:e2e-token-uts", text)

    def test_missing_part_is_a_clear_error(self):
        err, text = self._host_get("ut", "uts=only-uts")
        self.assertTrue(err)
        self.assertIn("ut=<token>;uts=<token>", text)

    def test_plain_token_is_refused_for_a_shared_header(self):
        err, text = self._host_get("uts", "0123456789abcdef")
        self.assertTrue(err)
        self.assertIn("share", text)


if __name__ == "__main__":
    unittest.main()
