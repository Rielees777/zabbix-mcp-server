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

"""The blocked write-tools file: shipped default, parsing, registration, method-name enforcement."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from zabbix_mcp.client import ClientManager
from zabbix_mcp.config import AppConfig, ConfigError, ServerConfig, ZabbixServerConfig, load_config
from zabbix_mcp.server import _register_tools
from zabbix_mcp.write_policy import PolicyError, WritePolicy, write_tool_names

SERVER_ENV = {
    "ZABBIX_MCP_ZABBIX_SERVERS__PROD__URL": "http://z",
    "ZABBIX_MCP_ZABBIX_SERVERS__PROD__API_TOKEN": "tok",
}


def _cfg(read_only=False):
    return AppConfig(server=ServerConfig(), zabbix_servers={
        "t": ZabbixServerConfig(name="t", url="http://localhost", api_token="x", read_only=read_only)})


def _build(policy):
    mgr = ClientManager(_cfg())
    mcp = MCPServer(name="test")
    _register_tools(mcp, mgr, write_policy=policy)
    return mcp, mgr, {t.name for t in mcp._tool_manager.list_tools()}


class TestShippedList(unittest.TestCase):
    def test_every_write_tool_is_listed_by_default(self):
        """A new write tool must not slip past the restrictions: it has to be added to the file."""
        policy = WritePolicy.load()
        missing = [n for n in write_tool_names() if not policy.blocks(n)]
        self.assertEqual(missing, [], "add these to src/zabbix_mcp/blocked_write_tools.txt "
                                      "(python -m zabbix_mcp.write_policy prints them all)")

    def test_no_read_tool_is_listed(self):
        policy = WritePolicy.load()
        write = set(write_tool_names())
        from zabbix_mcp.api import ALL_METHODS
        wrongly = [m.tool_name for m in ALL_METHODS if m.read_only and policy.blocks(m.tool_name)]
        self.assertEqual(wrongly, [])
        self.assertTrue(write)

    def test_default_registration_leaves_only_read_tools(self):
        _, _, names = _build(WritePolicy.load())
        self.assertIn("host_get", names)
        self.assertIn("problem_get", names)
        self.assertFalse(names & set(write_tool_names()))
        self.assertFalse({n for n in names if n.endswith(("_create", "_update", "_delete"))})


class TestParsing(unittest.TestCase):
    def test_comments_blanks_wildcards_and_case(self):
        p = WritePolicy.parse("# header\n\nHost_Create  # why\n*_delete\n")
        self.assertTrue(p.blocks("host_create"))
        self.assertTrue(p.blocks("item_delete"))
        self.assertFalse(p.blocks("host_get"))
        self.assertEqual(p.patterns, ("host_create", "*_delete"))

    def test_two_names_on_one_line_is_an_error(self):
        with self.assertRaises(PolicyError):
            WritePolicy.parse("host_create host_delete\n")

    def test_removing_a_line_allows_the_tool(self):
        _, _, names = _build(WritePolicy.parse("host_delete\nhost_create\n"))
        self.assertNotIn("host_delete", names)
        self.assertNotIn("host_create", names)
        self.assertIn("host_update", names)

    def test_empty_file_blocks_nothing(self):
        _, _, names = _build(WritePolicy.parse("# nothing\n"))
        self.assertIn("host_delete", names)

    def test_unknown_name_is_warned_about(self):
        with self.assertLogs("zabbix_mcp.write_policy", level="WARNING") as logs:
            WritePolicy.parse("host_craete\n")._warn_about_unknown()
        self.assertTrue(any("host_craete" in line for line in logs.output))

    def test_missing_file_fails_closed(self):
        with self.assertRaises(PolicyError):
            WritePolicy.load("/nonexistent/blocked.txt")


class TestMethodNames(unittest.TestCase):
    def test_api_method_maps_to_its_tool(self):
        p = WritePolicy.parse("host_delete\n")
        self.assertTrue(p.blocks_api_method("host.delete"))
        self.assertTrue(p.blocks_api_method(" HOST.Delete "))
        self.assertFalse(p.blocks_api_method("host.get"))
        self.assertFalse(p.blocks_api_method("host.update"))

    def _raw(self, policy):
        mcp, mgr, names = _build(policy)
        return mgr, mcp._tool_manager.get_tool("zabbix_raw_api_call")

    def test_raw_call_is_refused_per_method_when_it_is_unblocked(self):
        mgr, raw = self._raw(WritePolicy.parse("host_delete\n"))
        with patch.object(mgr, "call", return_value={}) as call, \
                patch.object(mgr, "get_version", return_value="7.4.0"), \
                patch.object(mgr, "get_zabbix_timezone", return_value="UTC"):
            with self.assertRaises(ToolError) as ctx:
                asyncio.run(raw.fn(method="host.delete", params={}))
            self.assertIn("blocked write-tools file", str(ctx.exception))
            call.assert_not_called()
            asyncio.run(raw.fn(method="host.update", params={"hostid": "1"}))   # not listed: allowed
        self.assertEqual(call.call_count, 1)

    def test_prepare_and_confirm_obey_the_list(self):
        mcp, mgr, names = _build(WritePolicy.parse("host_delete\n"))
        prepare = mcp._tool_manager.get_tool("action_prepare").fn
        confirm = mcp._tool_manager.get_tool("action_confirm").fn
        with self.assertRaises(ToolError):
            asyncio.run(prepare(action="host.delete", params={"hostids": ["1"]}))
        ok = json.loads(asyncio.run(prepare(action="host.update", params={"hostid": "1"})))
        self.assertIn("confirmation_token", ok)

    def test_prepare_refuses_user_methods_too(self):
        mcp, _, _ = _build(WritePolicy.parse(""))
        prepare = mcp._tool_manager.get_tool("action_prepare").fn
        for action in ("user.create", "usergroup.update", "role.delete", "token.generate"):
            with self.subTest(action=action), self.assertRaises(ToolError):
                asyncio.run(prepare(action=action, params={}))


class TestFromEnvironment(unittest.TestCase):
    def _load(self, **extra):
        with patch.dict(os.environ, {**SERVER_ENV, **extra}, clear=True):
            return load_config()

    def test_default_is_the_shipped_list(self):
        cfg = self._load()
        self.assertTrue(cfg._write_policy.blocks("host_delete"))
        self.assertIn("built in", cfg._write_policy.source)

    def test_custom_file_replaces_it(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("host_delete\n")
        try:
            cfg = self._load(ZABBIX_MCP_SERVER__BLOCKED_TOOLS_FILE=f.name)
        finally:
            os.unlink(f.name)
        self.assertTrue(cfg._write_policy.blocks("host_delete"))
        self.assertFalse(cfg._write_policy.blocks("host_create"))

    def test_unreadable_file_stops_the_start(self):
        with self.assertRaises(ConfigError):
            self._load(ZABBIX_MCP_SERVER__BLOCKED_TOOLS_FILE="/nonexistent/blocked.txt")


if __name__ == "__main__":
    unittest.main()
