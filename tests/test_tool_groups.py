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

"""Task-oriented tool groups, composed through ZABBIX_MCP_SERVER__TOOLS."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from mcp.server.mcpserver import MCPServer

from zabbix_mcp.client import ClientManager
from zabbix_mcp.config import TASK_GROUPS, TOOL_GROUPS, AppConfig, ServerConfig, ZabbixServerConfig, _expand_tool_groups, load_config
from zabbix_mcp.server import _register_tools, _strip_raw_json, _warn_unknown_tool_entries
from zabbix_mcp.write_policy import WritePolicy, write_tool_names

ENV = {
    "ZABBIX_MCP_ZABBIX_SERVERS__A__URL": "http://a",
    "ZABBIX_MCP_ZABBIX_SERVERS__A__API_TOKEN": "t",
}

EXPECTED = {
    "status": {"host_get", "hostgroup_get", "host_status_get", "hostgroup_overview_get",
               "infrastructure_summary_get", "health_check"},
    "problems": {"problem_active_get", "event_get", "trigger_get"},
    "metrics": {"item_get", "trend_get", "history_get", "item_history_summary_get", "graph_get", "graphitem_get"},
    "audit": {"auditlog_get"},
    "availability": {"sla_get", "sla_getsli", "maintenance_get"},
    "notifications": {"alert_get"},
    "analytics": {"item_threshold_search", "capacity_forecast", "anomaly_detect"},
    "inventory": {"template_get", "hostinterface_get", "proxy_get", "httptest_get", "hanode_get", "housekeeping_get"},
    "problems_raw": {"problem_get"},
}


def _tools_for(entries):
    cfg = AppConfig(zabbix_servers={"a": ZabbixServerConfig(name="a", url="http://a", api_token="t")})
    mcp = MCPServer(name="t")
    _register_tools(mcp, ClientManager(cfg), _expand_tool_groups(entries), None, write_policy=WritePolicy.load())
    return {t.name for t in mcp._tool_manager.list_tools()}


class TestGroups(unittest.TestCase):
    def test_each_group_selects_exactly_its_tools(self):
        for group, expected in EXPECTED.items():
            with self.subTest(group=group):
                self.assertEqual(_tools_for([group]), expected)

    def test_core_is_the_four_core_groups(self):
        core = _tools_for(["core"])
        self.assertEqual(core, EXPECTED["status"] | EXPECTED["problems"] | EXPECTED["metrics"] | EXPECTED["audit"])
        self.assertEqual(len(core), 16)

    def test_groups_combine(self):
        names = _tools_for(["core", "availability", "analytics"])
        self.assertEqual(len(names), 16 + 3 + 3)
        self.assertEqual(
            len(_tools_for(["core", "availability", "notifications", "analytics", "inventory", "problems_raw"])), 30)

    def test_groups_do_not_overlap(self):
        seen: dict[str, str] = {}
        for group, tools in EXPECTED.items():
            for tool in tools:
                self.assertNotIn(tool, seen, f"{tool} is in both {seen.get(tool)} and {group}")
                seen[tool] = group

    def test_no_group_brings_a_write_tool_with_the_default_list(self):
        write = set(write_tool_names())
        for group in TASK_GROUPS:
            with self.subTest(group=group):
                self.assertFalse(_tools_for([group]) & write)

    def test_nesting_and_cycles(self):
        self.assertEqual(_expand_tool_groups(["audit"]), ["auditlog"])
        with patch.dict(TOOL_GROUPS, {"loop_a": ["loop_b"], "loop_b": ["loop_a"]}):
            with self.assertRaises(ValueError):
                _expand_tool_groups(["loop_a"])

    def test_old_groups_still_work(self):
        self.assertIn("host", _expand_tool_groups(["monitoring"]))
        self.assertIn("graph_render", _expand_tool_groups(["extensions"]))

    def test_environment_selects_groups(self):
        with patch.dict(os.environ, {**ENV, "ZABBIX_MCP_SERVER__TOOLS": "core, availability"}, clear=True):
            cfg = load_config()
        self.assertIn("health_check", cfg.server.tools)
        self.assertIn("sla", cfg.server.tools)
        self.assertNotIn("core", cfg.server.tools, "group names are expanded at load time")


class TestUnknownEntries(unittest.TestCase):
    def test_a_misspelt_entry_is_reported(self):
        cfg = AppConfig(server=ServerConfig(tools=["host", "hostt", "host_status"]),
                        zabbix_servers={"a": ZabbixServerConfig(name="a", url="http://a", api_token="t")})
        with self.assertLogs("zabbix_mcp.server", level="WARNING") as logs:
            _warn_unknown_tool_entries(cfg)
        text = "\n".join(logs.output)
        self.assertIn("hostt", text)
        self.assertIn("host_status", text)      # an extension tool needs the full name
        self.assertNotIn(" host,", text)

    def test_valid_entries_are_silent(self):
        cfg = AppConfig(server=ServerConfig(tools=_expand_tool_groups(["core"])),
                        zabbix_servers={"a": ZabbixServerConfig(name="a", url="http://a", api_token="t")})
        with self.assertNoLogs("zabbix_mcp.server", level="WARNING"):
            _warn_unknown_tool_entries(cfg)


class TestRawJsonSchema(unittest.TestCase):
    def _tools(self):
        cfg = AppConfig(zabbix_servers={"a": ZabbixServerConfig(name="a", url="http://a", api_token="t")})
        mcp = MCPServer(name="t")
        _register_tools(mcp, ClientManager(cfg), None, None, write_policy=WritePolicy.load())
        return mcp._tool_manager.list_tools()

    def test_strip_removes_the_parameter_without_touching_the_originals(self):
        from mcp.types import Tool
        tools = [Tool(name=t.name, description=t.description, input_schema=t.parameters) for t in self._tools()]
        self.assertTrue(all("raw_json" in t.input_schema["properties"] for t in tools if t.name != "health_check"))
        stripped = _strip_raw_json(tools)
        self.assertFalse([t.name for t in stripped if "raw_json" in t.input_schema.get("properties", {})])
        self.assertTrue(any("raw_json" in t.input_schema["properties"] for t in tools), "originals are shared and stay intact")
        self.assertEqual([t.name for t in stripped], [t.name for t in tools])


if __name__ == "__main__":
    unittest.main()
