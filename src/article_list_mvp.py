from __future__ import annotations

import csv
import json
import re
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

try:
    import certifi as _certifi
    _SSL_CTX: ssl.SSLContext | None = ssl.create_default_context(cafile=_certifi.where())
except ImportError:
    _SSL_CTX = None

EXCLUDED_WORKFLOW_STATUSES = {"zum verbauen", "redigiert"}

_RESSORT_PATTERNS = {
    "/politik/": "Politik",
    "/sport/": "Sport",
    "/unterhaltung/": "Unterhaltung",
    "/leben-wissen/": "Leben & Wissen",
    "/lifestyle/": "Leben & Wissen",
    "/ratgeber/": "Leben & Wissen",
    "/regional/": "Regional",
    "/news/": "News",
    "/auto/": "Leben & Wissen",
    "/geld/": "Leben & Wissen",
}

ADOBE_WORKFLOW_STATUS_FIELD_ALIASES = (
    "workflow_status",
    "workflow status",
    "workflow-status",
    "workflowStatus",
    "workflowstatus",
    "workflow_state",
    "workflow state",
    "workflow-state",
    "workflowState",
    "status",
    "article_status",
    "article status",
    "article-status",
    "articleStatus",
    "artikel_status",
    "artikel status",
    "artikelStatus",
    "artikelstatus",
    "publication_status",
    "publicationStatus",
    "publishing_status",
    "publishingStatus",
)
RSS_WORKFLOW_STATUS_FIELD_ALIASES = (
    "workflow_status",
    "workflow status",
    "workflow-status",
    "workflowStatus",
    "workflowstatus",
    "workflow_state",
    "workflow state",
    "workflow-state",
    "workflowState",
    "status",
    "article_status",
    "article status",
    "article-status",
    "articleStatus",
    "publication_status",
    "publicationStatus",
    "publishing_status",
    "publishingStatus",
)


@dataclass(slots=True)
class ArticleRecord:
    canonical_url: str
    source_url: str = ""
    cms_id: str = ""
    title: str = ""
    workflow_status: str = ""
    live_readers: int = 0
    home_position: int | None = None
    published_at: datetime | None = None
    rss_guid: str = ""
    source_flags: set[str] = field(default_factory=set)
    workflow_statuses: set[str] = field(default_factory=set, repr=False)
    ressort: str = ""
    urgency_score: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_url": self.canonical_url,
            "source_url": self.source_url,
            "cms_id": self.cms_id,
            "title": self.title,
            "workflow_status": self.workflow_status,
            "live_readers": self.live_readers,
            "home_position": self.home_position,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "source_flags": sorted(self.source_flags),
            "ressort": self.ressort,
            "urgency_score": self.urgency_score,
        }


@dataclass(frozen=True, slots=True)
class ConnectorConfig:
    timeout_seconds: float = 5.0
    max_retries: int = 3
    backoff_seconds: float = 0.5
    backoff_factor: float = 2.0
    max_backoff_seconds: float = 8.0


@dataclass(slots=True)
class IngestRow:
    source: str
    canonical_url: str
    source_url: str = ""
    cms_id: str = ""
    title: str = ""
    workflow_status: str = ""
    live_readers: int = 0
    home_position: int | None = None
    published_at: datetime | None = None
    rss_guid: str = ""
    ressort: str = ""


def _detect_ressort(url: str) -> str:
    lower = (url or "").lower()
    for pattern, ressort in _RESSORT_PATTERNS.items():
        if pattern in lower:
            return ressort
    return ""


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().removeprefix("www.")
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/")
    if not path:
        path = "/"
    return urlunparse(("https", host, path, "", "", ""))


def _extract_cms_id(url: str, fallback_id: str = "") -> str:
    if fallback_id:
        return str(fallback_id)
    if not url:
        return ""
    cmsid_match = re.search(r"cmsid/([a-zA-Z0-9-]+)", url)
    if cmsid_match:
        return cmsid_match.group(1)

    trailing_id_match = re.search(r"-(\d{6,})(?:[/?#]|$)", url)
    if trailing_id_match:
        return trailing_id_match.group(1)
    return ""


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return default


