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

"""Settings for the Zabbix MCP server, read from the environment.

Everything is configured through ``ZABBIX_MCP_*`` environment variables
(and an optional ``.env`` file for local runs). Nested sections use a
double underscore::

    ZABBIX_MCP_SERVER__TRANSPORT=http
    ZABBIX_MCP_SERVER__AUTH_TOKEN=...
    ZABBIX_MCP_ZABBIX_SERVERS__PROD__URL=https://zabbix.example.com
    ZABBIX_MCP_ZABBIX_SERVERS__PROD__API_TOKEN=...
    ZABBIX_MCP_REPORTING__EMAIL__SMTP_PASSWORD=...

In Kubernetes the values come from Vault. Either let Vault Agent / the
Vault Secrets Operator / External Secrets expose them as environment
variables, or mount them as files: every file in the secrets directory
(``ZABBIX_MCP_SECRETS_DIR``, ``/vault/secrets`` when it exists) is read
as one setting, named like the variable in lower case, e.g.
``zabbix_mcp_zabbix_servers__prod__api_token``. Real environment
variables win over files.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, EnvSettingsSource, NoDecode, SettingsConfigDict

logger = logging.getLogger("zabbix_mcp.config")

ENV_PREFIX = "ZABBIX_MCP_"
DEFAULT_SECRETS_DIR = "/vault/secrets"


class ConfigError(Exception):
    """Raised when configuration is invalid."""


def _csv(value: Any) -> Any:
    """Accept ``a,b,c`` or a JSON array for list-valued environment variables."""
    if value is None or isinstance(value, (list, tuple)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.startswith("["):
            import json
            return json.loads(text)
        return [part.strip() for part in text.split(",") if part.strip()]
    return value


# A list read from one environment variable (comma separated or JSON).
EnvList = Annotated[list[str] | None, NoDecode]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ZabbixServerConfig(_Frozen):
    """Configuration for a single Zabbix server."""

    name: str
    url: str
    api_token: SecretStr = SecretStr("")
    read_only: bool = True
    verify_ssl: bool = True
    skip_version_check: bool = False
    # Zone for ``<field>_local`` renderings and for date-times the caller
    # sends without a zone. IANA name, "UTC" or "system". Empty = the zone
    # of the Zabbix user behind the API token, then the Zabbix frontend
    # default, then this host's zone. Wins over ``server.timezone``.
    timezone: str = ""
    # Legacy password login for Zabbix 5.0-5.2, which have no API tokens.
    # Off by default and only honoured when ``legacy_auth`` is set: the
    # operator has to say out loud that they are pointing an AI at a
    # Zabbix release that is out of upstream support, with a password
    # rather than a revocable token. Mutually exclusive with ``api_token``.
    # The client additionally refuses this path against any Zabbix >= 5.4.
    legacy_auth: bool = False
    username: str = ""
    password: SecretStr = SecretStr("")
    # Optional username + password used by ``graph_render`` to acquire a
    # Zabbix frontend session cookie when the API token alone is rejected
    # by ``/chart2.php`` (Zabbix 6.0+ frontend uses signed session
    # cookies, which only ``user.login`` can produce).
    frontend_username: str = ""
    frontend_password: SecretStr = SecretStr("")
    # Request timeout (seconds). Default 300 s matches the Zabbix PHP
    # frontend's max_execution_time.
    request_timeout: int = Field(default=300, gt=0)

    @field_validator("url")
    @classmethod
    def _check_url(cls, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"invalid URL '{url}'. Must start with http:// or https://")
        try:
            parsed = urlparse(url)
        except ValueError as exc:
            raise ValueError(f"URL '{url}' could not be parsed: {exc}") from exc
        host = parsed.hostname
        if not host:
            raise ValueError(f"URL '{url}' has no hostname")
        # DNS is not resolved here - the Zabbix host may legitimately be
        # down at boot - but malformed names are caught early.
        try:
            ip_address(host)
        except ValueError:
            import re
            label = r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
            valid = (
                0 < len(host) <= 253
                and re.fullmatch(rf"{label}(\.{label})*", host)
                and not re.fullmatch(r"[0-9.]+", host)
            )
            if not valid:
                raise ValueError(
                    f"URL '{url}' has an invalid hostname '{host}'. Use a DNS name "
                    f"(e.g. zabbix.example.com) or a valid IPv4/IPv6 address."
                ) from None
        return url.rstrip("/")

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        return _validate_timezone_setting(value)

    @model_validator(mode="after")
    def _check_credentials(self) -> "ZabbixServerConfig":
        token = self.api_token.get_secret_value().strip()
        user = self.username.strip()
        password = self.password.get_secret_value().strip()
        if self.legacy_auth:
            # Zabbix 5.0-5.2: password login, and only that. A token on the
            # same entry would leave it ambiguous which credential is live.
            if token:
                raise ValueError(
                    "sets both legacy_auth=true and api_token. Legacy password login "
                    "is for Zabbix 5.0-5.2 only, which has no API tokens - pick one."
                )
            if not user or not password:
                raise ValueError("legacy_auth=true needs both 'username' and 'password'")
        else:
            # A username/password pair without the explicit flag is refused
            # rather than quietly used: password auth is never a silent fallback.
            if self.username or self.password.get_secret_value():
                raise ValueError(
                    "has 'username'/'password' but not legacy_auth=true. Password login "
                    "is only for Zabbix 5.0-5.2 (no API tokens there) and must be "
                    "enabled explicitly; on Zabbix 5.4+ use 'api_token'."
                )
            if not token:
                raise ValueError("is missing 'api_token'")
        return self


class ServerConfig(_Frozen):
    """MCP server configuration."""

    transport: Literal["stdio", "http", "sse"] = "stdio"
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    log_file: str | None = None
    # Static bearer token the MCP clients must send. It is meant to be
    # short-lived: rotate it in Vault and restart the pods, or set
    # ``auth_token_expires_at`` so the server stops accepting it by itself.
    auth_token: SecretStr | None = None
    auth_token_expires_at: datetime | None = None
    rate_limit: int = 300
    tools: EnvList = None
    disabled_tools: EnvList = None
    tls_cert_file: str | None = None
    tls_key_file: str | None = None
    # External URL clients use to reach this server, advertised in the
    # auth metadata instead of "{scheme}://{host}:{port}". Required when
    # host is "0.0.0.0" / "::" behind a proxy or DNS name.
    public_url: str = ""
    cors_origins: EnvList = None
    allowed_import_dirs: EnvList = None
    allowed_hosts: EnvList = None
    # Explicit Origin header allowlist for DNS rebinding protection. When
    # unset and ``public_url`` is configured, the origin derived from it
    # is used. Wildcard ports work as ``http://example.com:*``.
    allowed_origins: EnvList = None
    # IPs of reverse proxies whose X-Forwarded-For / Forwarded headers we
    # trust for client-IP attribution. Empty means only the raw TCP peer.
    trusted_proxies: EnvList = None
    compact_output: bool = True
    validate_get_params: bool = True
    response_max_chars: int = Field(default=50000, ge=5000)
    # Freshness hint (seconds) attached to tools/list results as the
    # 2026-07-28 CacheableResult ``ttlMs``. 0 disables client caching.
    tools_list_cache_ttl: int = Field(default=300, ge=0, le=86400)
    report_logo: str | None = None
    report_company: str = ""
    report_subtitle: str = "IT Monitoring Service"
    # Over stdio there is no bearer-token context, so ``raw_json=true`` is
    # rejected by default; operators driving the process from a non-LLM
    # script can opt in. HTTP transport uses ``allow_raw_json`` instead.
    stdio_allow_raw_json: bool = False
    # HTTP only: let callers use ``raw_json=true`` (strips the
    # prompt-injection preamble). Off by default - it is a defence for LLM clients.
    allow_raw_json: bool = False
    # Default zone for every Zabbix entry that has none of its own.
    timezone: str = ""
    # File listing the tools that may change data in Zabbix and are blocked
    # (see zabbix_mcp.write_policy). None = the list built into the package.
    blocked_tools_file: str | None = None

    @field_validator(
        "tools", "disabled_tools", "cors_origins", "allowed_import_dirs",
        "allowed_hosts", "allowed_origins", "trusted_proxies",
        mode="before",
    )
    @classmethod
    def _split(cls, value: Any) -> Any:
        return _csv(value)

    @field_validator("log_level", mode="before")
    @classmethod
    def _lower_level(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value

    @field_validator("tools", "disabled_tools")
    @classmethod
    def _expand(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _expand_tool_groups(value)

    @field_validator("auth_token", mode="before")
    @classmethod
    def _blank_token(cls, value: Any) -> Any:
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        return value if isinstance(value, str) and value.strip() else None

    @field_validator("auth_token_expires_at", mode="before")
    @classmethod
    def _expiry(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        from zabbix_mcp.auth import parse_expiry
        try:
            return parse_expiry(value) if isinstance(value, str) else value
        except ValueError as exc:
            raise ValueError(f"auth_token_expires_at: {exc}") from exc

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        return _validate_timezone_setting(value)

    @field_validator("allowed_origins")
    @classmethod
    def _check_origins(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for entry in (e.strip() for e in value):
            if not entry:
                continue
            if not entry.startswith(("http://", "https://")):
                raise ValueError(f"entry '{entry}' must start with http:// or https://")
            # ``:*`` is a port wildcard of the MCP SDK that urlsplit rejects.
            probe = entry[:-2] if entry.endswith(":*") else entry
            try:
                parts = urlsplit(probe)
            except ValueError as exc:
                raise ValueError(f"entry '{entry}' is not a valid URL: {exc}") from exc
            if not parts.hostname:
                raise ValueError(f"entry '{entry}' is missing a host")
            if parts.path not in ("", "/"):
                raise ValueError(
                    f"entry '{entry}' must not include a path - drop everything after host[:port]"
                )
            if parts.query or parts.fragment:
                raise ValueError(f"entry '{entry}' must not include query / fragment")
            cleaned.append(entry)
        return cleaned or None

    @model_validator(mode="after")
    def _check_tls_and_public_url(self) -> "ServerConfig":
        if self.tls_cert_file and not self.tls_key_file:
            raise ValueError("tls_key_file is required when tls_cert_file is set")
        if self.tls_key_file and not self.tls_cert_file:
            raise ValueError("tls_cert_file is required when tls_key_file is set")
        if self.public_url:
            object.__setattr__(
                self, "public_url", _validate_public_url(self.public_url.strip(), self.tls_cert_file)
            )
        return self


class ReportEmailConfig(_Frozen):
    """SMTP settings for mailing generated PDF reports.

    Disabled by default. ``allowed_recipients`` is the safety fence: an AI
    client may ask for a report to be mailed, but only to an address the
    operator listed. Entries may glob a domain, e.g. ``*@example.com``.
    """

    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    from_address: str = ""
    use_starttls: bool = True
    use_ssl: bool = False
    timeout: int = 30
    allowed_recipients: EnvList = Field(default_factory=list)

    @field_validator("allowed_recipients", mode="before")
    @classmethod
    def _split(cls, value: Any) -> Any:
        return _csv(value) or []

    @model_validator(mode="after")
    def _check_enabled(self) -> "ReportEmailConfig":
        if self.enabled:
            if not self.smtp_host:
                raise ValueError("smtp_host is required when email is enabled")
            if not self.from_address:
                raise ValueError("from_address is required when email is enabled")
            if not self.allowed_recipients:
                raise ValueError(
                    "allowed_recipients must list at least one address or pattern when "
                    "email is enabled - without it an AI client could mail monitoring "
                    "data anywhere. Use e.g. [\"ops@example.com\"] or [\"*@example.com\"]."
                )
        return self


class ReportingConfig(_Frozen):
    """Out-of-band delivery for generated reports.

    A full PDF returned inline as base64 can exceed an LLM's context window
    or a proxy's read timeout. With ``output_dir`` set, the report can be
    written to disk instead and the tool returns just the path.
    """

    output_dir: str = ""
    # How long a zabbix://reports/<id> link stays fetchable, in seconds.
    link_ttl: int = Field(default=3600, ge=60, le=86400)
    # Ceiling on concurrently held reports; the oldest is evicted first.
    link_max_reports: int = Field(default=20, ge=1, le=500)
    # Also hand out an https download URL next to the MCP resource link.
    # The URL carries the same random 122-bit id as the resource and
    # expires with it: a capability URL, usable without a bearer token.
    download_urls: bool = True
    email: ReportEmailConfig = Field(default_factory=ReportEmailConfig)

    @field_validator("output_dir")
    @classmethod
    def _check_output_dir(cls, value: str) -> str:
        if value and not os.path.isabs(os.path.expanduser(value)):
            raise ValueError(f"output_dir must be an absolute path (got {value!r})")
        return value


def secrets_dir() -> Path | None:
    """Directory whose files hold settings (Vault Agent / CSI / mounted Secret)."""
    configured = os.environ.get(ENV_PREFIX + "SECRETS_DIR", "").strip()
    if configured:
        return Path(configured)
    default = Path(DEFAULT_SECRETS_DIR)
    return default if default.is_dir() else None


class _SecretFilesSource(EnvSettingsSource):
    """Reads settings from files, one file per variable.

    A file called like the variable in lower case (``zabbix_mcp_zabbix_servers__prod__api_token``)
    is treated exactly like that environment variable, nesting included,
    which pydantic-settings' own ``secrets_dir`` does not do. Trailing
    newlines are stripped. Dot-files are ignored, which skips the
    ``..data`` bookkeeping of mounted Kubernetes Secrets.
    """

    def __init__(self, settings_cls: type[BaseSettings], directory: Path) -> None:
        self._directory = directory
        super().__init__(settings_cls)

    def _load_env_vars(self) -> dict[str, str | None]:
        found: dict[str, str | None] = {}
        try:
            entries = sorted(self._directory.iterdir())
        except OSError as exc:
            logger.warning("Cannot read secrets directory %s: %s", self._directory, exc)
            return found
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_file():
                continue
            try:
                found[entry.name.lower()] = entry.read_text(encoding="utf-8").rstrip("\r\n")
            except OSError as exc:
                logger.warning("Cannot read secret file %s: %s", entry, exc)
        return found


class AppConfig(BaseSettings):
    """Top-level application settings, read from ``ZABBIX_MCP_*`` variables."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    server: ServerConfig = Field(default_factory=ServerConfig)
    zabbix_servers: dict[str, ZabbixServerConfig] = Field(default_factory=dict)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)
    # ``ZABBIX_MCP_ZABBIX_SERVERS__<NAME>__...`` entries that failed
    # validation and were skipped (name -> reason). One broken entry must
    # not take the whole service down at boot.
    skipped_zabbix_servers: dict[str, str] = Field(default_factory=dict)

    # Runtime handles attached by the server after start-up.
    _runtime_port: int | None = PrivateAttr(default=None)
    _task_store: Any = PrivateAttr(default=None)
    _report_store: Any = PrivateAttr(default=None)
    # The parsed blocked-tools file; None (e.g. in tests) = nothing is restricted.
    _write_policy: Any = PrivateAttr(default=None)

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        # Highest priority first: explicit arguments, real environment,
        # then secret files, then a local .env.
        sources = [init_settings, env_settings]
        directory = secrets_dir()
        if directory is not None:
            sources.append(_SecretFilesSource(settings_cls, directory))
        sources.append(dotenv_settings)
        return tuple(sources)

    @model_validator(mode="before")
    @classmethod
    def _validate_servers_one_by_one(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw = data.get("zabbix_servers") or {}
        if not isinstance(raw, dict):
            return data
        good: dict[str, ZabbixServerConfig] = {}
        skipped: dict[str, str] = {}
        for name, srv in raw.items():
            if isinstance(srv, ZabbixServerConfig):
                good[name] = srv
                continue
            try:
                if not isinstance(srv, dict):
                    raise ValueError("is not a table of settings")
                good[name] = ZabbixServerConfig.model_validate({**srv, "name": name})
            except (ValidationError, ValueError) as exc:
                reason = _format_error(exc)
                logger.warning("Skipping Zabbix server '%s' because of config error: %s", name, reason)
                skipped[name] = reason
        return {**data, "zabbix_servers": good, "skipped_zabbix_servers": skipped}

    @property
    def default_server(self) -> str | None:
        """Return the name of the first configured Zabbix server."""
        servers = list(self.zabbix_servers)
        return servers[0] if servers else None


def _format_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors(include_url=False, include_input=False):
            loc = ".".join(str(p) for p in err["loc"])
            msg = err["msg"].removeprefix("Value error, ")
            parts.append(f"{loc}: {msg}" if loc else msg)
        return "; ".join(parts)
    return str(exc)


TOOL_GROUPS: dict[str, list[str]] = {
    "monitoring": [
        "host", "hostgroup", "hostinterface", "hostprototype",
        "item", "itemprototype", "trigger", "triggerprototype",
        "problem", "problem_active_get", "event", "history", "trend",
        "graph", "graphitem", "graphprototype",
        "discoveryrule", "discoveryruleprototype",
        "dcheck", "dhost", "drule", "dservice", "httptest", "sla",
        # Pre-correlated views (one-shot replacements for raw chains)
        "host_status_get", "hostgroup_overview_get",
        "infrastructure_summary_get", "item_history_summary_get",
    ],
    "data_collection": [
        "template", "templategroup", "templatedashboard",
        "valuemap", "dashboard",
    ],
    "alerts": [
        "action", "alert", "mediatype", "script",
    ],
    "users": [
        # User accounts, groups, directories, roles, tokens and MFA are not
        # exposed at all (see zabbix_mcp.api.BLOCKED_API_OBJECTS).
        "usermacro",
    ],
    "administration": [
        "settings", "housekeeping", "authentication", "autoregistration",
        "configuration", "connector", "correlation", "hanode",
        "iconmap", "image", "maintenance", "map", "module",
        "proxy", "proxygroup", "regexp", "report", "task",
        "auditlog",
    ],
    "extensions": [
        "graph_render", "anomaly_detect", "capacity_forecast",
        "item_threshold_search", "problem_active_get",
        "host_status_get", "hostgroup_overview_get",
        "infrastructure_summary_get", "item_history_summary_get",
        "report_generate", "action_prepare", "action_confirm",
        "zabbix_raw_api_call", "health_check",
    ],
}


def _validate_timezone_setting(value: str) -> str:
    """Empty stays empty; anything else must be a zone ``timezones.validate_timezone`` accepts."""
    if not value or not value.strip():
        return ""
    from zabbix_mcp.timezones import validate_timezone
    return validate_timezone(value)


def _validate_public_url(value: str, tls_cert_file: object) -> str:
    """Validate the optional ``public_url`` override.

    Empty is allowed. Non-empty must be a bare http(s) URL (no path, query
    or fragment - ``/mcp`` or ``/sse`` is appended downstream), must not be
    a wildcard bind address, and must be https when the server serves TLS.
    """
    if not value:
        return ""
    try:
        parsed = urlparse(value)
    except ValueError as e:
        raise ValueError(f"'public_url' is not a valid URL: {e}") from e
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"'public_url' must start with http:// or https:// (got '{value}')")
    if not parsed.hostname:
        raise ValueError(f"'public_url' is missing the host part: '{value}'")
    if parsed.hostname in {"0.0.0.0", "::", "[::]"}:
        raise ValueError(
            f"'public_url' cannot be a wildcard bind address ('{parsed.hostname}'); "
            "use the actual public DNS name or IP that clients reach"
        )
    if parsed.path and parsed.path not in {"", "/"}:
        raise ValueError(
            f"'public_url' must be a bare URL with no path ('{parsed.path}' "
            "found); the /mcp or /sse path is appended automatically"
        )
    if parsed.query or parsed.fragment:
        raise ValueError("'public_url' must not contain a query string or fragment")
    if tls_cert_file and parsed.scheme != "https":
        raise ValueError(f"'public_url' must use https:// when tls_cert_file is set (got '{value}')")
    return value.rstrip("/")


