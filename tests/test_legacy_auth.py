#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
# Licensed under the GNU Affero General Public License v3.
# See LICENSE for details.
#

"""Legacy password login for Zabbix 5.0 / 5.2 (#77, PR #78 by @rspadim).

Those releases have no API tokens. Password login is therefore allowed,
but only behind an explicit `legacy_auth=true`, never as a fallback,
never next to a token, and never against a Zabbix that has tokens
(5.4+). The tests pin each of those edges because each one is a way the
"workaround" could quietly become the normal path.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from zabbix_mcp.client import ClientManager
from zabbix_mcp.config import AppConfig, ConfigError, ZabbixServerConfig, load_config


def _load(**fields: str) -> AppConfig:
    """Load one Zabbix server named ``old`` from environment variables."""
    env = {"ZABBIX_MCP_ZABBIX_SERVERS__OLD__URL": "http://zabbix5.example.com"}
    env.update({f"ZABBIX_MCP_ZABBIX_SERVERS__OLD__{k.upper()}": v for k, v in fields.items()})
    with patch.dict(os.environ, env, clear=True):
        return load_config()


class TestConfigRules(unittest.TestCase):

    def test_legacy_entry_is_valid(self):
        cfg = _load(legacy_auth="true", username="mcp", password="secret")
        srv = cfg.zabbix_servers["old"]
        self.assertTrue(srv.legacy_auth)
        self.assertEqual((srv.username, srv.password.get_secret_value()), ("mcp", "secret"))
        self.assertEqual(srv.api_token.get_secret_value(), "", "a legacy entry carries no token")

    def test_password_without_the_flag_is_refused(self):
        # The point of the flag: password login is never a silent fallback.
        with self.assertRaises(ConfigError) as ctx:
            _load(username="mcp", password="secret")
        self.assertIn("legacy_auth", str(ctx.exception))

    def test_flag_together_with_a_token_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            _load(legacy_auth="true", api_token="tok", username="mcp", password="secret")
        self.assertIn("pick one", str(ctx.exception))

    def test_flag_without_credentials_is_refused(self):
        for partial in ({"username": "mcp"}, {"password": "secret"}, {}):
            with self.subTest(partial=partial):
                with self.assertRaises(ConfigError):
                    _load(legacy_auth="true", **partial)

    def test_non_boolean_flag_is_refused(self):
        with self.assertRaises(ConfigError):
            _load(legacy_auth="maybe", username="mcp", password="secret")

    def test_blank_password_is_refused(self):
        with self.assertRaises(ConfigError):
            _load(legacy_auth="true", username="mcp", password="   ")

    def test_token_path_is_unchanged(self):
        cfg = _load(api_token="tok")
        srv = cfg.zabbix_servers["old"]
        self.assertFalse(srv.legacy_auth)
        self.assertEqual(srv.api_token.get_secret_value(), "tok")

    def test_missing_token_is_still_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            _load()
        self.assertIn("api_token", str(ctx.exception))


class TestClientLogin(unittest.TestCase):

    def _manager(self, **kw) -> ClientManager:
        srv = ZabbixServerConfig(name="t", url="http://zabbix.example.com", **kw)
        return ClientManager(AppConfig(zabbix_servers={"t": srv}))

    def test_legacy_logs_in_with_the_password_on_5_0(self):
        mgr = self._manager(api_token="", legacy_auth=True, username="mcp", password="secret")
        with patch("zabbix_mcp.client.ZabbixAPI") as cls:
            api = cls.return_value
            api.version = 5.0
            api.api_version.return_value = "5.0.47"
            with self.assertLogs("zabbix_mcp.client", level="WARNING") as logs:
                mgr._connect("t")
            api.login.assert_called_once_with(user="mcp", password="secret")
        # The library's >= 6.0 gate is relaxed for this entry only.
        self.assertTrue(cls.call_args.kwargs["skip_version_check"])
        self.assertTrue(any("LEGACY" in m and "security risk" in m for m in logs.output),
                        "the choice must stay visible in the operational log")

    def test_legacy_is_refused_against_a_zabbix_that_has_tokens(self):
        mgr = self._manager(api_token="", legacy_auth=True, username="mcp", password="secret")
        for version in (5.4, 6.0, 7.4, 8.0):
            with self.subTest(version=version):
                with patch("zabbix_mcp.client.ZabbixAPI") as cls:
                    api = cls.return_value
                    api.version = version
                    with self.assertRaises(ValueError) as ctx:
                        mgr._connect("t")
                    api.login.assert_not_called()
                self.assertIn("api_token", str(ctx.exception))

    def test_token_path_keeps_the_version_gate(self):
        mgr = self._manager(api_token="tok")
        with patch("zabbix_mcp.client.ZabbixAPI") as cls:
            api = cls.return_value
            api.version = 7.4
            api.api_version.return_value = "7.4.0"
            mgr._connect("t")
            api.login.assert_called_once_with(token="tok")
        self.assertFalse(cls.call_args.kwargs["skip_version_check"],
                         "token auth must not inherit the relaxed gate")

    def test_token_path_honours_the_operator_gate_setting(self):
        mgr = self._manager(api_token="tok", skip_version_check=True)
        with patch("zabbix_mcp.client.ZabbixAPI") as cls:
            cls.return_value.version = 8.0
            cls.return_value.api_version.return_value = "8.0.0"
            mgr._connect("t")
        self.assertTrue(cls.call_args.kwargs["skip_version_check"])


if __name__ == "__main__":
    unittest.main()
