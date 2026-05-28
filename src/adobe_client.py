"""
Adobe Analytics Adapter für tc-artikelliste.

Stellt serverseitigen OAuth2-Token-Flow und Analytics-Abfragen bereit.
Secrets werden ausschließlich aus ENV-Vars gelesen und niemals geloggt oder zurückgegeben.

ENV-Vars:
  ADOBE_CLIENT_ID          – Client ID (nicht geheim, darf in Status erscheinen)
  ADOBE_CLIENT_SECRET      – Pflicht für Token; niemals loggen/ausgeben
  ADOBE_GLOBAL_COMPANY_ID  – Analytics Company (default: axelsp2)
  ADOBE_RSID               – Report Suite ID (default: axelspringerbild)
  ADOBE_TOKEN_URL          – OAuth-Token-Endpoint
  ADOBE_ANALYTICS_BASE_URL – Analytics API Base
  ADOBE_DATE_RANGE_DAYS    – Lookback-Fenster in Tagen für Conversions (default: 7)
  ADOBE_LIVE_READERS_HOURS – Zeitfenster für Live-Leser in Stunden (default: 2)
  ADOBE_LIVE_READERS_METRIC – Metric ID für Live-Leser (default: metrics/pageviews)
  ADOBE_CONVERSION_METRIC  – Metric ID für BILDplus-Abos (default: metrics/event60)
  ADOBE_ARTICLE_DIMENSION  – Dimension für Artikel-URL (default: variables/prop21 = Document URL)
"""
from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

_ADOBE_CLIENT_ID = os.environ.get("ADOBE_CLIENT_ID", "").strip()
_ADOBE_CLIENT_SECRET = os.environ.get("ADOBE_CLIENT_SECRET", "").strip()
_ADOBE_GLOBAL_COMPANY_ID = os.environ.get("ADOBE_GLOBAL_COMPANY_ID", "axelsp2").strip()
_ADOBE_RSID = os.environ.get("ADOBE_RSID", "axelspringerbild").strip()
_ADOBE_TOKEN_URL = os.environ.get("ADOBE_TOKEN_URL", "https://ims-na1.adobelogin.com/ims/token/v3").strip()
_ADOBE_BASE_URL = os.environ.get("ADOBE_ANALYTICS_BASE_URL", "https://analytics.adobe.io").strip()
_ADOBE_DATE_RANGE_DAYS = max(1, int(os.environ.get("ADOBE_DATE_RANGE_DAYS", "7") or "7"))
_ADOBE_LIVE_READERS_METRIC = os.environ.get("ADOBE_LIVE_READERS_METRIC", "metrics/pageviews").strip()
_ADOBE_CONVERSION_METRIC = os.environ.get("ADOBE_CONVERSION_METRIC", "metrics/event60").strip()
# prop21 = Document URL (vollständige URL pro Seitenaufruf, präzise für Artikel-Matching)
# evar12 = Page Headline — NICHT für URL-Matching geeignet
_ADOBE_ARTICLE_DIMENSION = os.environ.get("ADOBE_ARTICLE_DIMENSION", "variables/prop21").strip()
# Zeitfenster für Live-Leser: letzte N Stunden (default 2h — "live" Daten)
_ADOBE_LIVE_READERS_HOURS = max(1, int(os.environ.get("ADOBE_LIVE_READERS_HOURS", "2") or "2"))

_token_lock = threading.Lock()
_token_cache: dict[str, Any] = {"token": "", "expires_at": 0.0}
_last_error: str = ""
_last_success_ts: float = 0.0


def _is_configured() -> bool:
    return bool(_ADOBE_CLIENT_ID and _ADOBE_CLIENT_SECRET)


def _is_mapping_complete() -> bool:
    return bool(_ADOBE_GLOBAL_COMPANY_ID and _ADOBE_RSID and _ADOBE_ARTICLE_DIMENSION)


def _mask(value: str) -> str:
    if not value:
        return ""
    return value[:6] + "..." + value[-4:] if len(value) > 10 else value[:3] + "..."


