"""
Editorial Suite JSON-Feed Client für TC-Artikelliste.

ENV-Vars:
  ES_CLIENT_ID      – OAuth2 Client ID
  ES_CLIENT_SECRET  – OAuth2 Client Secret (niemals loggen)
  ES_API_KEY        – x-api-key Header
  ES_TOKEN_URL      – OAuth2 Token-Endpoint
  ES_FEED_URL       – Feed-Endpoint
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

_ES_CLIENT_ID = os.environ.get("ES_CLIENT_ID", "").strip()
_ES_CLIENT_SECRET = os.environ.get("ES_CLIENT_SECRET", "").strip()
_ES_API_KEY = os.environ.get("ES_API_KEY", "").strip()
_ES_TOKEN_URL = os.environ.get(
    "ES_TOKEN_URL",
    "https://json-feeds-auth.prd.as.editorialsuite.io/oauth2/token",
).strip()
_ES_FEED_URL = os.environ.get(
    "ES_FEED_URL",
    "https://json-feeds.prd.as.editorialsuite.io/feed-api/v1/tenants/bild/feeds/rhehKDE0XWPOmSDlQxCk/document-groups/0",
).strip()

_token_lock = threading.Lock()
_token_cache: dict[str, Any] = {"token": "", "expires_at": 0.0}
_last_error: str = ""
_last_success_ts: float = 0.0


def _is_configured() -> bool:
    return bool(_ES_CLIENT_ID and _ES_CLIENT_SECRET and _ES_API_KEY)


def get_status() -> dict[str, Any]:
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
    return {
        "esFeedConfigured": _is_configured(),
        "esTokenStatus": token_status,
        "esLastError": _last_error or None,
        "esLastSuccessfulRequestAt": _last_success_ts or None,
    }


def _get_token(force: bool = False) -> str:
    global _last_error, _last_success_ts
    if not _is_configured():
        raise RuntimeError("ES_CLIENT_SECRET nicht konfiguriert")
    with _token_lock:
        now = time.time()
        if not force and _token_cache["token"] and now < _token_cache["expires_at"] - 60:
            return _token_cache["token"]
        payload = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": _ES_CLIENT_ID,
            "client_secret": _ES_CLIENT_SECRET,
        }).encode()
        req = urllib.request.Request(
            _ES_TOKEN_URL, data=payload, method="POST",
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
                body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            _last_error = f"HTTP {exc.code}: {body}"
            raise RuntimeError(f"ES Token-Fehler: {_last_error}") from exc
        except Exception as exc:
            _last_error = str(exc)
            raise RuntimeError(f"ES Token-Fehler: {_last_error}") from exc


def _parse_text(field: Any) -> str:
    if isinstance(field, dict):
        return (field.get("plainText") or "").strip()
    return str(field or "").strip()


def _detect_ressort(channel_name: str) -> str:
    mapping = {
        "politik": "Politik", "inland": "Politik", "ausland": "Politik",
        "sport": "Sport", "fussball": "Sport",
        "unterhaltung": "Unterhaltung", "stars": "Unterhaltung",
        "leben": "Leben & Wissen", "wissen": "Leben & Wissen",
        "regional": "Regional", "news": "News",
    }
    lower = channel_name.lower()
    for key, val in mapping.items():
        if key in lower:
            return val
    return channel_name or "News"


def fetch_articles() -> list[dict[str, Any]]:
    """
    Holt aktuelle Artikel aus dem Editorial Suite JSON-Feed.
    Gibt [] zurück wenn nicht konfiguriert — niemals Fake-Daten.
    """
    global _last_error, _last_success_ts
    if not _is_configured():
        return []
    token = _get_token()
    req = urllib.request.Request(
        _ES_FEED_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "x-api-key": _ES_API_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())
        _last_success_ts = time.time()
        _last_error = ""
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            token = _get_token(force=True)
            req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
                data = json.loads(resp.read())
            _last_success_ts = time.time()
            _last_error = ""
        else:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            _last_error = f"HTTP {exc.code}: {body}"
            raise RuntimeError(f"ES Feed-Fehler: {_last_error}") from exc

    documents = data.get("documents", []) if isinstance(data, dict) else data
    out: list[dict[str, Any]] = []
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        doc_id = str(doc.get("documentId") or "").strip()
        live_url = str(doc.get("liveUrl") or "").strip()
        if not doc_id and not live_url:
            continue

        headline = _parse_text(doc.get("headline"))
        kicker = _parse_text(doc.get("kicker"))
        full_title = f"{kicker} – {headline}" if kicker and headline else (headline or kicker)

        channel = doc.get("primaryChannel") or {}
        if isinstance(channel, dict):
            channel_name = channel.get("name", "")
            channel_path = channel.get("path", [])
        else:
            channel_name = str(channel)
            channel_path = []
        ressort = _detect_ressort(channel_name)

        premium = bool(doc.get("premium", False))
        pub_date = str(doc.get("documentPublicationDate") or doc.get("displayDate") or "")

        # Kanonische URL ableiten
        from urllib.parse import urlparse
        canonical = ""
        if live_url:
            p = urlparse(live_url)
            host = p.netloc.lower().replace("www.", "").replace("m.", "")
            path = p.path.rstrip("/")
            canonical = f"https://{host}{path}"

        out.append({
            "document_id": doc_id,
            "canonical_url": canonical,
            "source_url": live_url,
            "cms_id": doc_id,
            "title": full_title,
            "headline": headline,
            "kicker": kicker,
            "workflow_status": "BILD+" if premium else "Frei",
            "live_readers": 0,
            "home_position": None,
            "published_at": pub_date,
            "ressort": ressort,
            "source_flags": ["es_feed"],
            "urgency_score": 0,
        })
    return out


def fetch_all_articles_for_lookup() -> list[dict[str, Any]]:
    """
    Holt alle Artikel aus document-groups/1 die eine echte URL haben.
    Nur für URL/Metadaten-Lookup — kein Paginierungs-Overhead.
    Liefert [{document_id, canonical_url, source_url, title, published_at, workflow_status}]
    """
    if not _is_configured():
        return []
    token = _get_token()
    docs = _fetch_group(token, 1)
    out: list[dict[str, Any]] = []
    for doc in docs:
        live_url = str(doc.get("liveUrl") or "").strip()
        if not live_url or "/cmsid/" in live_url:
            continue  # keine echte URL → nutzlos für URL-Lookup
        doc_id = str(doc.get("documentId") or "").strip()
        from urllib.parse import urlparse
        p = urlparse(live_url)
        host = p.netloc.lower().replace("www.", "").replace("m.", "")
        canonical = f"https://{host}{p.path.rstrip('/')}"
        headline = _parse_text(doc.get("headline"))
        kicker = _parse_text(doc.get("kicker"))
        full_title = f"{kicker} – {headline}" if kicker and headline else (headline or kicker)
        pub_date = str(doc.get("documentPublicationDate") or doc.get("modificationDate") or "")
        out.append({
            "document_id": doc_id,
            "canonical_url": canonical,
            "source_url": live_url,
            "title": full_title,
            "published_at": pub_date,
            "workflow_status": "BILD+" if doc.get("premium") else "Frei",
        })
    return out


def _fetch_group(token: str, group_id: int) -> list[dict[str, Any]]:
    feed_base = _ES_FEED_URL.rsplit("/document-groups/", 1)[0]
    url = f"{feed_base}/document-groups/{group_id}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "x-api-key": _ES_API_KEY,
    })
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
        data = json.loads(resp.read())
    return data.get("documents", []) if isinstance(data, dict) else []


def fetch_editing_articles() -> list[dict[str, Any]]:
    """
    Holt Artikel in Bearbeitung (nicht publiziert) aus document-groups/1.
    Workflow-Status:
      - liveUrl mit echtem Slug → 'redigiert' (bereit zum Publizieren)
      - liveUrl mit /cmsid/ → 'zum verbauen' (noch in Arbeit)
    Gibt [] zurück wenn nicht konfiguriert.
    """
    if not _is_configured():
        return []
    token = _get_token()
    published_docs = _fetch_group(token, 0)
    all_docs = _fetch_group(token, 1)
    published_ids = {d.get("documentId") for d in published_docs if d.get("documentId")}

    out: list[dict[str, Any]] = []
    for doc in all_docs:
        doc_id = str(doc.get("documentId") or "").strip()
        if doc_id in published_ids:
            continue  # bereits publiziert → nicht im zweiten Tab
        live_url = str(doc.get("liveUrl") or "").strip()
        # Workflow-Status ableiten
        if "/cmsid/" in live_url or not live_url:
            workflow = "zum verbauen"
            canonical = f"https://bild.de/cmsid/{doc_id}" if doc_id else ""
        else:
            workflow = "redigiert"
            from urllib.parse import urlparse
            p = urlparse(live_url)
            host = p.netloc.lower().replace("www.", "").replace("m.", "")
            path = p.path.rstrip("/")
            canonical = f"https://{host}{path}"

        headline = _parse_text(doc.get("headline"))
        kicker = _parse_text(doc.get("kicker"))
        full_title = f"{kicker} – {headline}" if kicker and headline else (headline or kicker)

        channel = doc.get("primaryChannel") or {}
        channel_name = channel.get("name", "") if isinstance(channel, dict) else str(channel)
        ressort = _detect_ressort(channel_name)
        premium = bool(doc.get("premium", False))
        pub_date = str(doc.get("documentPublicationDate") or doc.get("modificationDate") or "")

        out.append({
            "document_id": doc_id,
            "canonical_url": canonical,
            "source_url": live_url,
            "cms_id": doc_id,
            "title": full_title,
            "workflow_status": workflow,
            "live_readers": 0,
            "home_position": None,
            "published_at": pub_date,
            "ressort": ressort,
            "source_flags": ["es_feed"],
            "urgency_score": 0,
        })
    return out
