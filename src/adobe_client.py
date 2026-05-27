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
  ADOBE_DATE_RANGE_DAYS    – Lookback-Fenster in Tagen (default: 7)
  ADOBE_LIVE_READERS_METRIC – Metric ID für Live-Leser (default: metrics/pageviews)
  ADOBE_CONVERSION_METRIC  – Metric ID für BILDplus-Abos (default: metrics/event60)
  ADOBE_ARTICLE_DIMENSION  – Dimension für Artikel-URL/Headline (default: variables/evar12)
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
_ADOBE_ARTICLE_DIMENSION = os.environ.get("ADOBE_ARTICLE_DIMENSION", "variables/evar12").strip()

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


def _days_ago(n: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - n * 86400))


def fetch_live_readers(canonical_urls: list[str]) -> dict[str, int]:
    """
    Gibt {canonical_url: pageviews_today} zurück.
    Liefert {} wenn nicht konfiguriert — niemals Fake-Werte.
    """
    if not _is_configured() or not _is_mapping_complete():
        return {}
    today = _days_ago(0)
    body = {
        "rsid": _ADOBE_RSID,
        "globalFilters": [{"type": "dateRange", "dateRange": f"{today}T00:00:00.000/{today}T23:59:59.000"}],
        "metricContainer": {"metrics": [{"columnId": "0", "id": _ADOBE_LIVE_READERS_METRIC}]},
        "dimension": _ADOBE_ARTICLE_DIMENSION,
        "settings": {"countRepeatInstances": True, "limit": 50000, "nonesBehavior": "exclude-nones"},
    }
    result = _request("POST", "/reports?locale=de_DE", body)
    rows = result.get("rows", [])
    url_set = set(canonical_urls)
    out: dict[str, int] = {}
    for row in rows:
        dim = (row.get("value") or "").strip()
        if dim in url_set:
            val = row.get("data", [0])
            out[dim] = int(val[0]) if val else 0
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
    # Minimale Analytics-Abfrage: einen Artikel-Eintrag holen
    try:
        today = _days_ago(0)
        body = {
            "rsid": _ADOBE_RSID,
            "globalFilters": [{"type": "dateRange", "dateRange": f"{today}T00:00:00.000/{today}T23:59:59.000"}],
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
