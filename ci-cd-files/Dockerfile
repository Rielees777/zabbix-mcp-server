FROM registry.sovcombank.group/devnetops-mcp/uss-rusca-db:8.0

WORKDIR /app

COPY ./ /app

# Trust the Russian root CA in the system store: the Zabbix servers' certificates may be
# issued under it, and the server verifies TLS through the system store.
COPY ci-cd-files/Russian_Trusted_Root_CA.crt /usr/local/share/ca-certificates/Russian_Trusted_Root_CA.crt
RUN update-ca-certificates --fresh

ENV PIP_INDEX_URL=https://nexus.sovcombank.group/repository/pypi-proxy/simple \
    PIP_TRUSTED_HOST=nexus.sovcombank.group \
    PYTHONDONTWRITEBYTECODE=1
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --no-deps .

USER nobody

CMD ["zabbix-mcp-server"]