def get_adobe_status() -> dict[str, Any]:
    """Konfigurationsstatus ohne Secrets — sicher für /healthz."""
    global _last_error, _last_success_ts
    token_status = "not_configured"
    if _is_configured():
        with _token_lock:
            if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
                token_status = "ok"
            elif _last_error:
                token_status = "error"
            else:
                token_status = "configured_untested"

    missing: list[str] = []
    if not _ADOBE_GLOBAL_COMPANY_ID:
        missing.append("ADOBE_GLOBAL_COMPANY_ID")
    if not _ADOBE_RSID:
        missing.append("ADOBE_RSID")
    if not _ADOBE_ARTICLE_DIMENSION:
        missing.append("ADOBE_ARTICLE_DIMENSION")
    if not _ADOBE_CONVERSION_METRIC:
        missing.append("ADOBE_CONVERSION_METRIC")

    return {
        "adobeConfigured": _is_configured() and _is_mapping_complete(),
        "adobeClientIdPresent": bool(_ADOBE_CLIENT_ID),
        "adobeClientIdMasked": _mask(_ADOBE_CLIENT_ID),
        "adobeClientSecretPresent": bool(_ADOBE_CLIENT_SECRET),
        "adobeImsOrgIdPresent": bool(os.environ.get("ADOBE_IMS_ORG_ID", "")),
        "adobeImsOrgId": os.environ.get("ADOBE_IMS_ORG_ID", ""),
        "adobeGlobalCompanyIdPresent": bool(_ADOBE_GLOBAL_COMPANY_ID),
        "adobeGlobalCompanyId": _ADOBE_GLOBAL_COMPANY_ID,
        "adobeReportSuiteIdPresent": bool(_ADOBE_RSID),
        "adobeReportSuiteId": _ADOBE_RSID,
        "adobeArticleDimensionPresent": bool(_ADOBE_ARTICLE_DIMENSION),
        "adobeArticleDimension": _ADOBE_ARTICLE_DIMENSION,
        "adobeConversionMetricPresent": bool(_ADOBE_CONVERSION_METRIC),
        "adobeConversionMetric": _ADOBE_CONVERSION_METRIC,
        "adobeLiveReadersHours": _ADOBE_LIVE_READERS_HOURS,
        "adobeTokenStatus": token_status,
        "adobeLastSuccessfulRequestAt": _last_success_ts or None,
        "adobeLastError": _last_error or None,
        "adobeMappingComplete": _is_mapping_complete(),
        "adobeMissingMappingKeys": missing,
        "usingRealAdobeData": _is_configured() and _is_mapping_complete() and token_status == "ok",
    }


