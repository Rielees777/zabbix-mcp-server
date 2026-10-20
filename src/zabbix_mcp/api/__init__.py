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

"""Aggregates all Zabbix API method definitions into a single registry."""

from zabbix_mcp.api.monitoring import MONITORING_METHODS
from zabbix_mcp.api.data_collection import DATA_COLLECTION_METHODS
from zabbix_mcp.api.alerts import ALERTS_METHODS
from zabbix_mcp.api.users import USERS_METHODS
from zabbix_mcp.api.administration import ADMINISTRATION_METHODS
from zabbix_mcp.api.types import MethodDef

ALL_METHODS: list[MethodDef] = [
    *MONITORING_METHODS,
    *DATA_COLLECTION_METHODS,
    *ALERTS_METHODS,
    *USERS_METHODS,
    *ADMINISTRATION_METHODS,
]

# Zabbix API objects that hold user data or user access. No tool is defined
# for them and ``zabbix_raw_api_call`` refuses them, so the MCP server can
# neither read nor change users, user groups, directories, roles, tokens or
# MFA methods in Zabbix.
BLOCKED_API_OBJECTS: frozenset[str] = frozenset({
    "user", "usergroup", "userdirectory", "role", "token", "mfa",
})


def is_blocked_api_method(method: str) -> bool:
    """True when *method* (``object.action``) belongs to a blocked API object."""
    return method.strip().split(".", 1)[0].strip().lower() in BLOCKED_API_OBJECTS
