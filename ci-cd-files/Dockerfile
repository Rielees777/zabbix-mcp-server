FROM registry.sovcombank.group/devnetops-dc/uss-rusca-db:8.0

WORKDIR /app
COPY ./ /app

# Python 3.10+ is required. Fail the build early with a clear message otherwise.
RUN python -c "import sys; assert sys.version_info >= (3, 10), 'Python 3.10+ required, got ' + sys.version"

# The [dev] extra brings pytest so the pipeline's `test` job can run inside this image.
# PDF reports (the [reporting] extra) are left out: weasyprint needs system libraries
# (Cairo, Pango) that this base image does not ship; report_generate is simply not registered.
RUN pip install ".[dev]" \
    --trusted-host nexus.sovcombank.group \
    --index-url "https://nexus.sovcombank.group/repository/pypi-proxy/simple"

# Run as a network service in Kubernetes. Secrets and Zabbix connection settings
# (ZABBIX_MCP_ZABBIX_SERVERS__<NAME>__URL / __API_TOKEN, ZABBIX_MCP_SERVER__AUTH_TOKEN, ...)
# are NOT baked in: they come from Vault via the pipeline's k8s component as env variables.
ENV PYTHONDONTWRITEBYTECODE=1 \
    ZABBIX_MCP_SERVER__TRANSPORT=http \
    ZABBIX_MCP_SERVER__HOST=0.0.0.0 \
    ZABBIX_MCP_SERVER__PORT=30030

USER nobody
EXPOSE 30030

CMD ["python", "-m", "zabbix_mcp"]