def get_access_token(force: bool = False) -> str:
    """OAuth2 Server-to-Server Token — gecacht, automatisch erneuert."""
    global _last_error, _last_success_ts
    if not _is_configured():
        raise RuntimeError("ADOBE_CLIENT_SECRET nicht konfiguriert")

    with _token_lock:
        now = time.time()
        if not force and _token_cache["token"] and now < _token_cache["expires_at"] - 60:
            return _token_cache["token"]

        payload = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": _ADOBE_CLIENT_ID,
            "client_secret": _ADOBE_CLIENT_SECRET,
            "scope": os.environ.get("ADOBE_SCOPES", "openid,AdobeID,additional_info.projectedProductContext"),
        }).encode()
        req = urllib.request.Request(
            _ADOBE_TOKEN_URL, data=payload, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                data = json.loads(resp.read())
            token = data["access_token"]
            expires_in = int(data.get("expires_in", 3600))
            _token_cache["token"] = token
            _token_cache["expires_at"] = time.time() + expires_in
            _last_success_ts = time.time()
            _last_error = ""
            return token
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raw_msg = f"HTTP {exc.code} {exc.reason}: {body}"
            _last_error = raw_msg.replace(_ADOBE_CLIENT_SECRET, "***") if _ADOBE_CLIENT_SECRET else raw_msg
            raise RuntimeError(f"Adobe Token-Fehler: {_last_error}") from exc
        except Exception as exc:
            _last_error = str(exc).replace(_ADOBE_CLIENT_SECRET, "***") if _ADOBE_CLIENT_SECRET else str(exc)
            raise RuntimeError(f"Adobe Token-Fehler: {_last_error}") from exc


def _request(method: str, path: str, body: dict | None = None) -> Any:
    """HTTP-Request gegen Adobe Analytics API mit automatischem Token-Retry."""
    global _last_error, _last_success_ts
    token = get_access_token()
    url = f"{_ADOBE_BASE_URL.rstrip('/')}/api/{_ADOBE_GLOBAL_COMPANY_ID}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-key": _ADOBE_CLIENT_ID,
        "x-proxy-global-company-id": _ADOBE_GLOBAL_COMPANY_ID,
        "Accept": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
            result = json.loads(resp.read())
            _last_success_ts = time.time()
            _last_error = ""
            return result
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            token = get_access_token(force=True)
            headers["Authorization"] = f"Bearer {token}"
            req2 = urllib.request.Request(url, data=data, method=method, headers=headers)
            with urllib.request.urlopen(req2, timeout=30, context=_SSL_CTX) as resp:
                result = json.loads(resp.read())
                _last_success_ts = time.time()
                _last_error = ""
                return result
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        _last_error = f"HTTP {exc.code} {exc.reason}: {body}"
        raise


def get_global_companies() -> list[dict]:
    """Listet verfügbare Adobe Analytics Companies — für Admin/Debug."""
    if not _is_configured():
        return []
    return _request("GET", "/") or []


def get_report_suites() -> list[dict]:
    """Listet Report Suites für die konfigurierte Company — für Admin/Debug."""
    if not _is_configured():
        return []
    result = _request("GET", "/collections/suites?limit=100") or {}
    return result.get("content", [])


_ARTICLE_PATH_EXCLUDES = {"/adblockwall.html", "/", ""}
_ARTICLE_PATH_PREFIXES = (
    "/news/", "/politik/", "/sport/", "/unterhaltung/", "/regional/",
    "/leben-wissen/", "/ratgeber/", "/auto/", "/geld/", "/lifestyle/",
    "/bild-plus/", "/bildplus/",
)
# Slug-Patterns die Sektions-Seiten oder alte Formate anzeigen (keine echten Artikel)
_SECTION_PAGE_PATTERNS = (
    "startseite", "-home", "home-", "/home",  # Sektions-Startseiten
)


def _is_article_path(path: str) -> bool:
    """Prüft ob der Pfad ein echter Artikel ist (keine Sektions-Seite)."""
    lower = path.lower()
    # Alte URL-Formate: .bild.html, .bildmobile, .bild
    if any(lower.endswith(s) for s in (".bild.html", ".bildmobile", ".bild", ".html.bild")):
        return False
    # Sektions-Pfadsegment: /startseite/ oder /home/
    if "/startseite/" in lower or "/startseite" == lower.rstrip("/")[-11:]:
        return False
    last = path.rstrip("/").split("/")[-1]
    # Numerische Artikel-IDs (altes Format: "15479124.bild")
    import re as _re2
    if _re2.match(r"^\d{6,}[.\-]", last):
        return False
    # Sehr kurze Slugs = wahrscheinlich Sektions-Seite
    parts = [p for p in last.split("-") if len(p) > 2]
    if len(parts) < 2:
        return False
    # Sektions-Startseite-Muster
    if any(p in lower for p in _SECTION_PAGE_PATTERNS):
        return False
    return True


def fetch_top_article_urls(
    n: int = 100,
    hours: int | None = None,
) -> list[dict[str, object]]:
    """
    Gibt die Top-n Artikel aus Adobe (nach Pageviews) zurück.
    Nutzt prop21-Dimension, aggregiert m.bild.de + www.bild.de.
    Liefert [] wenn nicht konfiguriert.

    Rückgabe: [{"canonical_url": str, "live_readers": int, "home_position": None}]
    """
    if not _is_configured() or not _is_mapping_complete():
        return []
    look = hours or _ADOBE_LIVE_READERS_HOURS
    now_ts = time.time()
    start = time.strftime("%Y-%m-%dT%H:%M:%S.000", time.localtime(now_ts - look * 3600))
    end = time.strftime("%Y-%m-%dT%H:%M:%S.000", time.localtime(now_ts))
    body = {
        "rsid": _ADOBE_RSID,
        "globalFilters": [{"type": "dateRange", "dateRange": f"{start}/{end}"}],
        "metricContainer": {"metrics": [{"columnId": "0", "id": _ADOBE_LIVE_READERS_METRIC}]},
        "dimension": _ADOBE_ARTICLE_DIMENSION,
        "settings": {"countRepeatInstances": True, "limit": n * 3, "nonesBehavior": "exclude-nones"},
    }
    result = _request("POST", "/reports?locale=de_DE", body)
    rows = result.get("rows", [])

    path_to_pv: dict[str, int] = {}
    path_to_sample_url: dict[str, str] = {}
    for row in rows:
        dim = (row.get("value") or "").strip()
        if not dim or dim == "(Low Traffic)":
            continue
        try:
            from urllib.parse import urlparse as _up
            parsed = _up(dim)
            path = parsed.path.rstrip("/").lower()
        except Exception:
            continue
        if not path or path in _ARTICLE_PATH_EXCLUDES:
            continue
        if not any(path.startswith(p) for p in _ARTICLE_PATH_PREFIXES):
            continue
        if not _is_article_path(path):
            continue
        val = row.get("data", [0])
        pv = int(val[0]) if val and val[0] is not None else 0
        path_to_pv[path] = path_to_pv.get(path, 0) + pv
        if path not in path_to_sample_url:
            path_to_sample_url[path] = dim

    # Schritt 2: Titel aus evar220 (Page ID – Headline)
    id_to_headline: dict[str, str] = {}
    try:
        # evar220 mit 24h-Fenster für mehr page_id-Matches (auch ältere Top-Artikel)
        evar220_start = time.strftime("%Y-%m-%dT%H:%M:%S.000", time.localtime(now_ts - 24 * 3600))
        body_evar220 = {
            "rsid": _ADOBE_RSID,
            "globalFilters": [{"type": "dateRange", "dateRange": f"{evar220_start}/{end}"}],
            "metricContainer": {"metrics": [{"columnId": "0", "id": _ADOBE_LIVE_READERS_METRIC}]},
            "dimension": "variables/evar220",
            "settings": {"countRepeatInstances": True, "limit": 1000, "nonesBehavior": "exclude-nones"},
        }
        evar220_result = _request("POST", "/reports?locale=de_DE", body_evar220)
        for row in evar220_result.get("rows", []):
            val = (row.get("value") or "").strip()
            if " – " in val:
                page_id, headline = val.split(" – ", 1)
                page_id = page_id.strip()
                headline = headline.strip()
                if headline and page_id:
                    id_to_headline[page_id] = headline
    except Exception:
        pass

    import re as _re
    _ID_RE = _re.compile(r"([0-9a-f]{24})")
    _STOP = {"die", "der", "das", "ein", "eine", "einen", "dem", "den", "des",
             "und", "oder", "fuer", "mit", "von", "zu", "bei", "in", "an",
             "auf", "im", "am", "aus", "hat", "ist", "es", "er", "sie",
             "ich", "wir", "wie", "was", "wer", "so", "nach", "zum", "zur",
             "auch", "nur", "noch", "jetzt", "schon", "sich", "als", "neue",
             "neuen", "ueber", "vor", "nach", "ab", "this", "the", "a", "for"}

    def _slug_words(path: str) -> set[str]:
        last = path.rstrip("/").split("/")[-1]
        raw = _re.sub(r"[0-9a-f]{8,}", "", last)
        parts = [w for w in _re.split(r"[-_]", raw) if len(w) > 2 and w not in _STOP]
        return set(parts)

    def _headline_words(headline: str) -> set[str]:
        words = _re.findall(r"[a-züäöß]{3,}", headline.lower())
        result = set()
        for w in words:
            if w not in _STOP:
                # ASCII-Normalisierung für Matching mit URL-Slugs (traegt = trägt)
                w_ascii = w.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
                result.add(w_ascii)
        return result

    # Headline-Word-Index für schnelles Matching
    headline_entries = [(page_id, headline, _headline_words(headline))
                        for page_id, headline in id_to_headline.items()]

    _ASCII_TO_DE = {"ae": "ä", "oe": "ö", "ue": "ü", "Ae": "Ä", "Oe": "Ö", "Ue": "Ü",
                    "fuer": "für", "ueber": "über", "ss": "ß"}

    def _title_from_slug(path: str) -> str:
        """Fallback-Titel aus URL-Slug: bereinigt, kapitalisiert, Umlaute repariert."""
        last = path.rstrip("/").split("/")[-1]
        # Trailing IDs und letztes unvollständiges Wort entfernen
        clean = _re.sub(r"-[0-9a-f]{5,}[^a-z]*$", "", last)
        clean = _re.sub(r"\.html$", "", clean)
        # Letztes Wort entfernen wenn Pfad truncated war (endet nicht sauber mit Bindestr.)
        if len(path) >= 79 and not path.endswith("-"):
            # Truncated — letztes möglicherweise unvollständiges Wort abschneiden
            parts = clean.rsplit("-", 1)
            if len(parts) == 2 and len(parts[1]) < 5:
                clean = parts[0]
        words = [w for w in clean.split("-") if w]
        # ASCII-Umlaute reparieren und kapitalisieren
        result_words = []
        for w in words:
            w_lower = w.lower()
            w = _ASCII_TO_DE.get(w_lower, w)
            result_words.append(w[:1].upper() + w[1:] if w else "")
        return " ".join(result_words) if result_words else ""

    def _best_headline(path: str) -> tuple[str, str]:
        """Gibt (page_id, headline) mit bestem Slug-Wort-Match zurück.
        Fallback: lesbarer Titel aus URL-Slug."""
        # Direkter ID-Match zuerst
        m = _ID_RE.search(path)
        if m:
            direct = id_to_headline.get(m.group(1), "")
            if direct:
                return m.group(1), direct
        # Wort-Matching zwischen URL-Slug und Headline
        slug_w = _slug_words(path)
        if slug_w:
            best_id, best_hl, best_score = "", "", 0
            for pid, hl, hl_words in headline_entries:
                if not hl_words:
                    continue
                # Direkte Überlappung
                direct = len(slug_w & hl_words)
                # Compound-Wort: slug hat "luxusketten", headline hat "ketten" → endswith-Match
                compound = sum(
                    1 for s in slug_w for h in hl_words
                    if len(h) >= 5 and s != h and s.endswith(h)
                )
                overlap = direct + compound
                if overlap >= 2 and overlap > best_score:
                    best_score = overlap
                    best_id, best_hl = pid, hl
            if best_hl:
                return best_id, best_hl
        # Fallback: Titel aus URL-Slug generieren
        return "", _title_from_slug(path)

    # Deduplizierung: m.bild.de (82 Zeichen Pfad) vs www.bild.de (80 Zeichen Pfad)
    # → selber Artikel erscheint mit 2 unterschiedlich langen Truncations
    # Strategie: kürzerer Pfad ist Prefix des längeren → merge
    sorted_paths = sorted(path_to_pv.keys(), key=len)  # kürzeste zuerst
    merged: dict[str, int] = {}   # längster_pfad → gesamt_pv
    _used: set[str] = set()
    for short_path in sorted_paths:
        if short_path in _used:
            continue
        total_pv = path_to_pv[short_path]
        best_path = short_path
        for long_path, long_pv in path_to_pv.items():
            if long_path == short_path or long_path in _used:
                continue
            if long_path.startswith(short_path) and len(short_path) >= 40:
                total_pv += long_pv
                best_path = long_path  # längeren Pfad bevorzugen
                _used.add(long_path)
        _used.add(short_path)
        merged[best_path] = merged.get(best_path, 0) + total_pv
    merged_pv = merged

    # Kanonisieren: bild.de ohne www/m
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    for path, pv in sorted(merged_pv.items(), key=lambda x: -x[1])[:n]:
        canonical = f"https://bild.de{path}"
        if canonical in seen:
            continue
        seen.add(canonical)
        article_page_id, title = _best_headline(path)
        # Publikationsdatum aus MongoDB ObjectID: erste 4 Bytes = Unix-Timestamp
        # article_page_id (aus evar220-Match) ist vollständig, URL-ID oft truncated
        import datetime as _dt
        pub_date: str | None = None
        url_id_match = _re.search(r"([0-9a-f]{24})", canonical)
        for id_src in [article_page_id, url_id_match.group(1) if url_id_match else ""]:
            if len(id_src) >= 8:
                try:
                    ts = int(id_src[:8], 16)
                    if 1600000000 < ts < 2000000000:  # 2020-2033
                        pub_date = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()
                        break
                except Exception:
                    pass

        # source_url: CMS-ID-URL wenn page_id bekannt (umgeht truncated URL),
        # sonst Adobe prop21 sample URL (vollständiger als kanonisierter Pfad)
        if article_page_id:
            source_url = f"https://www.bild.de/cmsid/{article_page_id}"
        else:
            sample = path_to_sample_url.get(path, "")
            source_url = sample if sample else canonical

        out.append({
            "canonical_url": canonical,
            "source_url": source_url,
            "live_readers": pv,
            "home_position": None,
            "title": title,
            "published_at": pub_date,
            "workflow_status": "Frei",
        })
    return out


def fetch_premium_status(canonical_urls: list[str]) -> dict[str, str]:
    """
    Ermittelt BILD+/Frei Status für Artikel via evar20 (Page Premium Status).
    Gibt {canonical_url: "BILD+"} für Premium-Artikel zurück.
    Nicht enthaltene Artikel = "Frei" (Standardwert).
    """
    if not _is_configured() or not _is_mapping_complete():
        return {}
    now_ts = time.time()
    start = time.strftime("%Y-%m-%dT%H:%M:%S.000", time.localtime(now_ts - 24 * 3600))
    end = time.strftime("%Y-%m-%dT%H:%M:%S.000", time.localtime(now_ts))

    # evar20 itemId für "true" (BILD+) holen
    body_evar20 = {
        "rsid": _ADOBE_RSID,
        "globalFilters": [{"type": "dateRange", "dateRange": f"{start}/{end}"}],
        "metricContainer": {"metrics": [{"columnId": "0", "id": _ADOBE_LIVE_READERS_METRIC}]},
        "dimension": "variables/evar20",
        "settings": {"countRepeatInstances": True, "limit": 5, "nonesBehavior": "exclude-nones"},
    }
    evar20_result = _request("POST", "/reports?locale=de_DE", body_evar20)
    premium_item_id = next(
        (str(row.get("itemId", "")) for row in evar20_result.get("rows", [])
         if str(row.get("value", "")).lower() in ("true", "1", "yes", "bild+", "premium")),
        None,
    )
    if not premium_item_id:
        return {}

    # prop21 gefiltert nach evar20=true → BILD+-Artikel-URLs
    body_premium = {
        "rsid": _ADOBE_RSID,
        "globalFilters": [
            {"type": "dateRange", "dateRange": f"{start}/{end}"},
            {"type": "breakdown", "dimension": "variables/evar20", "itemIds": [premium_item_id]},
        ],
        "metricContainer": {"metrics": [{"columnId": "0", "id": _ADOBE_LIVE_READERS_METRIC}]},
        "dimension": _ADOBE_ARTICLE_DIMENSION,
        "settings": {"countRepeatInstances": True, "limit": 50000, "nonesBehavior": "exclude-nones"},
    }
    premium_result = _request("POST", "/reports?locale=de_DE", body_premium)
    premium_paths: set[str] = set()
    for row in premium_result.get("rows", []):
        dim = (row.get("value") or "").strip()
        if not dim or dim == "(Low Traffic)":
            continue
        path = _extract_path(dim)
        if path and path != "/":
            premium_paths.add(path)

    out: dict[str, str] = {}
    for canonical_url in canonical_urls:
        path = _extract_path(canonical_url)
        if not path:
            continue
        if path in premium_paths:
            out[canonical_url] = "BILD+"
            continue
        for adobe_path in premium_paths:
            if len(adobe_path) >= 50 and path.startswith(adobe_path):
                out[canonical_url] = "BILD+"
                break
    return out


def fetch_home_positions(canonical_urls: list[str]) -> dict[str, int]:
    """
    Leitet Homepage-Positionen aus Adobe evar97 (Teaser Block Info) ab.
    - evar97=aufmacher → Position 1 (Aufmacher)
    - evar97=aufmacherbereich → Positionen 2-5
    Gibt {canonical_url: position} zurück. Liefert {} wenn nicht konfiguriert.
    """
    if not _is_configured() or not _is_mapping_complete():
        return {}
    now_ts = time.time()
    start = time.strftime("%Y-%m-%dT%H:%M:%S.000", time.localtime(now_ts - 1 * 3600))
    end = time.strftime("%Y-%m-%dT%H:%M:%S.000", time.localtime(now_ts))

    # Schritt 1: evar97 itemIds für "aufmacher" und "aufmacherbereich" holen
    body_ids = {
        "rsid": _ADOBE_RSID,
        "globalFilters": [{"type": "dateRange", "dateRange": f"{start}/{end}"}],
        "metricContainer": {"metrics": [{"columnId": "0", "id": _ADOBE_LIVE_READERS_METRIC}]},
        "dimension": "variables/evar97",
        "settings": {"countRepeatInstances": True, "limit": 30, "nonesBehavior": "exclude-nones"},
    }
    evar97_result = _request("POST", "/reports?locale=de_DE", body_ids)
    evar97_item_ids: dict[str, str] = {
        row.get("value", ""): str(row.get("itemId", ""))
        for row in evar97_result.get("rows", [])
    }

    blocks = [
        ("aufmacher", 1, 1),         # Block, Start-Position, Max Artikel
        ("aufmacherbereich", 2, 4),
    ]

    path_to_position: dict[str, int] = {}
    pos_counter = 1

    for block_name, start_pos, max_items in blocks:
        item_id = evar97_item_ids.get(block_name)
        if not item_id:
            continue
        body_block = {
            "rsid": _ADOBE_RSID,
            "globalFilters": [
                {"type": "dateRange", "dateRange": f"{start}/{end}"},
                {"type": "breakdown", "dimension": "variables/evar97", "itemIds": [item_id]},
            ],
            "metricContainer": {"metrics": [{"columnId": "0", "id": _ADOBE_LIVE_READERS_METRIC}]},
            "dimension": _ADOBE_ARTICLE_DIMENSION,
            "settings": {"countRepeatInstances": True, "limit": max_items, "nonesBehavior": "exclude-nones"},
        }
        try:
            block_result = _request("POST", "/reports?locale=de_DE", body_block)
        except Exception:
            continue
        for idx, row in enumerate(block_result.get("rows", [])[:max_items]):
            dim = (row.get("value") or "").strip()
            if not dim or dim == "(Low Traffic)":
                continue
            path = _extract_path(dim)
            if path and path not in path_to_position:
                path_to_position[path] = start_pos + idx

    # Canonical URLs über Pfad zuordnen
    out: dict[str, int] = {}
    for canonical_url in canonical_urls:
        path = _extract_path(canonical_url)
        if not path:
            continue
        if path in path_to_position:
            out[canonical_url] = path_to_position[path]
            continue
        # Präfix-Match für truncated prop21 URLs
        for adobe_path, pos in path_to_position.items():
            if len(adobe_path) >= 50 and path.startswith(adobe_path):
                out[canonical_url] = pos
                break
    return out


def _days_ago(n: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - n * 86400))


