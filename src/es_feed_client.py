"""
Editorial Suite JSON-Feed Client fuer TC-Artikelliste.

Datenkorrektheit hat absolute Prioritaet:
- Premium ist KEIN Workflow-Status (paywall_status getrennt).
- Workflow-Status wird ausschliesslich aus echten Lean-Feldern gelesen.
- Wenn kein Lean-Feld existiert: workflow_status = "" -> UI zeigt unbekannt.
- Kein Raten, keine URL-Heuristik, keine Fallbacks zwischen unverwandten Quellen.

ENV-Vars:
  ES_CLIENT_ID         - OAuth2 Client ID
  ES_CLIENT_SECRET     - OAuth2 Client Secret (niemals loggen)
  ES_API_KEY           - x-api-key Header
  ES_TOKEN_URL         - OAuth2 Token-Endpoint
  ES_FEED_URL          - Feed-Endpoint (document-groups/0)
  ES_STATUS_FIELDS     - Komma-Liste zusaetzlicher Lean-Status-Feldnamen
  ES_COMMENT_FIELDS    - Komma-Liste zusaetzlicher Lean-Kommentar-Feldnamen
"""
from __future__ import annotations
import json, os, ssl, threading, time, urllib.parse, urllib.request
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

_ES_CLIENT_ID = os.environ.get("ES_CLIENT_ID", "").strip()
_ES_CLIENT_SECRET = os.environ.get("ES_CLIENT_SECRET", "").strip()
_ES_API_KEY = os.environ.get("ES_API_KEY", "").strip()
_ES_TOKEN_URL = os.environ.get("ES_TOKEN_URL", "https://json-feeds-auth.prd.as.editorialsuite.io/oauth2/token").strip()
_ES_FEED_URL = os.environ.get("ES_FEED_URL", "https://json-feeds.prd.as.editorialsuite.io/feed-api/v1/tenants/bild/feeds/rhehKDE0XWPOmSDlQxCk/document-groups/0").strip()

_DEFAULT_STATUS_FIELDS: tuple[str, ...] = (
    "editorialStatus", "editorial_status",
    "workflowStatus", "workflow_status",
    "workflowState", "workflow_state",
    "documentStatus", "document_status",
    "publicationStatus", "publication_status",
    "publishingStatus", "publishing_status",
    "leanStatus", "lean_status",
    "leanWorkflowStatus", "lean_workflow_status",
    "redactionStatus", "redaction_status",
    "redaktionStatus", "redaktions_status", "redaktionsStatus",
    "stateLabel", "state_label",
    "status", "state",
)

_DEFAULT_COMMENT_FIELDS: tuple[str, ...] = (
    "editorialNote", "editorial_note",
    "editorialComment", "editorial_comment",
    "documentNote", "document_note",
    "documentComment", "document_comment",
    "internalNote", "internal_note",
    "internalComment", "internal_comment",
    "redactionalNote", "redactional_note",
    "redaktionsHinweis", "redaktion_hinweis",
    "redaktion_kommentar", "redaktionsKommentar",
    "leanComment", "lean_comment",
    "leanNote", "lean_note",
    "note", "notes", "comment", "kommentar", "remark", "remarks", "memo",
)


