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

"""Zabbix API definitions for global authentication settings.

User accounts, user groups, user directories, roles and API tokens are
deliberately NOT exposed: no tool reads or changes user data in Zabbix, and
``zabbix_raw_api_call`` refuses those API objects too (see
``BLOCKED_API_OBJECTS`` in ``zabbix_mcp.api``).

- **authentication**: Global authentication settings that control login behaviour,
  password policy, LDAP/SAML defaults, HTTP auth, and MFA requirements.
"""

from zabbix_mcp.api.types import MethodDef
from zabbix_mcp.api.common import SINGLETON_GET_PARAMS, UPDATE_PARAMS

# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------

_AUTHENTICATION_METHODS: list[MethodDef] = [
    MethodDef(
        api_method="authentication.get",
        tool_name="authentication_get",
        description=(
            "Get the global authentication settings. Returns the server-wide "
            "configuration for authentication methods: internal auth, LDAP "
            "defaults, SAML settings, HTTP authentication, password complexity "
            "requirements, login attempt limits, and MFA enforcement. This is a "
            "singleton object -- there are no IDs to filter by."
        ),
        read_only=True,
        params=SINGLETON_GET_PARAMS,
    ),
    MethodDef(
        api_method="authentication.update",
        tool_name="authentication_update",
        description=(
            "Update global authentication settings (LDAP, SAML, MFA, etc.). "
            "Controls server-wide login behaviour such as password minimum length, "
            "account lockout after N failed attempts, default authentication type, "
            "SAML SSO toggle, and MFA enforcement. Changes affect all users. "
            "The params dict should include only the fields to change."
        ),
        read_only=False,
        params=UPDATE_PARAMS,
    ),
]

# ---------------------------------------------------------------------------
# Public export
# ---------------------------------------------------------------------------

USERS_METHODS: list[MethodDef] = _AUTHENTICATION_METHODS