def _extract_path(url: str) -> str:
    """Extrahiert normalisierten Pfad für URL-Matching (ohne Domain, Query, Fragment)."""
    from urllib.parse import urlparse
    try:
        p = urlparse(url.strip())
        path = p.path.rstrip("/") or "/"
        return path.lower()
    except Exception:
        return ""


def fetch_live_readers(canonical_urls: list[str]) -> dict[str, int]:
    """
    Gibt {canonical_url: pageviews_last_N_hours} zurück.
    Matching über URL-Pfad (domain-agnostisch) für www vs m.bild.de Robustheit.
    Liefert {} wenn nicht konfiguriert — niemals Fake-Werte.
    """
    if not _is_configured() or not _is_mapping_complete():
        return {}
    now_ts = time.time()
    start_ts = now_ts - _ADOBE_LIVE_READERS_HOURS * 3600
    start = time.strftime("%Y-%m-%dT%H:%M:%S.000", time.localtime(start_ts))
    end = time.strftime("%Y-%m-%dT%H:%M:%S.000", time.localtime(now_ts))
    body = {
        "rsid": _ADOBE_RSID,
        "globalFilters": [{"type": "dateRange", "dateRange": f"{start}/{end}"}],
        "metricContainer": {"metrics": [{"columnId": "0", "id": _ADOBE_LIVE_READERS_METRIC}]},
        "dimension": _ADOBE_ARTICLE_DIMENSION,
        "settings": {"countRepeatInstances": True, "limit": 50000, "nonesBehavior": "exclude-nones"},
    }
    result = _request("POST", "/reports?locale=de_DE", body)
    rows = result.get("rows", [])

    # Path-Map aus Adobe-Ergebnissen aufbauen (Domain-agnostisch)
    path_to_pageviews: dict[str, int] = {}
    for row in rows:
        dim = (row.get("value") or "").strip()
        if not dim or dim == "(Low Traffic)":
            continue
        path = _extract_path(dim)
        if not path or path == "/":
            continue
        val = row.get("data", [0])
        pv = int(val[0]) if val and val[0] is not None else 0
        # m.bild.de + www.bild.de haben denselben Pfad — addieren
        path_to_pageviews[path] = path_to_pageviews.get(path, 0) + pv

    # Canonical URLs über Pfad zuordnen (prop21 wird auf 100 Zeichen total truncated →
    # Adobe-Pfad ist häufig ein Präfix des vollen Artikel-Pfades)
    out: dict[str, int] = {}
    for canonical_url in canonical_urls:
        path = _extract_path(canonical_url)
        if not path:
            continue
        # Exakter Match zuerst
        if path in path_to_pageviews:
            out[canonical_url] = path_to_pageviews[path]
            continue
        # Präfix-Match: Adobe-Pfad kann truncated sein → canonical startswith adobe_path
        for adobe_path, pv in path_to_pageviews.items():
            if len(adobe_path) >= 50 and path.startswith(adobe_path):
                out[canonical_url] = pv
                break
    return out