def _env_field_list(env_name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return defaults
    extra = tuple(p.strip() for p in raw.split(",") if p.strip())
    seen: set[str] = set()
    merged: list[str] = []
    for name in (*extra, *defaults):
        if name and name not in seen:
            seen.add(name)
            merged.append(name)
    return tuple(merged)


_STATUS_FIELD_CANDIDATES: tuple[str, ...] = _env_field_list("ES_STATUS_FIELDS", _DEFAULT_STATUS_FIELDS)
_COMMENT_FIELD_CANDIDATES: tuple[str, ...] = _env_field_list("ES_COMMENT_FIELDS", _DEFAULT_COMMENT_FIELDS)

_STATUS_LABEL_MAP: dict[str, str] = {
    "published": "publiziert", "publiziert": "publiziert",
    "veroeffentlicht": "publiziert", "live": "publiziert", "online": "publiziert",
    "freigegeben": "freigegeben", "released": "freigegeben", "approved": "freigegeben",
    "redigiert": "redigiert", "edited": "redigiert",
    "review": "review", "reviewed": "review", "in_review": "review",
    "draft": "draft", "entwurf": "draft",
    "writing": "in arbeit", "in_writing": "in arbeit",
    "in_work": "in arbeit", "in_arbeit": "in arbeit", "in_progress": "in arbeit",
    "to_build": "zum verbauen", "tobuild": "zum verbauen",
    "zum_verbauen": "zum verbauen", "zumverbauen": "zum verbauen",
    "zu_verbauen": "zum verbauen",
    "scheduled": "geplant", "geplant": "geplant",
    "archived": "archiviert", "archiviert": "archiviert",
    "deleted": "geloescht", "geloescht": "geloescht",
}

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
        "esStatusFieldCandidates": list(_STATUS_FIELD_CANDIDATES),
        "esCommentFieldCandidates": list(_COMMENT_FIELD_CANDIDATES),
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


def _coerce_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, (int, float, bool)):
        return str(raw).strip()
    if isinstance(raw, dict):
        for key in ("plainText", "text", "value", "label", "name"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""
    if isinstance(raw, list):
        for item in raw:
            txt = _coerce_text(item)
            if txt:
                return txt
        return ""
    return str(raw).strip()


def _parse_text(field: Any) -> str:
    return _coerce_text(field)


def _walk_for_field(
    doc: Any,
    field_names: Iterable[str],
    max_depth: int = 4,
) -> tuple[str, str]:
    target = {name.lower() for name in field_names if name}
    if not target:
        return ("", "")
    stack: list[tuple[Any, str, int]] = [(doc, "", 0)]
    while stack:
        node, path, depth = stack.pop()
        if depth > max_depth:
            continue
        if isinstance(node, dict):
            for key, value in node.items():
                if not isinstance(key, str):
                    continue
                key_lc = key.lower()
                sub_path = f"{path}.{key}" if path else key
                if key_lc in target:
                    text = _coerce_text(value)
                    if text:
                        return (text, sub_path)
                if isinstance(value, (dict, list)) and depth < max_depth:
                    stack.append((value, sub_path, depth + 1))
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                sub_path = f"{path}[{idx}]"
                if isinstance(item, (dict, list)) and depth < max_depth:
                    stack.append((item, sub_path, depth + 1))
    return ("", "")


def _normalize_label(raw: str) -> str:
    if not raw:
        return ""
    norm = raw.strip().lower()
    norm_key = norm.replace(" ", "_").replace("-", "_")
    if norm_key in _STATUS_LABEL_MAP:
        return _STATUS_LABEL_MAP[norm_key]
    if norm in _STATUS_LABEL_MAP:
        return _STATUS_LABEL_MAP[norm]
    return norm


def _extract_lean_status(doc: dict[str, Any]) -> tuple[str, str]:
    return _walk_for_field(doc, _STATUS_FIELD_CANDIDATES)


def _extract_lean_comment(doc: dict[str, Any]) -> tuple[str, str]:
    return _walk_for_field(doc, _COMMENT_FIELD_CANDIDATES)


def _detect_ressort(channel_name: str) -> str:
    mapping = {
        "politik": "Politik", "inland": "Politik", "ausland": "Politik",
        "sport": "Sport", "fussball": "Sport",
        "unterhaltung": "Unterhaltung", "stars": "Unterhaltung",
        "leben": "Leben & Wissen", "wissen": "Leben & Wissen",
        "regional": "Regional", "news": "News",
    }
    lower = (channel_name or "").lower()
    for key, val in mapping.items():
        if key in lower:
            return val
    return channel_name or "News"


def _canonical_url(live_url: str, doc_id: str = "") -> tuple[str, bool]:
    if not live_url:
        if doc_id:
            return (f"https://bild.de/cmsid/{doc_id}", False)
        return ("", False)
    p = urlparse(live_url)
    host = p.netloc.lower().replace("www.", "").replace("m.", "")
    path = p.path.rstrip("/")
    canonical = f"https://{host}{path}" if host else live_url
    has_real_slug = "/cmsid/" not in live_url
    return (canonical, has_real_slug)


def _map_document(doc: dict[str, Any], *, source_group: int) -> dict[str, Any]:
    doc_id = str(doc.get("documentId") or "").strip()
    live_url = str(doc.get("liveUrl") or "").strip()
    canonical, has_real_slug = _canonical_url(live_url, doc_id)

    headline = _coerce_text(doc.get("headline"))
    kicker = _coerce_text(doc.get("kicker"))
    full_title = f"{kicker} – {headline}" if kicker and headline else (headline or kicker)

    channel = doc.get("primaryChannel") or {}
    channel_name = (channel.get("name", "") or "") if isinstance(channel, dict) else str(channel)
    ressort = _detect_ressort(channel_name)

    premium_raw = doc.get("premium")
    has_premium_field = premium_raw is not None
    paywall_status = ("BILD+" if bool(premium_raw) else "Frei") if has_premium_field else ""

    lean_raw, lean_field = _extract_lean_status(doc)
    workflow_status = _normalize_label(lean_raw)

    comment_raw, comment_field = _extract_lean_comment(doc)

    pub_date = str(
        doc.get("documentPublicationDate")
        or doc.get("displayDate")
        or doc.get("modificationDate")
        or ""
    )

    field_sources = {
        "paywall_status": "premium" if has_premium_field else "",
        "workflow_status": lean_field,
        "lean_comment": comment_field,
        "headline": "headline" if headline else "",
        "kicker": "kicker" if kicker else "",
        "channel": "primaryChannel" if channel_name else "",
        "live_url": "liveUrl" if live_url else "",
    }

    warnings: list[str] = []
    if not lean_field:
        warnings.append("workflow_status: kein Lean-Feld gefunden")
    if not has_real_slug:
        warnings.append("live_url: keine echte Slug-URL (cmsid)")
    if not doc_id:
        warnings.append("documentId fehlt")

    return {
        "document_id": doc_id,
        "canonical_url": canonical,
        "source_url": live_url,
        "cms_id": doc_id,
        "title": full_title,
        "headline": headline,
        "kicker": kicker,
        "workflow_status": workflow_status,
        "paywall_status": paywall_status,
        "lean_workflow_status_raw": lean_raw,
        "lean_workflow_status_field": lean_field,
        "lean_comment": comment_raw,
        "lean_comment_raw": comment_raw,
        "lean_comment_field": comment_field,
        "field_sources": field_sources,
        "warnings": warnings,
        "live_readers": 0,
        "home_position": None,
        "published_at": pub_date,
        "ressort": ressort,
        "source_flags": ["es_feed"],
        "urgency_score": 0,
        "last_checked_at": time.time(),
        "source_group": source_group,
        "has_real_slug": has_real_slug,
    }


def _request_feed(url: str, token: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "x-api-key": _ES_API_KEY,
        },
    )
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
        data = json.loads(resp.read())
    return data if isinstance(data, dict) else {"documents": data}