def _expand_tool_groups(tools: list[str]) -> list[str]:
    """Expand group names (e.g. 'monitoring') into individual tool prefixes."""
    expanded: list[str] = []
    for entry in tools:
        entry = entry.lower()
        if entry in TOOL_GROUPS:
            expanded.extend(TOOL_GROUPS[entry])
        else:
            expanded.append(entry)
    return list(dict.fromkeys(expanded))  # deduplicate, preserve order


def load_config() -> AppConfig:
    """Read and validate the settings from the environment (and the secrets dir)."""
    try:
        config = AppConfig()
    except ValidationError as exc:
        raise ConfigError(_format_error(exc)) from exc
    from zabbix_mcp.write_policy import PolicyError, WritePolicy
    try:
        config._write_policy = WritePolicy.load(config.server.blocked_tools_file or None)
    except PolicyError as exc:
        raise ConfigError(str(exc)) from exc
    if not config.zabbix_servers:
        if config.skipped_zabbix_servers:
            raise ConfigError(
                "All configured Zabbix servers failed validation: "
                + "; ".join(f"{n}: {e}" for n, e in config.skipped_zabbix_servers.items())
            )
        raise ConfigError(
            "No Zabbix servers configured. Set ZABBIX_MCP_ZABBIX_SERVERS__<NAME>__URL "
            "and ZABBIX_MCP_ZABBIX_SERVERS__<NAME>__API_TOKEN."
        )
    return config
