#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, version 3.
#

"""Issue #61: [zabbix.X] sections skipped at config load must be
reported with their validation reason, not shown as restart-pending.

A skipped section stays skipped on every boot - presenting it as
"needs restart" sent the operator into an endless restart loop.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from zabbix_mcp.config import load_config
from zabbix_mcp.client import ClientManager


_ENV = {
    "ZABBIX_MCP_SERVER__TRANSPORT": "http",
    "ZABBIX_MCP_SERVER__AUTH_TOKEN": "test-token-0123456789abcdef0123456789abcdef",
    "ZABBIX_MCP_ZABBIX_SERVERS__GOOD__URL": "https://zabbix.example.com",
    "ZABBIX_MCP_ZABBIX_SERVERS__GOOD__API_TOKEN": "dummy",
    "ZABBIX_MCP_ZABBIX_SERVERS__BROKEN__URL": "not-a-valid-url",
    "ZABBIX_MCP_ZABBIX_SERVERS__BROKEN__API_TOKEN": "dummy",
}


def _load(**overrides):
    with patch.dict(os.environ, {**_ENV, **overrides}, clear=True):
        return load_config()


class TestSkippedServers(unittest.TestCase):

    def setUp(self):
        self.config = _load()

    def test_broken_section_skipped_with_reason(self):
        self.assertIn("good", self.config.zabbix_servers)
        self.assertNotIn("broken", self.config.zabbix_servers)
        self.assertIn("broken", self.config.skipped_zabbix_servers)
        self.assertIn("invalid URL", self.config.skipped_zabbix_servers["broken"])

    def test_client_manager_exposes_skipped(self):
        cm = ClientManager(self.config)
        self.assertEqual(cm.server_names, ["good"])
        self.assertIn("broken", cm.skipped_servers)
        self.assertIn("invalid URL", cm.skipped_servers["broken"])

    def test_all_valid_config_has_empty_skips(self):
        cfg = _load(ZABBIX_MCP_ZABBIX_SERVERS__BROKEN__URL="https://other.example.com")
        self.assertEqual(cfg.skipped_zabbix_servers, {})
        self.assertEqual(ClientManager(cfg).skipped_servers, {})


if __name__ == "__main__":
    unittest.main()
