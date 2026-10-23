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

"""Operator-controlled list of tools that may change data in Zabbix.

The list lives in a plain text file, ``blocked_write_tools.txt`` (shipped in
this package, or any file named by ``ZABBIX_MCP_SERVER__BLOCKED_TOOLS_FILE``).
One tool name per line, ``#`` starts a comment, ``*`` / ``?`` wildcards are
allowed (``*_delete``). Every tool named there is

* not registered, so the model never sees it in ``tools/list``, and
* refused when reached by API method name through ``zabbix_raw_api_call``
  or ``action_prepare`` / ``action_confirm`` (``host.delete`` is blocked when
  ``host_delete`` is listed).

To allow a tool, delete its line (and set ``READ_ONLY=false`` for the Zabbix
server it should write to - the per-server flag stays a second, independent
gate). The shipped file lists every write tool, so nothing can change data
until someone removes lines on purpose.

``python -m zabbix_mcp.write_policy`` prints every write tool the server can
register, as a starting point for a new file.
"""

from __future__ import annotations

import fnmatch
import logging
from importlib import resources
from pathlib import Path

logger = logging.getLogger("zabbix_mcp.write_policy")

DEFAULT_FILE = "blocked_write_tools.txt"

# Hand-written extension tools that can change data (the generated ones are
# flagged on their MethodDef).
EXTENSION_WRITE_TOOLS: tuple[str, ...] = (
    "action_prepare",
    "action_confirm",
    "zabbix_raw_api_call",
)


class PolicyError(Exception):
    """The blocked-tools file cannot be used."""


def write_tool_names() -> list[str]:
    """Every tool this server can register that changes data in Zabbix."""
    from zabbix_mcp.api import ALL_METHODS

    names = {m.tool_name for m in ALL_METHODS if not m.read_only}
    names.update(EXTENSION_WRITE_TOOLS)
    return sorted(names)


def all_tool_names() -> set[str]:
    from zabbix_mcp.api import ALL_METHODS
    from zabbix_mcp.config import TOOL_GROUPS

    return {m.tool_name for m in ALL_METHODS} | set(TOOL_GROUPS["extensions"])


class WritePolicy:
    """The parsed blocked-tools file."""

    def __init__(self, patterns: tuple[str, ...], source: str) -> None:
        self.patterns = patterns
        self.source = source
        self._cache: dict[str, bool] = {}

    @classmethod
    def parse(cls, text: str, source: str = "<string>") -> "WritePolicy":
        patterns: list[str] = []
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if any(ch.isspace() for ch in line):
                raise PolicyError(f"{source}:{lineno}: one tool name per line, got {line!r}")
            patterns.append(line.lower())
        return cls(tuple(dict.fromkeys(patterns)), source)

    @classmethod
    def load(cls, path: str | None = None) -> "WritePolicy":
        """Read *path*, or the file shipped with the package when it is None."""
        try:
            if path:
                source = str(Path(path))
                text = Path(path).read_text(encoding="utf-8")
            else:
                source = f"zabbix_mcp/{DEFAULT_FILE} (built in)"
                text = resources.files("zabbix_mcp").joinpath(DEFAULT_FILE).read_text(encoding="utf-8")
        except OSError as exc:
            # Fail closed: a missing list must stop the start-up, never mean "nothing is blocked".
            raise PolicyError(f"cannot read the blocked-tools file {path or DEFAULT_FILE}: {exc}") from exc
        policy = cls.parse(text, source)
        policy._warn_about_unknown()
        return policy

    def _warn_about_unknown(self) -> None:
        known = all_tool_names()
        for pattern in self.patterns:
            if not any(fnmatch.fnmatchcase(name, pattern) for name in known):
                logger.warning(
                    "%s: '%s' matches no tool (typo? a misspelt line blocks nothing)",
                    self.source, pattern,
                )

    def blocks(self, tool_name: str) -> bool:
        name = tool_name.lower()
        hit = self._cache.get(name)
        if hit is None:
            hit = any(fnmatch.fnmatchcase(name, p) for p in self.patterns)
            self._cache[name] = hit
        return hit

    def blocks_api_method(self, method: str) -> bool:
        """True when *method* (``host.delete``) is the API call of a blocked tool."""
        from zabbix_mcp.api import ALL_METHODS

        wanted = method.strip().lower()
        return any(
            self.blocks(m.tool_name) for m in ALL_METHODS if m.api_method.lower() == wanted
        )

    def allowed_write_tools(self) -> list[str]:
        return [name for name in write_tool_names() if not self.blocks(name)]


def main() -> None:
    print("# Every tool that can change data in Zabbix. One name per line.")
    for name in write_tool_names():
        print(name)


if __name__ == "__main__":
    main()
