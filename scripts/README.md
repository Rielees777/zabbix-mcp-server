# Helper scripts

Developer tools for the get-method contracts. Nothing here is imported by the server.

## `build_get_contracts.py` / `audit_get_contracts_live.py` - get-method contracts

`src/zabbix_mcp/api/get_contracts.json` (what every `*_get` tool offers in
`output`, `filter`, `search`, `sortfield` and `extra_params`, and what the
server refuses before the call) is generated, never edited by hand:

```bash
# merge the per-page catalogs collected from the API reference (5.0 ... 8.0)
# and the live observations into the shipped file
python3 scripts/build_get_contracts.py scripts/get_contracts_src/catalog_*.json \
  --observed scripts/get_contracts_src/observed_*.json

# check the catalog against a real server (every property, sort field and
# select of every method, plus a filter / search probe per property);
# writes get_contracts_observed_<version>.json into the current directory
python3 scripts/audit_get_contracts_live.py https://zabbix.example.com <api-token>
python3 scripts/audit_get_contracts_live.py https://zabbix.example.com Admin <password>
```

The audit ends with `0 problems` on every release the catalog claims to
know; run it against each one before shipping a catalog change, then
re-run the builder with the fresh `observed_*.json` files so what a server
returns beyond the documentation (and what it refuses or ignores) is
marked in the shipped file. Releases before 6.4 are logged in with the
body `auth` field automatically.

## Pre-release ritual (recap)

Both runners must report green before tagging a release. Plus the OS
matrix:

```bash
cd tests/installer && ./run_all.sh   # 18 distros, ~30 min on first run
```

See `CONTRIBUTING.md` and the `feedback_release_checklist` memory for the
full checklist.