def test_auth() -> dict[str, object]:
    """Testet Adobe-Auth und eine minimale Analytics-Abfrage. Sicher: keine Secrets in Ausgabe."""
    result: dict[str, object] = {
        "configured": _is_configured() and _is_mapping_complete(),
        "tokenOk": False,
        "tokenError": None,
        "apiOk": False,
        "apiError": None,
        "apiRowCount": None,
        "tokenUrl": _ADOBE_TOKEN_URL,
        "globalCompanyId": _ADOBE_GLOBAL_COMPANY_ID,
        "reportSuiteId": _ADOBE_RSID,
        "articleDimension": _ADOBE_ARTICLE_DIMENSION,
    }
    if not _is_configured():
        result["tokenError"] = "ADOBE_CLIENT_SECRET nicht gesetzt"
        return result
    try:
        get_access_token(force=True)
        result["tokenOk"] = True
    except Exception as exc:
        result["tokenError"] = str(exc)
        return result
    # Minimale Analytics-Abfrage: einen URL-Eintrag aus letzter Stunde holen
    try:
        now_ts = time.time()
        start = time.strftime("%Y-%m-%dT%H:%M:%S.000", time.localtime(now_ts - 3600))
        end = time.strftime("%Y-%m-%dT%H:%M:%S.000", time.localtime(now_ts))
        body = {
            "rsid": _ADOBE_RSID,
            "globalFilters": [{"type": "dateRange", "dateRange": f"{start}/{end}"}],
            "metricContainer": {"metrics": [{"columnId": "0", "id": _ADOBE_LIVE_READERS_METRIC}]},
            "dimension": _ADOBE_ARTICLE_DIMENSION,
            "settings": {"countRepeatInstances": True, "limit": 1, "nonesBehavior": "exclude-nones"},
        }
        resp = _request("POST", "/reports?locale=de_DE", body)
        rows = resp.get("rows", [])
        result["apiOk"] = True
        result["apiRowCount"] = len(rows)
    except Exception as exc:
        result["apiError"] = str(exc)
    return result


