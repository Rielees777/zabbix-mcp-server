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

FROM python:3.13.5-slim AS builder

WORKDIR /build
COPY . .
RUN python -m venv /opt/zabbix-mcp/venv \
    && /opt/zabbix-mcp/venv/bin/pip install --no-cache-dir --quiet ".[reporting]"

FROM python:3.13.5-slim

LABEL maintainer="initMAX s.r.o. <info@initmax.com>"
LABEL org.opencontainers.image.title="Zabbix MCP Server"
LABEL org.opencontainers.image.description="MCP server for the complete Zabbix API"
LABEL org.opencontainers.image.source="https://gitlab.sovcombank.group/devnetops-mcp/zabbix-mcp-server"
LABEL org.opencontainers.image.url="https://gitlab.sovcombank.group/devnetops-mcp/zabbix-mcp-server"
LABEL org.opencontainers.image.documentation="https://gitlab.sovcombank.group/devnetops-mcp/zabbix-mcp-server/-/blob/main/README.md"
LABEL org.opencontainers.image.vendor="initMAX s.r.o."
LABEL org.opencontainers.image.licenses="AGPL-3.0-only"
LABEL org.opencontainers.image.version="2.0"

# System libs for weasyprint PDF rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 \
    libffi8 shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --shell /usr/sbin/nologin --home-dir /opt/zabbix-mcp zabbix-mcp

COPY --from=builder /opt/zabbix-mcp/venv /opt/zabbix-mcp/venv

ENV PATH="/opt/zabbix-mcp/venv/bin:$PATH"

# All settings come from ZABBIX_MCP_* environment variables (see .env.example);
# Vault-rendered files in /vault/secrets are picked up as well.
ENV ZABBIX_MCP_SERVER__TRANSPORT=http \
    ZABBIX_MCP_SERVER__HOST=0.0.0.0 \
    ZABBIX_MCP_SERVER__PORT=8080

USER zabbix-mcp
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"]

ENTRYPOINT ["/opt/zabbix-mcp/venv/bin/zabbix-mcp-server"]
