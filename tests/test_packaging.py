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

"""Build files stay consistent: the image builds the way netbox-mcp's does."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    tomllib = None


def _pins() -> dict[str, str]:
    pins = {}
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, _, version = line.partition("==")
            assert version, f"requirements.txt must pin exact versions: {line!r}"
            pins[name.strip().lower().replace("_", "-")] = version.strip()
    return pins


class TestBuildFiles(unittest.TestCase):
    def test_base_image_lives_in_the_projects_registry_group(self):
        """The 401 on the base image: the Dockerfile pulled from another group's namespace."""
        ci = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
        group = ci["variables"]["GROUP_NAME"]
        first = (ROOT / "ci-cd-files" / "Dockerfile").read_text().splitlines()[0]
        match = re.match(r"FROM registry\.sovcombank\.group/([^/]+)/", first)
        self.assertIsNotNone(match, first)
        self.assertEqual(match.group(1), group)

    def test_ci_does_not_trace_commands(self):
        ci = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
        self.assertEqual(ci["variables"]["DEBUG"], "false")

    def test_the_image_installs_pins_then_the_package_without_deps(self):
        dockerfile = (ROOT / "ci-cd-files" / "Dockerfile").read_text()
        self.assertIn("-r requirements.txt", dockerfile)
        self.assertIn("--no-deps .", dockerfile)
        self.assertIn("Russian_Trusted_Root_CA.crt", dockerfile)
        self.assertTrue((ROOT / "ci-cd-files" / "Russian_Trusted_Root_CA.crt").is_file())

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
    def test_every_dependency_is_pinned_within_its_range(self):
        from packaging.requirements import Requirement

        pins = _pins()
        deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
        for dep in deps:
            req = Requirement(dep)
            name = req.name.lower().replace("_", "-")
            with self.subTest(dependency=dep):
                self.assertIn(name, pins, f"{name} is missing from requirements.txt")
                self.assertTrue(req.specifier.contains(pins[name]),
                                f"{name}=={pins[name]} is outside {req.specifier}")
        self.assertEqual(set(pins), {Requirement(d).name.lower().replace("_", "-") for d in deps},
                         "requirements.txt and pyproject.toml list different packages")

    def test_helm_values_for_both_stands(self):
        for stand in ("prod", "test"):
            with self.subTest(stand=stand):
                values = yaml.safe_load((ROOT / "helm" / f"values-{stand}.yaml").read_text())
                env = values["env"]
                self.assertEqual(env["ZABBIX_MCP_SERVER__AUTH_MODE"], "per_request_token")
                self.assertEqual(env["ZABBIX_MCP_SERVER__TRANSPORT"], "http")
                port = int(env["ZABBIX_MCP_SERVER__PORT"])
                self.assertEqual(values["containers"]["zabbix-mcp-server"]["ports"][0]["containerPort"], port)
                self.assertEqual(values["service"]["zabbix-mcp-server"]["ports"][0]["servicePort"], port)
                for key, value in env.items():
                    self.assertNotRegex(key, r"API_TOKEN|PASSWORD|AUTH_TOKEN$", "no secrets in Helm values")
                # every server reads the caller's token from its own header
                servers = {k.split("__")[1] for k in env if k.startswith("ZABBIX_MCP_ZABBIX_SERVERS__")}
                for name in servers:
                    self.assertIn(f"ZABBIX_MCP_ZABBIX_SERVERS__{name}__TOKEN_HEADER", env)

    def test_helm_settings_load(self):
        """The prod values, used as the environment, are a valid configuration."""
        import os
        from unittest.mock import patch

        from zabbix_mcp.config import load_config

        env = {k: str(v) for k, v in yaml.safe_load((ROOT / "helm" / "values-prod.yaml").read_text())["env"].items()
               if k.startswith("ZABBIX_MCP_")}
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertEqual(sorted(cfg.zabbix_servers), ["ut", "uts"])
        self.assertEqual(cfg.server.auth_mode, "per_request_token")


if __name__ == "__main__":
    unittest.main()