def _fetch_group(token: str, group_id: int) -> list[dict[str, Any]]:
    feed_base = _ES_FEED_URL.rsplit("/document-groups/", 1)[0]
    url = f"{feed_base}/document-groups/{group_id}"
    try:
        data = _request_feed(url, token)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            new_token = _get_token(force=True)
            data = _request_feed(url, new_token)
        else:
            raise
    return data.get("documents", []) if isinstance(data, dict) else []


def fetch_articles() -> list[dict[str, Any]]:
    global _last_error, _last_success_ts
    if not _is_configured():
        return []
    token = _get_token()
    try:
        documents = _fetch_group(token, 0)
        _last_success_ts = time.time()
        _last_error = ""
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        _last_error = f"HTTP {exc.code}: {body}"
        raise RuntimeError(f"ES Feed-Fehler: {_last_error}") from exc
    out: list[dict[str, Any]] = []
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        doc_id = str(doc.get("documentId") or "").strip()
        live_url = str(doc.get("liveUrl") or "").strip()
        if not doc_id and not live_url:
            continue
        out.append(_map_document(doc, source_group=0))
    return out


def fetch_all_articles_for_lookup() -> list[dict[str, Any]]:
    if not _is_configured():
        return []
    token = _get_token()
    docs = _fetch_group(token, 1)
    out: list[dict[str, Any]] = []
    for doc in docs:
        live_url = str(doc.get("liveUrl") or "").strip()
        if not live_url or "/cmsid/" in live_url:
            continue
        mapped = _map_document(doc, source_group=1)
        if not mapped.get("source_url"):
            continue
        out.append(mapped)
    return out


def fetch_editing_articles() -> list[dict[str, Any]]:
    if not _is_configured():
        return []
    token = _get_token()
    published_docs = _fetch_group(token, 0)
    all_docs = _fetch_group(token, 1)
    published_ids = {d.get("documentId") for d in published_docs if d.get("documentId")}
    out: list[dict[str, Any]] = []
    for doc in all_docs:
        doc_id = str(doc.get("documentId") or "").strip()
        if doc_id and doc_id in published_ids:
            continue
        out.append(_map_document(doc, source_group=1))
    return out


def fetch_raw_document_by_id(doc_id: str) -> dict[str, Any] | None:
    target = (doc_id or "").strip()
    if not target or not _is_configured():
        return None
    token = _get_token()
    for group_id in (0, 1):
        for doc in _fetch_group(token, group_id):
            if str(doc.get("documentId") or "").strip() == target:
                return {"document": doc, "source_group": group_id}
    return None


def fetch_raw_documents_by_predicate(
    predicate: Callable[[dict[str, Any]], bool],
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if not _is_configured():
        return []
    token = _get_token()
    matches: list[dict[str, Any]] = []
    for group_id in (0, 1):
        for doc in _fetch_group(token, group_id):
            try:
                if predicate(doc):
                    matches.append({"document": doc, "source_group": group_id})
                    if limit is not None and len(matches) >= limit:
                        return matches
            except Exception:
                continue
    return matches


def fetch_raw_document_by_url(url: str) -> dict[str, Any] | None:
    target = (url or "").strip()
    if not target or not _is_configured():
        return None
    target_canon, _ = _canonical_url(target)
    target_lc = target_canon.lower()

    def _matches(doc: dict[str, Any]) -> bool:
        live = str(doc.get("liveUrl") or "").strip()
        if not live:
            return False
        canon, _ = _canonical_url(live)
        return canon.lower() == target_lc

    results = fetch_raw_documents_by_predicate(_matches, limit=1)
    return results[0] if results else None
