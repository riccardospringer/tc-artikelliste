from __future__ import annotations

import asyncio
import csv
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from article_list_mvp import canonicalize_url, run_mvp
try:
    import adobe_client as _adobe
    _ADOBE_AVAILABLE = True
except ImportError:
    _adobe = None  # type: ignore[assignment]
    _ADOBE_AVAILABLE = False

try:
    import es_feed_client as _es
    _ES_AVAILABLE = True
except ImportError:
    _es = None  # type: ignore[assignment]
    _ES_AVAILABLE = False

BASE_DIR = Path(__file__).resolve().parents[1]
ADOBE = BASE_DIR / "fixtures" / "adobe_sample.json"
RSS = BASE_DIR / "fixtures" / "rss_sample.xml"
HOME = BASE_DIR / "fixtures" / "home_positions_sample.json"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        parsed = int(raw.strip())
    except ValueError:
        return default
    return max(parsed, minimum)


def _parse_optional_port(raw: str | None) -> int | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    try:
        return max(int(value), 1)
    except ValueError:
        return None


def _resolve_listen_port() -> tuple[int, str]:
    tc_port = _parse_optional_port(os.environ.get("TC_PORT"))
    if tc_port is not None:
        return tc_port, "TC_PORT"

    platform_port = _parse_optional_port(os.environ.get("PORT"))
    if platform_port is not None:
        return platform_port, "PORT"

    return 8099, "default"


def _env_source(name: str, default_path: Path) -> Path | str:
    value = os.environ.get(name, "").strip()
    if not value:
        return default_path
    return value


def _normalize_base_path(path: str) -> str:
    value = path.strip()
    if not value:
        return "/"
    parts = [part for part in value.split("/") if part]
    if not parts:
        return "/"
    return "/" + "/".join(parts)


def _normalize_public_base_url(url: str | None) -> str | None:
    if url is None:
        return None
    value = url.strip()
    if not value:
        return None
    return value.rstrip("/")


def _normalize_request_path(path: str) -> str:
    if not path:
        return "/"
    return _normalize_base_path(path)


def _with_base_path(path: str, *, base_path: str | None = None) -> str:
    target_base_path = BASE_PATH if base_path is None else base_path
    if target_base_path == "/":
        return path
    return f"{target_base_path}{path}"


def _public_url(path: str) -> str | None:
    if PUBLIC_BASE_URL is None:
        return None
    if path == "/":
        return f"{PUBLIC_BASE_URL}/"
    return f"{PUBLIC_BASE_URL}{path}"


def _path_from_request(request_path: str) -> str:
    normalized_path = _normalize_request_path(request_path)
    if BASE_PATH == "/":
        return normalized_path
    if normalized_path == BASE_PATH:
        return "/"
    prefix = f"{BASE_PATH}/"
    if normalized_path.startswith(prefix):
        suffix = normalized_path[len(prefix) :].lstrip("/")
        return f"/{suffix}" if suffix else "/"
    # Unterstützt Reverse-Proxys, die den Prefix bereits vor Weiterleitung entfernen.
    return normalized_path


def _base_path_for_links(request_path: str) -> str:
    if BASE_PATH == "/":
        return "/"
    normalized_path = _normalize_request_path(request_path)
    if normalized_path == BASE_PATH or normalized_path.startswith(f"{BASE_PATH}/"):
        return BASE_PATH
    return "/"