_TZ_ABBREV = {
    "CEST": "+0200", "CET": "+0100", "GMT": "+0000", "UTC": "+0000",
    "EDT": "-0400", "EST": "-0500", "PDT": "-0700", "PST": "-0800",
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    txt = value.strip()
    for abbrev, offset in _TZ_ABBREV.items():
        if txt.endswith(" " + abbrev):
            txt = txt[: -(len(abbrev))] + offset
            break
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",  # RFC822
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(txt, fmt)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _normalize_field_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _pick_first(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return str(row.get(key) or "")

    normalized_row_keys = {_normalize_field_name(k): k for k in row.keys()}
    for key in keys:
        raw_key = normalized_row_keys.get(_normalize_field_name(key))
        if raw_key is None:
            continue
        if row.get(raw_key) not in (None, ""):
            return str(row.get(raw_key) or "")
    return ""


def _xml_local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _xml_item_to_row(item: ET.Element) -> dict[str, str]:
    row: dict[str, str] = {}
    for child in item:
        key = _xml_local_name(child.tag)
        value = (child.text or "").strip()
        if not key or not value:
            continue
        if key not in row:
            row[key] = value

    if "title" not in row:
        row["title"] = (item.findtext("title") or "").strip()
    if "link" not in row:
        row["link"] = (item.findtext("link") or "").strip()
    if "guid" not in row:
        row["guid"] = (item.findtext("guid") or "").strip()
    if "pubDate" not in row:
        row["pubDate"] = (item.findtext("pubDate") or "").strip()

    return row


def _is_http_source(source: Path | str) -> bool:
    return str(source).strip().lower().startswith(("http://", "https://"))


def _is_json_source(source: Path | str) -> bool:
    if _is_http_source(source):
        return str(source).strip().lower().endswith(".json")
    return Path(source).suffix.lower() == ".json"


def _read_with_retry(
    source: Path | str,
    config: ConnectorConfig,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    if not _is_http_source(source):
        return Path(source).read_text(encoding="utf-8")

    url = str(source).strip()
    attempts = max(config.max_retries, 0) + 1
    last_error: Exception | None = None

    for attempt in range(attempts):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Accept": "application/json, application/xml, text/xml;q=0.9, */*;q=0.8",
                    "Accept-Language": "de-DE,de;q=0.9",
                },
            )
            kw = {"context": _SSL_CTX} if _SSL_CTX else {}
            with urlopen(request, timeout=config.timeout_seconds, **kw) as response:
                payload = response.read()
                return payload.decode(response.headers.get_content_charset() or "utf-8")
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
            if attempt >= attempts - 1:
                break
            delay = min(config.backoff_seconds * (config.backoff_factor**attempt), config.max_backoff_seconds)
            if delay > 0:
                sleep_fn(delay)

    raise RuntimeError(f"Connector konnte Quelle nicht laden: {source}") from last_error


def _probe_http_source(
    source: Path | str,
    config: ConnectorConfig,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    url = str(source).strip()
    attempts = max(config.max_retries, 0) + 1
    last_error: Exception | None = None
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, application/xml, text/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9",
    }

    for attempt in range(attempts):
        try:
            try:
                request = Request(url, method="HEAD", headers=headers)
                kw = {"context": _SSL_CTX} if _SSL_CTX else {}
                with urlopen(request, timeout=config.timeout_seconds, **kw) as response:
                    _ = response.status
            except HTTPError as head_error:
                if head_error.code not in {405, 501}:
                    raise
                request = Request(url, method="GET", headers={**headers, "Range": "bytes=0-0"})
                kw = {"context": _SSL_CTX} if _SSL_CTX else {}
                with urlopen(request, timeout=config.timeout_seconds, **kw) as response:
                    response.read(1)
            return
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
            if attempt >= attempts - 1:
                break
            delay = min(config.backoff_seconds * (config.backoff_factor**attempt), config.max_backoff_seconds)
            if delay > 0:
                sleep_fn(delay)

    raise RuntimeError(f"API-Check fehlgeschlagen: {source}") from last_error


def check_live_connector_apis(
    adobe_source: Path | str,
    rss_source: Path | str,
    config: ConnectorConfig | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    cfg = config or ConnectorConfig()
    result = {"adobe": "skipped", "rss": "skipped"}

    if _is_http_source(adobe_source):
        _probe_http_source(adobe_source, config=cfg, sleep_fn=sleep_fn)
        result["adobe"] = "ok"
    if _is_http_source(rss_source):
        _probe_http_source(rss_source, config=cfg, sleep_fn=sleep_fn)
        result["rss"] = "ok"
    return result


def _load_json_source(source: Path | str, config: ConnectorConfig) -> Any:
    text = _read_with_retry(source, config=config)
    return json.loads(text)


def load_adobe(source: Path | str, config: ConnectorConfig | None = None) -> list[dict[str, Any]]:
    cfg = config or ConnectorConfig()
    if _is_json_source(source):
        raw = _load_json_source(source, cfg)
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict) and isinstance(raw.get("items"), list):
            return raw["items"]
        return []

    csv_text = _read_with_retry(source, config=cfg)
    rows: list[dict[str, Any]] = []
    for row in csv.DictReader(csv_text.splitlines()):
        rows.append(dict(row))
    return rows


_SITEMAP_NS = {
    "s": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
}


def _parse_sitemap_xml(xml_text: str) -> list[dict[str, Any]]:
    """Parst Google News Sitemap XML (sitemap-news.xml) in RSS-kompatible Rows."""
    root = ET.fromstring(xml_text)
    local = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    if local not in ("urlset", "sitemapindex"):
        return []
    items: list[dict[str, Any]] = []
    for url_el in root.findall("s:url", _SITEMAP_NS):
        loc = (url_el.findtext("s:loc", "", _SITEMAP_NS) or "").strip()
        if not loc:
            continue
        news_el = url_el.find("news:news", _SITEMAP_NS)
        title = ""
        pub_date = ""
        if news_el is not None:
            title = (news_el.findtext("news:title", "", _SITEMAP_NS) or "").strip()
            pub_date = (news_el.findtext("news:publication_date", "", _SITEMAP_NS) or "").strip()
        if not pub_date:
            pub_date = (url_el.findtext("s:lastmod", "", _SITEMAP_NS) or "").strip()
        items.append({"link": loc, "url": loc, "title": title, "pubDate": pub_date})
    return items


def load_rss(source: Path | str, config: ConnectorConfig | None = None) -> list[dict[str, Any]]:
    cfg = config or ConnectorConfig()
    if _is_json_source(source):
        raw = _load_json_source(source, cfg)
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict) and isinstance(raw.get("items"), list):
            return raw["items"]
        return []

    xml_text = _read_with_retry(source, config=cfg)
    root = ET.fromstring(xml_text)
    local_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    # Sitemap-Format (sitemap-news.xml) statt RSS
    if local_tag in ("urlset", "sitemapindex"):
        return _parse_sitemap_xml(xml_text)
    items: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        items.append(_xml_item_to_row(item))
    return items


def load_home_positions(source: Path | str, config: ConnectorConfig | None = None) -> list[dict[str, Any]]:
    cfg = config or ConnectorConfig()
    raw = _load_json_source(source, cfg)
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        return raw["items"]
    if isinstance(raw, list):
        return raw
    return []


def _norm_status(status: str) -> str:
    compact = (status or "").strip().lower()
    compact = compact.replace("_", " ").replace("-", " ")
    return " ".join(compact.split())


def _record_key(canonical_url: str, cms_id: str) -> str:
    if canonical_url:
        return canonical_url
    if cms_id:
        return f"cms:{cms_id}"
    return ""


def _normalize_adobe_rows(adobe_rows: list[dict[str, Any]]) -> list[IngestRow]:
    normalized: list[IngestRow] = []
    for row in adobe_rows:
        url = str(row.get("url") or row.get("article_url") or row.get("link") or "")
        canonical = canonicalize_url(url)
        cms_id = _extract_cms_id(url, str(row.get("cms_id") or row.get("content_id") or ""))
        if not canonical and not cms_id:
            continue
        normalized.append(
            IngestRow(
                source="adobe",
                canonical_url=canonical,
                source_url=url,
                cms_id=cms_id,
                title=str(row.get("title") or row.get("headline") or ""),
                workflow_status=_pick_first(
                    row,
                    ADOBE_WORKFLOW_STATUS_FIELD_ALIASES,
                ),
                live_readers=_to_int(row.get("live_readers") or row.get("readers") or row.get("active_users")),
                published_at=_parse_dt(str(row.get("published_at") or row.get("publish_time") or "")),
                ressort=_detect_ressort(canonical or url),
            )
        )
    return normalized


def _normalize_rss_rows(rss_items: list[dict[str, Any]]) -> list[IngestRow]:
    normalized: list[IngestRow] = []
    for item in rss_items:
        url = str(item.get("link") or item.get("url") or "")
        canonical = canonicalize_url(url)
        cms_id = _extract_cms_id(url, str(item.get("cms_id") or ""))
        if not canonical and not cms_id:
            continue
        wf = _pick_first(item, RSS_WORKFLOW_STATUS_FIELD_ALIASES)
        if not wf:
            premium_raw = str(item.get("premium") or "").strip().lower()
            if premium_raw == "true":
                wf = "BILD+"
            elif premium_raw == "false":
                wf = "Frei"
        normalized.append(
            IngestRow(
                source="rss",
                canonical_url=canonical,
                source_url=url,
                cms_id=cms_id,
                title=str(item.get("title") or ""),
                workflow_status=wf,
                rss_guid=str(item.get("guid") or ""),
                published_at=_parse_dt(str(item.get("pubDate") or item.get("published_at") or "")),
                ressort=_detect_ressort(canonical or url),
            )
        )
    return normalized


def _normalize_home_rows(home_rows: list[dict[str, Any]]) -> list[IngestRow]:
    normalized: list[IngestRow] = []
    for row in home_rows:
        url = str(row.get("url") or row.get("link") or "")
        canonical = canonicalize_url(url)
        cms_id = _extract_cms_id(url, str(row.get("cms_id") or ""))
        if not canonical and not cms_id:
            continue
        pos = _to_int(row.get("position") or row.get("home_position"), default=-1)
        normalized.append(
            IngestRow(
                source="home",
                canonical_url=canonical,
                source_url=url,
                cms_id=cms_id,
                home_position=pos if pos > 0 else None,
            )
        )
    return normalized


def _merge_ingest_rows(rows: list[IngestRow]) -> list[ArticleRecord]:
    records: dict[str, ArticleRecord] = {}
    for row in rows:
        key = _record_key(row.canonical_url, row.cms_id)
        if not key:
            continue
        rec = records.get(key) or ArticleRecord(canonical_url=row.canonical_url)
        rec.source_url = rec.source_url or row.source_url
        rec.cms_id = rec.cms_id or row.cms_id
        rec.title = rec.title or row.title
        rec.workflow_status = rec.workflow_status or row.workflow_status
        normalized_status = _norm_status(row.workflow_status)
        if normalized_status:
            rec.workflow_statuses.add(normalized_status)
        rec.live_readers = max(rec.live_readers, row.live_readers)
        if row.home_position is not None:
            rec.home_position = row.home_position if rec.home_position is None else min(rec.home_position, row.home_position)
        rec.published_at = rec.published_at or row.published_at
        rec.rss_guid = rec.rss_guid or row.rss_guid
        rec.source_flags.add(row.source)
        rec.ressort = rec.ressort or row.ressort
        records[key] = rec
    return list(records.values())


def build_prioritized_article_list(
    adobe_rows: list[dict[str, Any]],
    rss_items: list[dict[str, Any]],
    home_rows: list[dict[str, Any]],
) -> list[ArticleRecord]:
    merged = _merge_ingest_rows(
        _normalize_adobe_rows(adobe_rows)
        + _normalize_rss_rows(rss_items)
        + _normalize_home_rows(home_rows)
    )

    filtered = [
        rec
        for rec in merged
        if rec.workflow_statuses.isdisjoint(EXCLUDED_WORKFLOW_STATUSES)
        and ("rss" in rec.source_flags or "adobe" in rec.source_flags)
    ]

    def _sort_key(rec: ArticleRecord) -> tuple[int, int, float]:
        home = rec.home_position if rec.home_position is not None else 10**9
        readers = -rec.live_readers
        ts = -(rec.published_at.timestamp() if rec.published_at else 0.0)
        return (home, readers, ts)

    filtered.sort(key=_sort_key)

    for rec in filtered:
        pos = rec.home_position
        home_score = max(0, 100 - (pos - 1) * 5) if pos is not None else 0
        readers_score = min(100, rec.live_readers / 100)
        rec.urgency_score = round(0.6 * home_score + 0.4 * readers_score)

    return filtered


def run_mvp(
    adobe_file: Path | str | None,
    rss_file: Path | str | None,
    home_file: Path | str | None,
    out_file: Path | None = None,
    connector_config: ConnectorConfig | None = None,
    api_check: bool = False,
) -> list[dict[str, Any]]:
    cfg = connector_config or ConnectorConfig()
    if api_check and adobe_file and rss_file:
        check_live_connector_apis(adobe_file, rss_file, config=cfg)

    adobe = load_adobe(adobe_file, config=cfg) if adobe_file else []
    rss = load_rss(rss_file, config=cfg) if rss_file else []
    home = load_home_positions(home_file, config=cfg) if home_file else []

    ranked = build_prioritized_article_list(adobe, rss, home)
    result = [r.to_dict() for r in ranked]

    if out_file:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    return result