def fetch_actual_conversions(canonical_urls: list[str], days: int | None = None) -> dict[str, int | None]:
    """
    Gibt {canonical_url: actual_conversions} zurück.
    None = Daten nicht verfügbar, niemals Fake-Werte.
    Nur wenn ADOBE_CONVERSION_METRIC konfiguriert ist.
    """
    if not _is_configured() or not _is_mapping_complete():
        return {url: None for url in canonical_urls}
    lookback = days or _ADOBE_DATE_RANGE_DAYS
    start = _days_ago(lookback)
    end = _days_ago(0)
    body = {
        "rsid": _ADOBE_RSID,
        "globalFilters": [{"type": "dateRange", "dateRange": f"{start}T00:00:00.000/{end}T23:59:59.000"}],
        "metricContainer": {"metrics": [{"columnId": "0", "id": _ADOBE_CONVERSION_METRIC}]},
        "dimension": _ADOBE_ARTICLE_DIMENSION,
        "settings": {"countRepeatInstances": True, "limit": 50000, "nonesBehavior": "exclude-nones"},
    }
    result = _request("POST", "/reports?locale=de_DE", body)
    rows = result.get("rows", [])
    url_set = set(canonical_urls)
    out: dict[str, int | None] = {url: None for url in canonical_urls}
    for row in rows:
        dim = (row.get("value") or "").strip()
        if dim in url_set:
            val = row.get("data", [None])
            out[dim] = int(val[0]) if val and val[0] is not None else None
    return out
