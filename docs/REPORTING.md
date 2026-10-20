# PDF Reporting Guide

> **Status: BETA**
>
> Server-side PDF reporting was introduced in v1.16. The 4 built-in templates are stable, but the set of context variables and the template inventory are still evolving. Feedback (what's missing, what's confusing, what should be configurable) is very welcome at [issues](https://github.com/initMAX/zabbix-mcp-server/issues).
>
> **Why server-side reports?** LLMs cannot reliably produce consistent, well-formatted reports on their own. Templates make the output deterministic: the model picks the report type and parameters, the server fetches data from Zabbix, fills a Jinja2 template, and renders it to PDF. Same input -> same output, every time.

## Contents

- [Architecture](#architecture)
- [Built-in templates](#built-in-templates)
- [The `report_generate` MCP tool](#the-report_generate-mcp-tool)
- [Branding](#branding)
- [Templates](#templates)
  - [Adding a template to the package](#adding-a-template-to-the-package)
  - [Available context variables](#available-context-variables)
  - [CSS classes provided by `base.html`](#css-classes-provided-by-basehtml)
  - [Worked example: simple problems summary](#worked-example-simple-problems-summary)
- [Limitations and roadmap](#limitations-and-roadmap)

---

## Architecture

```
LLM call -> report_generate tool
              |
              v
       data_fetcher.fetch_<type>_data()    <- queries Zabbix via JSON-RPC
              |
              v
       ReportEngine.generate_report()      <- merges with branding context
              |
              v
       Jinja2 template (HTML)              <- /src/zabbix_mcp/reporting/templates/
              v
       WeasyPrint -> PDF bytes
              |
              v
       base64 data URI returned to client
```

Key files in the source tree:

| File | Purpose |
|---|---|
| `src/zabbix_mcp/reporting/engine.py` | `ReportEngine` class, template registry, branding logic |
| `src/zabbix_mcp/reporting/data_fetcher.py` | One `fetch_<type>_data()` per report type |
| `src/zabbix_mcp/reporting/templates/base.html` | Common layout, CSS, header/footer, page numbering |
| `src/zabbix_mcp/reporting/templates/*.html` | One Jinja2 template per built-in report type |

Required Python packages: `jinja2`, `weasyprint`. Install with the optional extra:

```bash
pip install zabbix-mcp-server[reporting]
```

If either dependency is missing, the `report_generate` tool is not registered and a log line `PDF reporting disabled (install 'weasyprint' and 'jinja2' to enable)` is emitted at startup.

---

## Built-in templates

| Type | Description | Zabbix API methods used |
|---|---|---|
| `availability` | Host availability over a period: SLA gauge, total events, per-host availability table | `host.get`, `event.get` (problem + recovery) |
| `capacity_host` | CPU / memory / disk usage (avg, min, max) per host with colored bars | `host.get`, `item.get`, `trend.get` |
| `capacity_network` | Network bandwidth per interface (Mbit/s, derived from `net.if.in/out`) plus per-host CPU stats | `host.get`, `item.get`, `trend.get` |
| `backup` | Daily backup status matrix (hosts x days) - auto-detects backup item keys (`veeam`, `bacula`, `borg`, `restic`, `backup`); falls back to backup-named triggers | `host.get`, `item.get`, `history.get`, `trigger.get`, `event.get` |

### What each template needs from `data_fetcher`

The fetcher functions return a Python dict that becomes the Jinja2 context. Each fetcher accepts a `params` dict with `hostgroupid` (or `hostids`) plus `period_from` / `period_to` epoch timestamps. The `report_generate` tool builds these from the high-level `period` argument (`"7d"`, `"30d"`, `"90d"`).

---

## The `report_generate` MCP tool

```
report_generate(
    report_type: str,           # "availability" | "capacity_host" | "capacity_network" | "backup"
    hostgroupid: str,           # Zabbix host group ID
    period: str = "30d",        # "<int><d|h|m>" - days, hours, or minutes
    company: str | None = None, # overrides report_company from config
    server: str | None = None,  # multi-server: target Zabbix instance
)
```

**Returns** a JSON document:

```json
{
  "report": "data:application/pdf;base64,JVBERi0xLj...",
  "report_type": "availability",
  "pages": 4,
  "size_kb": 38.2
}
```

The base64 payload can be saved as a `.pdf` file by the client. Most LLM frontends will offer a download link automatically when they see a PDF data URI in the tool output.

---

## Branding

Three optional environment variables control the look of the report header:

```bash
ZABBIX_MCP_SERVER__REPORT_LOGO=/etc/zabbix-mcp/logo.png      # PNG, JPG, JPEG, or SVG
ZABBIX_MCP_SERVER__REPORT_COMPANY="ACME Corp"                # appears in <h1>: "ACME Corp / Availability Report"
ZABBIX_MCP_SERVER__REPORT_SUBTITLE="IT Monitoring Service"   # small subtitle in the header
```

In a container, mount the logo file (a ConfigMap or an image layer) and point `REPORT_LOGO` at it.

**Logo handling:** the engine reads the file at startup, base64-encodes it, and embeds it as a data URI - so the file path does not need to be readable by WeasyPrint at render time. Symlinks are rejected (TOCTOU protection) and the extension allowlist is `.png`, `.jpg`, `.jpeg`, `.svg`.

The `company` value can be overridden per-call by passing the `company` argument to `report_generate`.

---

## Templates

Reports are rendered from the HTML templates shipped in `src/zabbix_mcp/reporting/templates/`. Templates are no longer uploaded or registered at run time (that needed the removed admin portal and a writable config file); to change or add one, edit the package.

### Adding a template to the package

1. Create `src/zabbix_mcp/reporting/templates/<name>.html` extending `base.html`.
2. Register it in `_REPORT_TEMPLATES` in `src/zabbix_mcp/reporting/engine.py`.
3. A template reuses the existing **data fetchers**: the fetcher is selected by the `report_type` you pass to `report_generate`. If the template needs different data, add a `fetch_<type>_data()` to `data_fetcher.py` as well.
4. Rebuild the image / reinstall the package.

### Available context variables

Every template (built-in and custom) receives a common context plus the per-report fetcher output.

**Common context (always present):**

| Variable | Type | Description |
|---|---|---|
| `company` | str | From `report_company` config or `company` tool argument |
| `subtitle` | str | From `report_subtitle` config |
| `logo_base64` | str or None | Data URI for the configured logo, or None if not set |
| `generated_at` | str | Render timestamp, formatted `YYYY-MM-DD HH:MM UTC` |
| `page_label` | str | Localizable "Page" label used in the footer |

**Per-report context:**

`availability`:
| Variable | Type | Description |
|---|---|---|
| `hosts` | list of `{name, event_count, availability_pct}` | Per-host availability rows |
| `total_events` | int | Total number of problem events in the period |
| `availability_pct` | float | Average availability across all hosts |
| `gauge_arc_path` | str | Pre-computed SVG arc path for the gauge |
| `period_from` / `period_to` | str | Human-readable period boundaries |
| `service_hours` | str | From `params.service_hours` (default `"24x7"`) |
| `operational_hours` | str | From `params.operational_hours` (default `"24x7"`) |

`capacity_host`:
| Variable | Type | Description |
|---|---|---|
| `hosts` | list | Resolved Zabbix host objects |
| `metrics` | list of `{label, rows}` | One entry per metric (CPU, Memory, Disk); each `rows` is a list of `{endpoint, avg, min, max}` |
| `period_from` / `period_to` | str | Human-readable period boundaries |

`capacity_network`:
| Variable | Type | Description |
|---|---|---|
| `hosts` | list of `{name, interfaces}` | Per-host interface list; each interface is `{name, bandwidth_mbps, cpu_avg, cpu_min, cpu_max}` |
| `cpu_rows` | list of `{endpoint, avg, min, max}` | Per-host CPU summary |
| `period_from` / `period_to` | str | Human-readable period boundaries |

`backup`:
| Variable | Type | Description |
|---|---|---|
| `backup_matrix` | list of `{host, statuses}` | `statuses` is a `{day_number: bool}` map (True = success) |
| `days` | list of int | Sorted day numbers covered by the period |
| `period_label` | str | From `params.period_label`, e.g. `"March 2026"` |

### CSS classes provided by `base.html`

If you `{% extends "base.html" %}`, the following classes are pre-styled:

| Class | Use |
|---|---|
| `.info-table` | Two-column key/value tables (gray header column) |
| `.metric-box` / `.metric-value` / `.metric-label` | Big-number summary tiles |
| `.gauge-container` | Wrapper for an SVG gauge (centered, margin) |
| `.bar` / `.bar-fill.green` / `.bar-fill.yellow` / `.bar-fill.red` | Inline progress bars; pick the color via a Jinja2 expression |
| `.check` / `.cross` | Green check / red cross glyphs |
| `.page-break` | Force a page break before the element |

The default font is Helvetica Neue / Arial 10pt, table headers use the initMAX red (`#d32f2f`), and pagination ("Page N/M") + `generated_at` are rendered automatically in the page footer via WeasyPrint `@page` rules.

### Worked example: simple problems summary

A minimal custom template that lists problem counts per host. It reuses the `availability` fetcher (so `report_type=availability` when calling), but only renders a subset of the data:

```html
{% extends "base.html" %}

{% block content %}
<h1>{{ company }} / Problems Summary</h1>

<table class="info-table">
    <tr><th>Period</th><td>{{ period_from }} - {{ period_to }}</td></tr>
    <tr><th>Total events</th><td>{{ total_events }}</td></tr>
    <tr><th>Hosts checked</th><td>{{ hosts | length }}</td></tr>
</table>

<h2>Top noisy hosts</h2>
<table>
    <thead>
        <tr><th>Host</th><th>Events</th><th>Availability</th></tr>
    </thead>
    <tbody>
        {% for h in hosts | sort(attribute='event_count', reverse=true) %}
        <tr>
            <td>{{ h.name }}</td>
            <td>{{ h.event_count }}</td>
            <td>
                <div class="bar" style="width: 200px;">
                    <div class="bar-fill {% if h.availability_pct > 99 %}green{% elif h.availability_pct > 95 %}yellow{% else %}red{% endif %}"
                         style="width: {{ h.availability_pct }}%;"></div>
                </div>
            </td>
        </tr>
        {% endfor %}
    </tbody>
</table>
{% endblock %}
```

Save it as `src/zabbix_mcp/reporting/templates/problems_summary.html` and register it as described above.

---

## Limitations and roadmap

**Current limitations:**

- Templates and data fetchers are part of the package; they cannot be added at run time.
- The `period` argument is limited to a single suffix (`"30d"`, `"7d"`, `"24h"`); explicit `period_from` / `period_to` cannot be passed from the LLM.
- The `backup` fetcher uses heuristic key/trigger matching - works for common backup tools but may need a `backup_item_key` override for unusual setups (currently only settable from a Python call, not the MCP tool).
- Internationalization is limited - templates are English-only and date formats are fixed UTC.

**Planned improvements:**

- Pluggable templates and data fetchers (a Python entry point).
- Optional caching of fetched data so subsequent renders are cheap.
- Localized date/time formatting and translatable labels.
- Built-in template for SLA reports based on Zabbix Services / SLA API.

If your use case is not covered by the built-ins, please open an issue describing what report you need and what data should be in it - this is exactly the feedback that drives the next iteration.