def _safe_int(value: object, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _find_pyc_module(pycache_dir: Path, module_name: str) -> Path | None:
    matches = sorted(pycache_dir.glob(f"{module_name}.cpython-*.pyc"))
    if not matches:
        return None
    return matches[0]


def _load_sourceless_module(module_name: str, module_path: Path):
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing

    loader = importlib.machinery.SourcelessFileLoader(module_name, str(module_path))
    spec = importlib.util.spec_from_loader(module_name, loader)
    if spec is None:
        raise RuntimeError(f"Sourceless-Spec konnte nicht geladen werden: {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loader.exec_module(module)
    return module


def _load_editorial_one_module():
    global _EDITORIAL_ONE_MODULE

    if _EDITORIAL_ONE_MODULE is not None:
        return _EDITORIAL_ONE_MODULE

    with _EDITORIAL_ONE_MODULE_LOCK:
        if _EDITORIAL_ONE_MODULE is not None:
            return _EDITORIAL_ONE_MODULE

        pyc_path = Path(EDITORIAL_ONE_PYC)
        if not pyc_path.is_file():
            raise FileNotFoundError(f"Editorial-One Modul nicht gefunden: {pyc_path}")

        pycache_dir = pyc_path.parent
        for dependency in ("config", "utils", "adobe_client", "db", "schemas", "data_pipeline", "seed_articles"):
            dep_path = _find_pyc_module(pycache_dir, dependency)
            if dep_path is None:
                continue
            _load_sourceless_module(dependency, dep_path)

        _EDITORIAL_ONE_MODULE = _load_sourceless_module("bild_published", pyc_path)
        return _EDITORIAL_ONE_MODULE


def _map_editorial_one_row(row: dict[str, object]) -> dict[str, object] | None:
    url = str(row.get("url") or row.get("source_url") or "").strip()
    canonical_url = canonicalize_url(url)
    if not canonical_url:
        return None

    home_raw = row.get("home_position")
    if home_raw in (None, ""):
        home_raw = row.get("_rank_home")
    home_position = _safe_int(home_raw, default=0)
    if home_position <= 0 or home_position >= 9999:
        home_position_value: int | None = None
    else:
        home_position_value = home_position

    live_readers = _safe_int(row.get("adobe_readers"), default=0)
    if live_readers <= 0:
        live_readers = _safe_int(row.get("live_readers"), default=0)
    if live_readers <= 0:
        live_readers = abs(_safe_int(row.get("_rank_readers"), default=0))

    published = row.get("published") or row.get("published_at")
    published_at: str | None
    if published is None:
        published_at = None
    elif hasattr(published, "isoformat"):
        published_at = published.isoformat()  # type: ignore[union-attr]
    else:
        published_at = str(published)

    source_flags = {"editorial_one", "rss"}
    if home_position_value is not None:
        source_flags.add("home")
    if live_readers > 0:
        source_flags.add("adobe")

    return {
        "canonical_url": canonical_url,
        "source_url": url,
        "title": str(row.get("title") or ""),
        "workflow_status": str(row.get("workflow_status") or ""),
        "live_readers": live_readers,
        "home_position": home_position_value,
        "published_at": published_at,
        "source_flags": sorted(source_flags),
    }


def _load_editorial_one_articles(force_refresh: bool = False) -> list[dict[str, object]]:
    module = _load_editorial_one_module()
    get_fetcher = getattr(module, "get_bild_published_fetcher", None)
    if not callable(get_fetcher):
        raise RuntimeError("Editorial-One Modul liefert keinen get_bild_published_fetcher()")

    fetcher = get_fetcher()
    get_list = getattr(fetcher, "get_published_list", None)
    if not callable(get_list):
        raise RuntimeError("Editorial-One Fetcher liefert keinen get_published_list()")

    async def _fetch() -> list[dict[str, object]]:
        rows = await get_list(
            hours=EDITORIAL_ONE_HOURS,
            limit=EDITORIAL_ONE_LIMIT,
            force_refresh=force_refresh,
        )
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    raw_rows = asyncio.run(_fetch())
    mapped_rows: list[dict[str, object]] = []
    for row in raw_rows:
        mapped = _map_editorial_one_row(row)
        if mapped is not None:
            mapped_rows.append(mapped)
    return mapped_rows


def build_index_html(*, ui_path: str, api_path: str, health_path: str, csv_path: str = "", fixture_mode: bool = False, no_sources_state: bool = False) -> str:
    if no_sources_state:
        top_banner = (
            '<div style="background:#fdecea;border-bottom:3px solid #c62828;padding:12px 16px;font-size:13px;color:#c62828;">'
            '<strong>Keine Live-Daten verfügbar</strong> – echte Quellen nicht konfiguriert. '
            'Bitte <code>TC_RSS_SOURCE</code> und/oder <code>TC_ADOBE_SOURCE</code> als HTTP-URL setzen. '
            'Für lokales Testen: <code>TC_FIXTURE_MODE=true</code>'
            '</div>'
        )
    elif fixture_mode:
        top_banner = (
            '<div style="background:#b71c1c;border-bottom:3px solid #7f0000;padding:8px 16px;font-size:12px;color:#fff;">'
            '<strong>DEMO-MODUS</strong> – Beispieldaten aktiv, keine echten Live-Artikel. '
            'Nur für lokale Entwicklung (TC_FIXTURE_MODE=true).'
            '</div>'
        )
    else:
        top_banner = ""
    return f"""<!doctype html>
<html lang="de">
<head>  <!-- fixture_mode={fixture_mode} health={health_path} -->
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Textchefs Artikelliste</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 13px; background: #f5f5f5; color: #1a1a1a; }}
    header {{ background: #d0021b; color: #fff; padding: 10px 16px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }}
    header h1 {{ font-size: 16px; font-weight: 700; letter-spacing: .5px; white-space: nowrap; }}
    .toolbar {{ background: #fff; border-bottom: 1px solid #ddd; padding: 8px 16px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
    #adobe-banner {{ display:none; padding: 8px 16px; font-size: 12px; border-bottom: 2px solid; }}
    #adobe-banner.error {{ background: #fdecea; color: #c62828; border-color: #c62828; }}
    #adobe-banner.warn {{ background: #fff8e1; color: #7f6000; border-color: #f9a825; }}
    #adobe-banner.ok {{ background: #e8f5e9; color: #1b5e20; border-color: #388e3c; }}
    .toolbar input {{ height: 30px; padding: 0 8px; border: 1px solid #ccc; border-radius: 3px; font-size: 13px; min-width: 200px; }}
    .toolbar select {{ height: 30px; padding: 0 6px; border: 1px solid #ccc; border-radius: 3px; font-size: 13px; }}
    .btn {{ height: 30px; padding: 0 12px; border: 1px solid #ccc; border-radius: 3px; background: #fff; font-size: 12px; cursor: pointer; white-space: nowrap; }}
    .btn:hover {{ background: #f0f0f0; }}
    .btn-primary {{ background: #d0021b; color: #fff; border-color: #b50218; }}
    .btn-primary:hover {{ background: #b50218; }}
    .status-bar {{ font-size: 11px; color: #666; margin-left: auto; white-space: nowrap; }}
    .tab-bar {{ background: #fff; border-bottom: 1px solid #ddd; padding: 0 16px; display: flex; gap: 4px; }}
    .tab-btn {{ height: 36px; padding: 0 16px; border: none; border-bottom: 3px solid transparent; background: none; font-size: 13px; font-weight: 500; cursor: pointer; color: #666; white-space: nowrap; }}
    .tab-btn:hover {{ color: #1a1a1a; }}
    .tab-btn.active {{ color: #d0021b; border-bottom-color: #d0021b; font-weight: 700; }}
    .table-wrap {{ overflow-x: auto; padding: 0 16px 16px; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; margin-top: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
    thead th {{ background: #1a1a1a; color: #fff; padding: 8px 10px; text-align: left; font-size: 11px; font-weight: 600; white-space: nowrap; cursor: pointer; user-select: none; position: sticky; top: 0; z-index: 1; }}
    thead th:hover {{ background: #333; }}
    thead th .sort-arrow {{ margin-left: 4px; opacity: .5; }}
    thead th.sorted .sort-arrow {{ opacity: 1; }}
    tbody tr {{ border-bottom: 1px solid #eee; }}
    tbody tr:hover {{ background: #fafafa; }}
    tbody tr.top5 {{ border-left: 3px solid #d0021b; }}
    td {{ padding: 7px 10px; vertical-align: top; }}
    td.rank {{ font-weight: 700; color: #888; font-size: 12px; min-width: 32px; }}
    td.score {{ min-width: 52px; }}
    .score-badge {{ display: inline-block; padding: 2px 7px; border-radius: 10px; font-size: 11px; font-weight: 700; color: #fff; }}
    .score-high {{ background: #d0021b; }}
    .score-med {{ background: #e67e00; }}
    .score-low {{ background: #888; }}
    td.title {{ max-width: 280px; }}
    td.title a {{ color: #1a1a1a; text-decoration: none; font-weight: 500; word-break: break-word; }}
    td.title a:hover {{ color: #d0021b; text-decoration: underline; }}
    td.url {{ max-width: 340px; }}
    td.url a {{ color: #555; font-size: 11px; word-break: break-all; white-space: normal; }}
    td.url a:hover {{ color: #d0021b; }}
    .status-pill {{ display: inline-block; padding: 2px 7px; border-radius: 10px; font-size: 11px; font-weight: 500; background: #e8f5e9; color: #1b5e20; }}
    .status-pill.unknown {{ background: #fff3e0; color: #7f4f00; }}
    td.readers {{ min-width: 70px; font-variant-numeric: tabular-nums; }}
    td.homepos {{ min-width: 60px; text-align: center; }}
    .home-badge {{ display: inline-block; padding: 2px 6px; border-radius: 3px; background: #e3f2fd; color: #0d47a1; font-size: 11px; font-weight: 600; }}
    td.ressort {{ min-width: 90px; }}
    td.published {{ min-width: 100px; font-size: 11px; color: #555; white-space: nowrap; }}
    td.sources {{ font-size: 10px; color: #888; }}
    .empty {{ text-align: center; padding: 40px; color: #888; }}
    .share-row {{ font-size: 11px; color: #ddd; display: flex; align-items: center; gap: 8px; }}
    .share-row a {{ color: #fff; }}
  </style>
</head>
<body>
  {top_banner}
  <div id="adobe-banner"></div>
  <header>
    <h1>Textchefs Artikelliste</h1>
    <div class="share-row">
      Share: <a id="share-link" href="{ui_path}">{ui_path}</a>
      <button class="btn btn-primary" id="copy-share-link" type="button" style="height:24px;padding:0 8px;font-size:11px;">Link kopieren</button>
      <span id="copy-status" aria-live="polite"></span>
      &nbsp;·&nbsp;<a id="health-link" href="{health_path}" style="font-size:10px;color:#eee;">health</a>
    </div>
  </header>
  <div id="no-sources-state" style="display:{'none' if not no_sources_state else 'block'};max-width:700px;margin:60px auto;padding:32px;background:#fff;border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.12);text-align:center;">
    <div style="font-size:48px;margin-bottom:16px;">⚙️</div>
    <h2 style="font-size:20px;margin-bottom:12px;color:#c62828;">Keine echten Datenquellen konfiguriert</h2>
    <p style="color:#555;margin-bottom:20px;">Die App läuft, aber es sind keine echten Live-Quellen eingerichtet.<br>Bitte folgende Umgebungsvariablen setzen:</p>
    <pre style="background:#f5f5f5;padding:14px;border-radius:4px;text-align:left;font-size:12px;color:#1a1a1a;">TC_RSS_SOURCE=https://www.bild.de/rss-feeds/...
TC_ADOBE_SOURCE=https://intern.example.com/adobe/live.json
TC_HOME_SOURCE=https://intern.example.com/home/positions.json</pre>
    <p style="color:#888;font-size:12px;margin-top:16px;">Für lokales Testen mit Beispieldaten: <code>TC_FIXTURE_MODE=true</code></p>
    <p style="color:#888;font-size:12px;"><a href="{health_path}" style="color:#d0021b;">Healthcheck</a></p>
  </div>
  <div id="app-content" style="display:{'none' if no_sources_state else 'block'}">
  <div class="tab-bar">
    <button class="tab-btn active" id="tab-main" type="button">Aktuelle Artikel</button>
    <button class="tab-btn" id="tab-excluded" type="button">Redigiert / Zum Verbauen</button>
  </div>
  <div class="toolbar">
    <input type="search" id="search" placeholder="Suche nach Titel oder URL…" autocomplete="off">
    <select id="filter-ressort"><option value="">Alle Ressorts</option></select>
    <select id="filter-home">
      <option value="">Alle</option>
      <option value="on">Auf der Home</option>
      <option value="off">Nicht auf der Home</option>
    </select>
    <button class="btn" id="toggle-top20">Nur Top 20</button>
    <button class="btn btn-primary" id="refresh-btn">Neu laden</button>
    <a class="btn" id="csv-link" href="{csv_path}" download="tc_artikelliste.csv">CSV Export</a>
    <a class="btn" id="json-link" href="{api_path}" target="_blank">JSON</a>
    <div class="status-bar" id="status-bar">lade…</div>
  </div>
  <div class="table-wrap">
    <table id="main-table">
      <thead>
        <tr>
          <th data-col="rank">#<span class="sort-arrow"></span></th>
          <th data-col="urgency_score">Score<span class="sort-arrow"></span></th>
          <th data-col="title">Titel<span class="sort-arrow"></span></th>
          <th data-col="url">URL<span class="sort-arrow"></span></th>
          <th data-col="workflow_status">Status<span class="sort-arrow"></span></th>
          <th data-col="live_readers">Live-Leser<span class="sort-arrow"></span></th>
          <th data-col="home_position">Home-Pos.<span class="sort-arrow"></span></th>
          <th data-col="ressort">Ressort<span class="sort-arrow"></span></th>
          <th data-col="published_at">Veröffentlicht<span class="sort-arrow"></span></th>
          <th data-col="source_flags">Quellen<span class="sort-arrow"></span></th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
    <div class="empty" id="empty-msg" style="display:none">Keine Artikel gefunden.</div>
  </div>
  </div><!-- end app-content -->

  <script>
    const NO_SOURCES = {json.dumps(no_sources_state)};
    const FIXTURE_ACTIVE = {json.dumps(fixture_mode)};

    const fallbackUiPath = {json.dumps(ui_path)};
    const fallbackApiPath = {json.dumps(api_path)};
    const fallbackCsvPath = {json.dumps(csv_path)};

    function resolveUrl(path) {{
      try {{ return new URL(path, window.location.origin).toString(); }} catch(_) {{ return path; }}
    }}
    function runtimeUiUrl() {{
      const p = window.location.pathname || '/';
      return resolveUrl(p.endsWith('/') ? p : p + '/');
    }}

    let resolvedUiUrl = resolveUrl(fallbackUiPath);
    let resolvedApiUrl = resolveUrl(fallbackApiPath);
    let resolvedCsvUrl = resolveUrl(fallbackCsvPath);
    try {{
      resolvedUiUrl = runtimeUiUrl();
      resolvedApiUrl = new URL('api/articles', resolvedUiUrl).toString();
      resolvedCsvUrl = new URL('api/export/csv', resolvedUiUrl).toString();
    }} catch(_) {{}}

    document.getElementById('share-link').href = resolvedUiUrl;
    document.getElementById('share-link').textContent = resolvedUiUrl;
    document.getElementById('json-link').href = resolvedApiUrl;
    document.getElementById('csv-link').href = resolvedCsvUrl;

    // ── State ──────────────────────────────────────────────────────────────
    let allArticles = [];
    let activeTab = 'main';
    let sortCol = 'rank';
    let sortAsc = true;
    let showTop20 = false;
    let refreshTimer = null;
    let countdown = 60;
    let adobeActive = false; // true sobald tokenStatus=ok

    // ── DOM refs ───────────────────────────────────────────────────────────
    const tbody = document.getElementById('tbody');
    const emptyMsg = document.getElementById('empty-msg');
    const statusBar = document.getElementById('status-bar');
    const searchEl = document.getElementById('search');
    const filterRessort = document.getElementById('filter-ressort');
    const filterHome = document.getElementById('filter-home');
    const toggle20Btn = document.getElementById('toggle-top20');
    const refreshBtn = document.getElementById('refresh-btn');

    // ── Helpers ────────────────────────────────────────────────────────────
    function fmtDate(iso) {{
      if (!iso) return '';
      try {{
        const d = new Date(iso);
        return d.toLocaleString('de-DE', {{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}});
      }} catch(_) {{ return iso; }}
    }}

    function scoreClass(s) {{
      if (s >= 60) return 'score-high';
      if (s >= 25) return 'score-med';
      return 'score-low';
    }}

    function esc(s) {{
      return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }}

    // ── Filters ────────────────────────────────────────────────────────────
    function applyFilters(articles) {{
      const q = searchEl.value.trim().toLowerCase();
      const res = filterRessort.value;
      const home = filterHome.value;
      return articles.filter((a, idx) => {{
        if (q && !((a.title||'').toLowerCase().includes(q) || (a.canonical_url||'').toLowerCase().includes(q))) return false;
        if (res && a.ressort !== res) return false;
        if (home === 'on' && a.home_position == null) return false;
        if (home === 'off' && a.home_position != null) return false;
        if (showTop20 && idx >= 20) return false;
        return true;
      }});
    }}

    // ── Sort ───────────────────────────────────────────────────────────────
    function sortArticles(articles) {{
      const col = sortCol;
      const asc = sortAsc;
      return [...articles].sort((a, b) => {{
        let va = a[col] ?? (col === 'home_position' ? 1e9 : (typeof a[col] === 'number' ? -1e9 : ''));
        let vb = b[col] ?? (col === 'home_position' ? 1e9 : (typeof b[col] === 'number' ? -1e9 : ''));
        if (col === 'rank') {{ va = a._rank ?? 0; vb = b._rank ?? 0; }}
        if (typeof va === 'string') return asc ? va.localeCompare(vb) : vb.localeCompare(va);
        return asc ? va - vb : vb - va;
      }});
    }}

    // ── Render ─────────────────────────────────────────────────────────────
    function render() {{
      const filtered = applyFilters(allArticles);
      const sorted = sortArticles(filtered);
      if (!sorted.length) {{
        tbody.innerHTML = '';
        emptyMsg.style.display = '';
        return;
      }}
      emptyMsg.style.display = 'none';
      const html = sorted.map((a, i) => {{
        const rank = a._rank ?? (i + 1);
        const isTop5 = rank <= 5;
        const score = a.urgency_score ?? 0;
        const url = a.source_url || a.canonical_url || '';
        const title = a.title || url;
        const statusCls = a.workflow_status ? '' : ' unknown';
        const statusLabel = a.workflow_status || 'unbekannt';
        const homeHtml = a.home_position != null
          ? `<span class="home-badge">Pos. ${{a.home_position}}</span>`
          : '<span style="color:#aaa">—</span>';
        const sources = (a.source_flags || []).join(', ');
        const readersVal = a.live_readers;
        const readersHtml = adobeActive
          ? (readersVal != null ? Number(readersVal).toLocaleString('de-DE') : '<span style="color:#aaa">n.v.</span>')
          : '<span style="color:#aaa" title="Adobe Analytics nicht aktiv">n.v.</span>';
        return `<tr class="${{isTop5 ? 'top5' : ''}}">
          <td class="rank">${{rank}}</td>
          <td class="score"><span class="score-badge ${{scoreClass(score)}}">${{score}}</span></td>
          <td class="title"><a href="${{esc(url)}}" target="_blank" rel="noopener">${{esc(title)}}</a></td>
          <td class="url"><a href="${{esc(url)}}" target="_blank" rel="noopener">${{esc(url)}}</a></td>
          <td><span class="status-pill${{statusCls}}">${{esc(statusLabel)}}</span></td>
          <td class="readers">${{readersHtml}}</td>
          <td class="homepos">${{homeHtml}}</td>
          <td class="ressort">${{esc(a.ressort || '')}}</td>
          <td class="published">${{fmtDate(a.published_at)}}</td>
          <td class="sources">${{esc(sources)}}</td>
        </tr>`;
      }}).join('');
      tbody.innerHTML = html;
    }}

    // ── Adobe status banner ────────────────────────────────────────────────
    const adobeBanner = document.getElementById('adobe-banner');

    function updateAdobeBanner(h) {{
      const adobe = h && h.adobe;
      if (!adobe) {{ adobeBanner.style.display = 'none'; return; }}
      const ts = adobe.adobeTokenStatus;
      const err = adobe.adobeLastError;
      const total = h.articlesTotal;
      const withAdobe = h.articlesWithAdobeLiveReaders;
      adobeActive = ts === 'ok';
      if (ts === 'ok') {{
        const parts = ['Adobe Analytics: verbunden'];
        if (typeof withAdobe === 'number' && typeof total === 'number') {{
          parts.push(`${{withAdobe}} von ${{total}} Artikeln mit Live-Leser-Wert`);
        }}
        adobeBanner.className = 'ok';
        adobeBanner.textContent = parts.join(' · ');
        adobeBanner.style.display = '';
      }} else if (ts === 'error') {{
        adobeBanner.className = 'error';
        adobeBanner.textContent = 'Adobe Analytics: Fehler' + (err ? ' – ' + err : '');
        adobeBanner.style.display = '';
      }} else if (ts === 'configured_untested') {{
        adobeBanner.className = 'warn';
        adobeBanner.textContent = 'Adobe Analytics: konfiguriert, aber noch nicht erfolgreich getestet';
        adobeBanner.style.display = '';
      }} else if (!adobe.adobeConfigured) {{
        adobeBanner.className = 'warn';
        adobeBanner.textContent = 'Adobe Analytics: nicht konfiguriert – Live-Leser-Werte nicht verfügbar';
        adobeBanner.style.display = '';
      }} else {{
        adobeBanner.style.display = 'none';
      }}
    }}

    function loadHealthz() {{
      const healthUrl = resolveUrl({json.dumps(health_path)});
      fetch(healthUrl).then(r => r.ok ? r.json() : null).then(h => {{
        if (!h) return;
        updateAdobeBanner(h);
        render(); // re-render Tabelle mit korrektem adobeActive-Flag
      }}).catch(() => {{}});
    }}

    // ── Data load ──────────────────────────────────────────────────────────
    function updateStatus(msg) {{ statusBar.textContent = msg; }}

    function loadData(force) {{
      const tabParam = activeTab === 'excluded' ? '?tab=excluded' : (force ? '?refresh=1' : '');
      updateStatus('Lade…');
      fetch(resolvedApiUrl + tabParam)
        .then(r => {{ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }})
        .then(data => {{
          allArticles = data.map((a, i) => ({{ ...a, _rank: i + 1 }}));
          populateRessortFilter();
          render();
          const now = new Date().toLocaleTimeString('de-DE', {{hour:'2-digit',minute:'2-digit',second:'2-digit'}});
          updateStatus(`${{allArticles.length}} Artikel · Stand: ${{now}}`);
          resetCountdown();
        }})
        .catch(e => {{ updateStatus('Fehler: ' + e.message); }});
    }}

    function populateRessortFilter() {{
      const current = filterRessort.value;
      const ressorts = [...new Set(allArticles.map(a => a.ressort).filter(Boolean))].sort();
      filterRessort.innerHTML = '<option value="">Alle Ressorts</option>' +
        ressorts.map(r => `<option value="${{esc(r)}}"${{r === current ? ' selected' : ''}}>${{esc(r)}}</option>`).join('');
    }}

    // ── Auto-refresh countdown ─────────────────────────────────────────────
    function resetCountdown() {{
      countdown = 60;
      clearInterval(refreshTimer);
      refreshTimer = setInterval(() => {{
        countdown -= 1;
        if (countdown <= 0) {{
          loadData(false);
        }} else {{
          const base = statusBar.textContent.replace(/ · Refresh in \\d+s$/, '');
          statusBar.textContent = base + ` · Refresh in ${{countdown}}s`;
        }}
      }}, 1000);
    }}

    // ── Sort header clicks ─────────────────────────────────────────────────
    document.querySelectorAll('thead th[data-col]').forEach(th => {{
      th.addEventListener('click', () => {{
        const col = th.dataset.col;
        if (sortCol === col) {{ sortAsc = !sortAsc; }} else {{ sortCol = col; sortAsc = col !== 'live_readers' && col !== 'urgency_score'; }}
        document.querySelectorAll('thead th').forEach(t => t.classList.remove('sorted'));
        th.classList.add('sorted');
        th.querySelector('.sort-arrow').textContent = sortAsc ? ' ▲' : ' ▼';
        render();
      }});
    }});

    // ── Controls ───────────────────────────────────────────────────────────
    searchEl.addEventListener('input', render);
    filterRessort.addEventListener('change', render);
    filterHome.addEventListener('change', render);
    toggle20Btn.addEventListener('click', () => {{
      showTop20 = !showTop20;
      toggle20Btn.textContent = showTop20 ? 'Alle zeigen' : 'Nur Top 20';
      render();
    }});
    refreshBtn.addEventListener('click', () => loadData(true));

    document.getElementById('copy-share-link').addEventListener('click', async () => {{
      const copyStatus = document.getElementById('copy-status');
      try {{
        if (navigator.clipboard && window.isSecureContext) {{
          await navigator.clipboard.writeText(resolvedUiUrl);
        }} else {{
          const ta = document.createElement('textarea');
          ta.value = resolvedUiUrl; ta.style.position='absolute'; ta.style.left='-9999px';
          document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
        }}
        copyStatus.textContent = 'Link kopiert';
        setTimeout(() => {{ copyStatus.textContent = ''; }}, 2000);
      }} catch(_) {{ copyStatus.textContent = 'Kopieren fehlgeschlagen'; }}
    }});

    // ── Tab-Switch ─────────────────────────────────────────────────────────
    document.getElementById('tab-main').addEventListener('click', () => {{
      activeTab = 'main';
      document.getElementById('tab-main').classList.add('active');
      document.getElementById('tab-excluded').classList.remove('active');
      sortCol = 'rank'; sortAsc = true;
      loadData(false);
    }});
    document.getElementById('tab-excluded').addEventListener('click', () => {{
      activeTab = 'excluded';
      document.getElementById('tab-excluded').classList.add('active');
      document.getElementById('tab-main').classList.remove('active');
      sortCol = 'rank'; sortAsc = true;
      loadData(false);
    }});

    // ── Init ───────────────────────────────────────────────────────────────
    if (NO_SOURCES) {{
      updateStatus('Keine echten Datenquellen konfiguriert.');
      document.getElementById('csv-link').style.display = 'none';
      document.getElementById('json-link').style.display = 'none';
      document.getElementById('refresh-btn').style.display = 'none';
    }} else {{
      loadHealthz();
      loadData(false);
    }}
  </script>
</body>
</html>""".strip()


ADOBE_SOURCE = _env_source("TC_ADOBE_SOURCE", ADOBE)
RSS_SOURCE = _env_source("TC_RSS_SOURCE", RSS)
HOME_SOURCE = _env_source("TC_HOME_SOURCE", HOME)

def _is_fixture(source: Path | str) -> bool:
    return not str(source).strip().lower().startswith(("http://", "https://"))

API_CHECK = _env_bool("TC_API_CHECK", default=False)
CACHE_SECONDS = _env_int("TC_CACHE_SECONDS", default=30, minimum=0)
BASE_PATH = _normalize_base_path(os.environ.get("TC_BASE_PATH", "/"))
PUBLIC_BASE_URL = _normalize_public_base_url(os.environ.get("TC_PUBLIC_BASE_URL"))
EDITORIAL_ONE_ENABLED = _env_bool("TC_EDITORIAL_ONE_ENABLED", default=False)
EDITORIAL_ONE_STRICT = _env_bool("TC_EDITORIAL_ONE_STRICT", default=False)

# TC_FIXTURE_MODE=true erlaubt Fixture-Daten nur in explizitem Development-Modus.
FIXTURE_MODE_EXPLICIT = _env_bool("TC_FIXTURE_MODE", default=False)
USING_REAL_DATA = EDITORIAL_ONE_ENABLED or not _is_fixture(ADOBE_SOURCE) or not _is_fixture(RSS_SOURCE)
# Fixture-Modus nur aktiv wenn explizit aktiviert UND keine echten Quellen vorhanden.
FIXTURE_MODE_ACTIVE = FIXTURE_MODE_EXPLICIT and not USING_REAL_DATA
# Setup-State: keine echten Quellen, kein Fixture-Flag → leere Artikelliste, kein Export.
NO_SOURCES_STATE = not USING_REAL_DATA and not FIXTURE_MODE_EXPLICIT
# Rückwärtskompatibel für Tests
IS_FIXTURE_MODE = FIXTURE_MODE_ACTIVE
EDITORIAL_ONE_HOURS = _env_int("TC_EDITORIAL_ONE_HOURS", default=24, minimum=1)
EDITORIAL_ONE_LIMIT = _env_int("TC_EDITORIAL_ONE_LIMIT", default=300, minimum=1)
EDITORIAL_ONE_PYC = os.environ.get(
    "TC_EDITORIAL_ONE_PYC",
    "/Users/riccardo.longo/editorial-intel/__pycache__/bild_published.cpython-313.pyc",
).strip()
_CACHE_LOCK = threading.Lock()
_CACHE_DATA: list[dict[str, object]] | None = None
_CACHE_EXCLUDED: list[dict[str, object]] = []
_CACHE_EXPIRES_AT = 0.0
_LOAD_IN_PROGRESS = threading.Lock()  # verhindert parallele Lade-Vorgänge
_EDITORIAL_ONE_MODULE = None
_EDITORIAL_ONE_MODULE_LOCK = threading.Lock()
_ADOBE_ENRICHMENT_RUNNING = threading.Event()


import math as _math


def _readers_score(live_r: int) -> float:
    """Logarithmische Skalierung: differenziert von 0 bis 100 über den vollen Leserbereich.
    0 Leser=0, ~100=10, ~1k=27, ~10k=53, ~50k=75, ~100k=85, ~500k=100
    """
    if live_r <= 0:
        return 0.0
    return min(100.0, _math.log10(live_r + 1) / _math.log10(500_001) * 100.0)


def _recompute_urgency_score(article: dict[str, object]) -> None:
    """Urgency-Score nach live_readers-Update neu berechnen."""
    live_r = _safe_int(article.get("live_readers"), default=0)
    home_pos_raw = article.get("home_position")
    home_pos = _safe_int(home_pos_raw, default=0) if home_pos_raw is not None else None
    home_score = max(0.0, 100.0 - (home_pos - 1) * 5.0) if home_pos else 0.0
    r_score = _readers_score(live_r)
    if home_pos:
        article["urgency_score"] = round(0.6 * home_score + 0.4 * r_score)
    else:
        # Ohne Home-Position: rein readers-basiert, voller 0–100 Bereich
        article["urgency_score"] = round(r_score)


def _sort_articles_inplace(articles: list[dict[str, object]]) -> None:
    """Sortiert nach: Home-Pos (aufsteigend) → Live-Leser (absteigend) → Score → Datum."""
    def _key(a: dict[str, object]) -> tuple:
        home = _safe_int(a.get("home_position"), default=0)
        home_sort = home if home > 0 else 10 ** 9
        readers = -_safe_int(a.get("live_readers"), default=0)
        score = -_safe_int(a.get("urgency_score"), default=0)
        pub = a.get("published_at") or ""
        return (home_sort, readers, score, str(pub))
    articles.sort(key=_key)


def _run_adobe_enrichment_async(data: list[dict[str, object]]) -> None:
    """Holt Live-Leser im Hintergrund und aktualisiert Cache + Sortierung."""
    if not _ADOBE_AVAILABLE:
        return
    try:
        urls = [a.get("canonical_url", "") for a in data if a.get("canonical_url")]
        home_map: dict[str, int] = {}
        try:
            home_map = _adobe.fetch_home_positions(urls)
        except Exception:
            pass
        if home_map:
            with _CACHE_LOCK:
                if _CACHE_DATA is not None:
                    for a in _CACHE_DATA:
                        url = a.get("canonical_url", "")
                        if url in home_map:
                            a["home_position"] = home_map[url]
                            flags = list(a.get("source_flags") or [])
                            if "home" not in flags:
                                flags.append("home")
                            a["source_flags"] = sorted(flags)
                        _recompute_urgency_score(a)
                    _sort_articles_inplace(_CACHE_DATA)
    except Exception:
        pass
    finally:
        _ADOBE_ENRICHMENT_RUNNING.clear()


def _enrich_adobe_articles_with_es(
    adobe_articles: list[dict[str, object]],
) -> list[dict[str, object]]:
    """
    Reichert Adobe-Artikel mit Metadaten aus dem ES-Feed an.
    Matching: documentId (24-hex) aus Artikel-URL vs ES-Feed documentId.
    ES-Feed-Artikel die nicht in Adobe-Liste sind werden HINZUGEFÜGT.
    """
    if not _ES_AVAILABLE:
        return adobe_articles
    es_articles = _es.fetch_articles()
    if not es_articles:
        return adobe_articles

    import re as _re
    _ID_RE = _re.compile(r"([0-9a-f]{24})")

    # ES-Index: documentId → ES-Artikel
    es_by_id: dict[str, dict] = {a["document_id"]: a for a in es_articles if a.get("document_id")}
    # ES-Index: canonical_url-Pfad → ES-Artikel
    from urllib.parse import urlparse
    es_by_path: dict[str, dict] = {}
    for a in es_articles:
        if a.get("canonical_url"):
            path = urlparse(a["canonical_url"]).path.rstrip("/").lower()
            if path:
                es_by_path[path] = a

    # Adobe-Artikel mit ES-Metadaten anreichern
    adobe_ids_matched: set[str] = set()
    for a in adobe_articles:
        canon = str(a.get("canonical_url") or "")
        # Versuche documentId aus URL extrahieren
        m = _ID_RE.search(canon)
        doc_id = m.group(1) if m else ""
        es = es_by_id.get(doc_id) if doc_id else None
        if not es:
            path = urlparse(canon).path.rstrip("/").lower()
            es = es_by_path.get(path)
            if not es:
                # Präfix-Match für truncated Adobe URLs
                for es_path, es_a in es_by_path.items():
                    if (len(path) >= 30 and es_path.startswith(path[:60])) or \
                       (len(es_path) >= 30 and path.startswith(es_path[:60])):
                        es = es_a
                        break
        if es:
            adobe_ids_matched.add(es.get("document_id", ""))
            # Vollständige URL aus ES-Feed übernehmen
            if es.get("source_url") and "/cmsid/" not in es.get("source_url", ""):
                a["source_url"] = es["source_url"]
                a["canonical_url"] = es.get("canonical_url", a["canonical_url"])
            if not a.get("title") or str(a.get("title","")).strip() == "":
                a["title"] = es.get("title", "")
            a["workflow_status"] = es.get("workflow_status", a.get("workflow_status", ""))
            a["ressort"] = es.get("ressort") or a.get("ressort", "")
            if not a.get("published_at"):
                a["published_at"] = es.get("published_at")
            a["cms_id"] = es.get("document_id", "")
            flags = list(a.get("source_flags") or [])
            if "es_feed" not in flags:
                flags.append("es_feed")
            a["source_flags"] = sorted(flags)

    # ES-Artikel die nicht in Adobe-Liste sind hinzufügen (neueste publizierten Artikel)
    existing_canonical = {str(a.get("canonical_url","")) for a in adobe_articles}
    for es_a in es_articles:
        es_doc_id = es_a.get("document_id", "")
        es_canon = es_a.get("canonical_url", "")
        if es_doc_id in adobe_ids_matched:
            continue
        if es_canon in existing_canonical:
            continue
        # Nicht in Adobe → als neuer Artikel hinzufügen (live_readers=0)
        adobe_articles.append(es_a)
    return adobe_articles


def _enrich_adobe_articles_with_rss(
    adobe_articles: list[dict[str, object]],
    rss_src,
) -> list[dict[str, object]]:
    """
    Reichert Adobe-Artikel mit Metadaten aus RSS an (Titel, pubDate, premium, Ressort).
    Adobe-Artikel die keinen RSS-Match haben behalten leere Metadaten.
    """
    if not rss_src:
        return adobe_articles
    try:
        from article_list_mvp import load_rss, canonicalize_url, _detect_ressort, _parse_dt
        rss_items = load_rss(rss_src)
        # RSS-Index nach kanonischer URL + Pfad-Präfix
        rss_by_path: dict[str, dict] = {}
        for item in rss_items:
            url = str(item.get("link") or "").strip()
            if not url:
                continue
            from urllib.parse import urlparse
            path = urlparse(url).path.rstrip("/").lower()
            if path:
                rss_by_path[path] = item

        for a in adobe_articles:
            canon = str(a.get("canonical_url") or "")
            from urllib.parse import urlparse
            canon_path = urlparse(canon).path.rstrip("/").lower()
            rss_item = rss_by_path.get(canon_path)
            # Präfix-Match wenn kein exakter Treffer
            if not rss_item:
                for rss_path, item in rss_by_path.items():
                    if (len(canon_path) >= 30 and
                            (rss_path.startswith(canon_path[:50]) or
                             canon_path.startswith(rss_path[:50]))):
                        rss_item = item
                        break
            if rss_item:
                # RSS-Titel hat Vorrang (vollständiger Titel), außer Adobe-Titel ist schon gesetzt
                rss_title = str(rss_item.get("title") or "").strip()
                if rss_title:
                    a["title"] = rss_title
                elif not a.get("title"):
                    a["title"] = ""
                if not a.get("published_at"):
                    pub = _parse_dt(str(rss_item.get("pubDate") or ""))
                    a["published_at"] = pub.isoformat() if pub else None
                if not a.get("workflow_status"):
                    prem = str(rss_item.get("premium") or "").strip().lower()
                    a["workflow_status"] = "BILD+" if prem == "true" else ("Frei" if prem == "false" else "")
                if not a.get("ressort"):
                    a["ressort"] = _detect_ressort(canon)
                a.setdefault("source_flags", [])
                if "rss" not in a["source_flags"]:
                    a["source_flags"] = sorted(set(a["source_flags"]) | {"rss", "adobe"})
            else:
                # Kein RSS-Match: Ressort aus URL ableiten
                if not a.get("ressort"):
                    a["ressort"] = _detect_ressort(canon)
                if not a.get("workflow_status"):
                    a["workflow_status"] = "Frei"  # Default für Adobe-only
                a.setdefault("title", "")
                a.setdefault("published_at", None)
                a.setdefault("source_flags", ["adobe"])
            a.setdefault("cms_id", "")
    except Exception:
        pass
    return adobe_articles


def _do_fetch_articles(
    force_refresh: bool, adobe_src, rss_src, home_src
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Führt den eigentlichen Datenabruf durch — OHNE Lock, kann lange dauern.
    Gibt (main_articles, excluded_articles) zurück."""
    if EDITORIAL_ONE_ENABLED:
        try:
            editorial_one_data = _load_editorial_one_articles(force_refresh=force_refresh)
            if editorial_one_data:
                return editorial_one_data, []
            if EDITORIAL_ONE_STRICT:
                raise RuntimeError("Editorial-One lieferte keine Artikel")
        except Exception:
            if EDITORIAL_ONE_STRICT:
                raise

    # Adobe als primäre Quelle wenn konfiguriert
    if _ADOBE_AVAILABLE and _adobe.get_adobe_status().get("adobeConfigured"):
        try:
            adobe_articles = _adobe.fetch_top_article_urls(n=100)
            if adobe_articles:
                editing_articles: list[dict[str, object]] = []
                # ES-Feed für Metadaten + Editing-Artikel nutzen
                if _ES_AVAILABLE and _es.get_status().get("esFeedConfigured"):
                    try:
                        adobe_articles = _enrich_adobe_articles_with_es(adobe_articles)
                    except Exception:
                        pass
                    try:
                        editing_articles = _es.fetch_editing_articles()
                    except Exception:
                        pass
                # Restliche ohne ES-Metadaten aus RSS anreichern
                adobe_articles = _enrich_adobe_articles_with_rss(adobe_articles, rss_src)
                # Scores berechnen
                for a in adobe_articles:
                    _recompute_urgency_score(a)
                _sort_articles_inplace(adobe_articles)
                return adobe_articles, editing_articles
        except Exception:
            pass  # Fallback auf RSS-only

    result = run_mvp(
        adobe_file=adobe_src,
        rss_file=rss_src,
        home_file=home_src,
        api_check=API_CHECK,
        return_excluded=True,
    )
    if isinstance(result, tuple):
        return result
    return result, []


def load_excluded_articles() -> list[dict[str, object]]:
    """Gibt die aktuellen ausgeschlossenen Artikel (redigiert/zum verbauen) zurück."""
    return _CACHE_EXCLUDED


def load_articles(force_refresh: bool = False) -> list[dict[str, object]]:
    global _CACHE_DATA, _CACHE_EXCLUDED, _CACHE_EXPIRES_AT

    now = time.monotonic()
    if not force_refresh and CACHE_SECONDS > 0 and _CACHE_DATA is not None and now < _CACHE_EXPIRES_AT:
        return _CACHE_DATA

    # Wenn bereits ein Ladevorgang läuft: sofort stale/leere Daten zurückgeben
    # (verhindert, dass HTTP-Requests auf den Render-Proxy-Timeout von 5s warten)
    if not _LOAD_IN_PROGRESS.acquire(blocking=False):
        return _CACHE_DATA if _CACHE_DATA is not None else []

    try:
        # Double-check nach Lock-Erwerb
        now = time.monotonic()
        if not force_refresh and CACHE_SECONDS > 0 and _CACHE_DATA is not None and now < _CACHE_EXPIRES_AT:
            return _CACHE_DATA

        # Dynamisch auswerten damit monkeypatching in Tests funktioniert.
        _no_sources_now = (
            not EDITORIAL_ONE_ENABLED
            and _is_fixture(ADOBE_SOURCE)
            and _is_fixture(RSS_SOURCE)
            and not FIXTURE_MODE_EXPLICIT
        )
        if _no_sources_now:
            return []

        _adobe_src = ADOBE_SOURCE if (FIXTURE_MODE_EXPLICIT or not _is_fixture(ADOBE_SOURCE)) else None
        _rss_src = RSS_SOURCE if (FIXTURE_MODE_EXPLICIT or not _is_fixture(RSS_SOURCE)) else None
        _home_src = HOME_SOURCE if (FIXTURE_MODE_EXPLICIT or not _is_fixture(HOME_SOURCE)) else None

        # RSS-Fetch läuft ohne Cache-Lock — dauert >5s, darf andere Requests nicht blockieren
        data, excluded = _do_fetch_articles(force_refresh, _adobe_src, _rss_src, _home_src)

        # Alte live_readers-Werte aus dem Cache in neue Artikel übernehmen
        with _CACHE_LOCK:
            old_cache = _CACHE_DATA
        if old_cache and data:
            old_readers: dict[str, int] = {
                str(a.get("canonical_url", "")): _safe_int(a.get("live_readers"), default=0)
                for a in old_cache if a.get("live_readers")
            }
            if old_readers:
                changed = False
                for a in data:
                    url = str(a.get("canonical_url", ""))
                    old_val = old_readers.get(url, 0)
                    if old_val > 0 and _safe_int(a.get("live_readers"), default=0) == 0:
                        a["live_readers"] = old_val
                        _recompute_urgency_score(a)
                        changed = True
                if changed:
                    _sort_articles_inplace(data)

        # Adobe Live-Reader-Enrichment: im Hintergrund
        if _ADOBE_AVAILABLE and data and _adobe.get_adobe_status().get("adobeConfigured"):
            if not _ADOBE_ENRICHMENT_RUNNING.is_set():
                _ADOBE_ENRICHMENT_RUNNING.set()
                t = threading.Thread(target=_run_adobe_enrichment_async, args=(data,), daemon=True)
                t.start()

        # Ergebnis in Cache schreiben
        with _CACHE_LOCK:
            if CACHE_SECONDS > 0:
                _CACHE_DATA = data
                _CACHE_EXCLUDED = excluded
                _CACHE_EXPIRES_AT = time.monotonic() + CACHE_SECONDS
            else:
                _CACHE_DATA = data
                _CACHE_EXCLUDED = excluded
                _CACHE_EXPIRES_AT = 0.0
        return data
    finally:
        _LOAD_IN_PROGRESS.release()


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, data: object, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, html: str, status: int = 200) -> None:
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = _normalize_request_path(parsed.path)
        query = parse_qs(parsed.query)
        route_path = _path_from_request(path)

        if route_path == "/healthz":
            cached = _CACHE_DATA or []
            articles_with_readers = sum(1 for a in cached if (a.get("live_readers") or 0) > 0)
            self._send_json(
                {
                    "status": "ok",
                    "cache_seconds": CACHE_SECONDS,
                    "api_check": API_CHECK,
                    "base_path": BASE_PATH,
                    "public_base_url": PUBLIC_BASE_URL,
                    "share_url": _public_url("/"),
                    "using_real_data": USING_REAL_DATA,
                    "adobe_source_configured": not _is_fixture(ADOBE_SOURCE),
                    "rss_source_configured": not _is_fixture(RSS_SOURCE),
                    "fixture_mode_active": FIXTURE_MODE_ACTIVE,
                    "fixture_mode_explicit": FIXTURE_MODE_EXPLICIT,
                    "no_sources_state": NO_SOURCES_STATE,
                    "adobe": _adobe.get_adobe_status() if _ADOBE_AVAILABLE else {"adobeConfigured": False},
                    "es_feed": _es.get_status() if _ES_AVAILABLE else {"esFeedConfigured": False},
                    "editorial_one_enabled": EDITORIAL_ONE_ENABLED,
                    "editorial_one_strict": EDITORIAL_ONE_STRICT,
                    "editorial_one_hours": EDITORIAL_ONE_HOURS if EDITORIAL_ONE_ENABLED else None,
                    "editorial_one_limit": EDITORIAL_ONE_LIMIT if EDITORIAL_ONE_ENABLED else None,
                    "articlesTotal": len(cached),
                    "articlesWithAdobeLiveReaders": articles_with_readers,
                    "articlesMissingAdobeLiveReaders": len(cached) - articles_with_readers,
                    "liveReadersMissingShownAsZero": False,
                }
            )
            return

        if route_path == "/api/articles":
            refresh = query.get("refresh", [""])[0].strip().lower() in {"1", "true", "yes"}
            tab = query.get("tab", ["main"])[0].strip().lower()
            try:
                if tab == "excluded":
                    self._send_json(load_excluded_articles())
                else:
                    data = load_articles(force_refresh=refresh)
                    self._send_json(data)
            except Exception as exc:
                self._send_json({"error": "data_load_failed", "message": str(exc)}, status=502)
                return
            return

        if route_path == "/api/admin/adobe/test":
            if not _ADOBE_AVAILABLE:
                self._send_json({"error": "adobe_module_unavailable"}, status=503)
                return
            result = _adobe.test_auth()
            self._send_json(result)
            return

        if route_path == "/api/export/csv":
            if NO_SOURCES_STATE:
                self._send_json({"error": "no_real_sources", "message": "Keine echten Datenquellen konfiguriert. Bitte TC_RSS_SOURCE oder TC_ADOBE_SOURCE als HTTP-URL setzen."}, status=503)
                return
            try:
                data = load_articles(force_refresh=False)
            except Exception as exc:
                self._send_json({"error": "data_load_failed", "message": str(exc)}, status=502)
                return
            buf = io.StringIO()
            fieldnames = [
                "rank", "urgency_score", "title", "canonical_url", "workflow_status",
                "live_readers", "home_position", "ressort", "published_at", "source_flags", "cms_id",
            ]
            writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            for idx, row in enumerate(data, start=1):
                row_out = dict(row)
                row_out["rank"] = idx
                row_out["source_flags"] = ", ".join(row_out.get("source_flags") or [])
                writer.writerow({k: row_out.get(k, "") for k in fieldnames})
            payload = buf.getvalue().encode("utf-8-sig")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="tc_artikelliste.csv"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if route_path == "/":
            link_base_path = _base_path_for_links(path)
            self._send_html(
                build_index_html(
                    ui_path=_with_base_path("/", base_path=link_base_path),
                    api_path=_with_base_path("/api/articles", base_path=link_base_path),
                    health_path=_with_base_path("/healthz", base_path=link_base_path),
                    csv_path=_with_base_path("/api/export/csv", base_path=link_base_path),
                    fixture_mode=FIXTURE_MODE_ACTIVE,
                    no_sources_state=NO_SOURCES_STATE,
                )
            )
            return

        self._send_json({"error": "not_found"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = _normalize_request_path(parsed.path)
        route_path = _path_from_request(path)

        if route_path == "/api/admin/refresh":
            try:
                data = load_articles(force_refresh=True)
            except Exception as exc:
                self._send_json({"error": "refresh_failed", "message": str(exc)}, status=502)
                return
            adobe_status: dict[str, object] = {}
            if _ADOBE_AVAILABLE:
                adobe_status = _adobe.get_adobe_status()
            articles_with_readers = sum(1 for a in data if (a.get("live_readers") or 0) > 0)
            self._send_json({
                "refreshed": True,
                "articlesTotal": len(data),
                "articlesWithAdobeLiveReaders": articles_with_readers,
                "articlesMissingAdobeLiveReaders": len(data) - articles_with_readers,
                "adobe": adobe_status,
            })
            return

        self._send_json({"error": "not_found"}, status=404)


if __name__ == "__main__":
    host = os.environ.get("TC_HOST", "0.0.0.0")
    port, port_source = _resolve_listen_port()

    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"Server-Start fehlgeschlagen auf {host}:{port}: {exc}")
        print("Setze bei Bedarf einen freien Port, z. B.: TC_PORT=8100 python3 src/server.py")
        print("Wenn nur lokale Tests gewünscht sind: TC_HOST=127.0.0.1 TC_PORT=8100 python3 src/server.py")
        print("Serverloser Fallback (ohne localhost-Port): python3 src/static_preview.py")
        raise SystemExit(1)

    actual_port = server.server_address[1]
    print(f"Server gebunden auf {host}:{actual_port}")
    print(f"Port-Quelle: {port_source}")
    print(f"Datenquellen: adobe={ADOBE_SOURCE} rss={RSS_SOURCE} home={HOME_SOURCE}")
    print(f"API-Check: {'aktiv' if API_CHECK else 'aus'} | Cache: {CACHE_SECONDS}s | Base-Path: {BASE_PATH}")
    if EDITORIAL_ONE_ENABLED:
        print(
            "Editorial-One Modus: aktiv"
            f" | pyc={EDITORIAL_ONE_PYC}"
            f" | hours={EDITORIAL_ONE_HOURS}"
            f" | limit={EDITORIAL_ONE_LIMIT}"
            f" | strict={'ja' if EDITORIAL_ONE_STRICT else 'nein'}"
        )
    else:
        print("Editorial-One Modus: aus (nutze TC_ADOBE_SOURCE/TC_RSS_SOURCE/TC_HOME_SOURCE)")
    if PUBLIC_BASE_URL:
        print(f"Share-Link: {_public_url('/')}")
        print(f"Share-API: {_public_url('/api/articles')}")
        print(f"Share-Health: {_public_url('/healthz')}")
    else:
        print(f"Hinweis: Für klaren Hosting-Link TC_PUBLIC_BASE_URL setzen, z. B. https://dein-host{_with_base_path('/')}")
    if host == "0.0.0.0":
        print(f"UI lokal: http://localhost:{actual_port}{_with_base_path('/')}")
        print(f"API lokal: http://localhost:{actual_port}{_with_base_path('/api/articles')}")
        print(f"Health lokal: http://localhost:{actual_port}{_with_base_path('/healthz')}")
        print(f"Alternative lokal: http://127.0.0.1:{actual_port}{_with_base_path('/')}")
    else:
        print(f"UI lokal: http://{host}:{actual_port}{_with_base_path('/')}")
        print(f"API lokal: http://{host}:{actual_port}{_with_base_path('/api/articles')}")
        print(f"Health lokal: http://{host}:{actual_port}{_with_base_path('/healthz')}")
    # Startup-Preload: Lock im Hauptthread acquirieren BEVOR Connections akzeptiert werden.
    # → Eingehende /api/articles Requests erhalten sofort [] (kein Warten, kein 502)
    # → Sobald Preload fertig, wird Lock freigegeben und Cache ist warm.
    if not NO_SOURCES_STATE and _LOAD_IN_PROGRESS.acquire(blocking=False):
        def _startup_preload_worker():
            try:
                _adobe_src = ADOBE_SOURCE if not _is_fixture(ADOBE_SOURCE) else None
                _rss_src = RSS_SOURCE if not _is_fixture(RSS_SOURCE) else None
                _home_src = HOME_SOURCE if not _is_fixture(HOME_SOURCE) else None
                data, excluded = _do_fetch_articles(True, _adobe_src, _rss_src, _home_src)
                with _CACHE_LOCK:
                    global _CACHE_DATA, _CACHE_EXCLUDED, _CACHE_EXPIRES_AT
                    _CACHE_DATA = data
                    _CACHE_EXCLUDED = excluded
                    _CACHE_EXPIRES_AT = time.monotonic() + (CACHE_SECONDS if CACHE_SECONDS > 0 else 0.0)
                if _ADOBE_AVAILABLE and data and _adobe.get_adobe_status().get("adobeConfigured"):
                    if not _ADOBE_ENRICHMENT_RUNNING.is_set():
                        _ADOBE_ENRICHMENT_RUNNING.set()
                        threading.Thread(
                            target=_run_adobe_enrichment_async, args=(data,), daemon=True,
                        ).start()
            except Exception:
                pass
            finally:
                _LOAD_IN_PROGRESS.release()
        threading.Thread(target=_startup_preload_worker, daemon=True).start()
    server.serve_forever()
