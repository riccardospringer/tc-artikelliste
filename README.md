# Textchefs Artikelliste (TC-2 Live Ingestion)

MVP zur Erstellung einer priorisierten Liste publizierter Texte auf bild.de.

## Kurzantwort: Wo läuft TC Artikelliste lokal?
- Laufzeitumgebung: lokal auf deinem Rechner als Python-Prozess (`run_local.sh` startet `src/server.py`)
- UI: `http://localhost:8080/`
- API: `http://localhost:8080/api/articles`
- Start: `cd /Users/riccardo.longo/projecthub/textchefs_artikelliste && ./run_local.sh`
- Voraussetzungen: Python 3 installiert, Start im Projektordner, freier Port ab `8080`
- Wenn `8080` belegt ist, wird automatisch ein freier Port zwischen `8080..8130` genutzt.

Siehe auch: `tickets/TC-10_lokale_laufzeit.md`

## Ziel
- Alle publizierten Artikel aus Adobe + RSS zusammenführen.
- Workflow-Status `zum verbauen` und `redigiert` ausschließen.
- Ranking:
  1. `home_position` (niedriger = relevanter)
  2. `live_readers` (höher = relevanter)
  3. `published_at` (neuer = relevanter)

## Datenquellen und Feldmapping

### Adobe (JSON/CSV/HTTP)
- `url` / `article_url` / `link` -> URL des Artikels
- `cms_id` / `content_id` -> Artikel-ID
- `title` / `headline` -> Titel
- Workflow-Zustand über Mapping-Regeln: `workflow_status`, `workflow status`, `workflow-status`, `workflowStatus`, `workflow_state`, `workflowState`, `status`, `article_status`, `articleStatus`, `artikel_status`, `artikelStatus`, `publication_status`, `publicationStatus`, `publishing_status`, `publishingStatus`
- `live_readers` / `readers` / `active_users` -> Live-Leser
- `published_at` / `publish_time` -> Veröffentlichungszeit

### RSS (XML/JSON/HTTP)
- `link` / `url` -> URL des Artikels
- `guid` -> RSS-GUID
- `title` -> Titel
- `pubDate` / `published_at` -> Veröffentlichungszeit
- optionaler Workflow-Zustand über Mapping-Regeln: `workflow_status`, `workflowStatus`, `workflow-state`, `workflow_state`, `status`, `article_status`, `articleStatus`, `publication_status`, `publicationStatus`, `publishing_status`, `publishingStatus`
- bei RSS-XML werden zusätzlich benannte Item-Tags (auch mit Namespace-Präfix) eingelesen, z. B. `<workflowStatus>`, `<workflow-status>`, `<status>`, `<tc:workflow-status>`

### Home-Position (JSON)
- `url` / `link` -> URL des Artikels
- `position` / `home_position` -> Platz auf Homepage
- optional: `cms_id`

## Matching-Logik
- Primär über kanonisierte URL (`https`, `www.` entfernt, Query/Fragment entfernt)
- Fallback über `cms_id`
- Quellen werden auf einen gemeinsamen Artikel-Record gemergt

## Lokal starten (Antwort auf: "wo läuft tc artikelliste lokal?")
Die TC Artikelliste läuft lokal als Python-Prozess auf deinem Rechner, nicht als dauerhaft laufender Dienst.

Direktantwort:
- Lokale UI: `http://localhost:8080/`
- Lokale API: `http://localhost:8080/api/articles`
- Startbefehl: `./run_local.sh` im Projektordner

- Standard-URL (UI): `http://localhost:8080/`
- Standard-URL (JSON-API): `http://localhost:8080/api/articles`

Voraussetzungen:
1. Python 3 ist installiert (`python3 --version`)
2. Der Befehl wird im Projektordner ausgeführt
3. Der lokale Port `8080` ist frei

Startbefehl:

```bash
cd /Users/riccardo.longo/projecthub/textchefs_artikelliste
./run_local.sh
```

Direkt mit Editorial-One-Daten starten (ist bereits vorkonfiguriert):

```bash
cd /Users/riccardo.longo/projecthub/textchefs_artikelliste
./run_local.sh
```

Hinweis: `run_local.sh` lädt automatisch `.tc_live_sources.env`.  
Wenn du die Konfiguration überschreiben willst, nutze `.tc_live_sources.env.example` als Vorlage.

Erwartetes Verhalten:
1. Wenn ein lokaler Port nutzbar ist, startet die Web-UI auf `http://localhost:8080/`
2. Wenn `8080` belegt ist, sucht `run_local.sh` automatisch einen freien Port im Bereich `8080..8130` und startet dort die Web-UI.
3. Wenn in diesem Bereich kein Port frei ist, wird automatisch eine serverlose Vorschau erzeugt:
   `file:///tmp/tc_artikelliste_preview.html`

Manueller Serverstart (optional):

```bash
TC_HOST=0.0.0.0 TC_PORT=8080 python3 src/server.py
```

Erwartete Ausgabe beim manuellen Serverstart:

```text
Server gebunden auf 0.0.0.0:8080
UI lokal: http://localhost:8080/
API lokal: http://localhost:8080/api/articles
Alternative lokal: http://127.0.0.1:8080/
```

Schnellcheck Erreichbarkeit:

```bash
curl -i http://localhost:8080/api/articles
```

Wenn `http://localhost:8080/` oder `http://127.0.0.1:8080/` nicht erreichbar ist:
1. Prüfen, ob der Server-Prozess wirklich läuft (Terminal mit `python3 src/server.py` muss offen bleiben)
2. Prüfen, ob Port 8080 belegt ist: `lsof -nP -iTCP:8080 -sTCP:LISTEN`
3. Wenn mit `./run_local.sh` gestartet wurde: die im Terminal ausgegebene URL mit dem tatsächlich gewählten Port öffnen.
4. Server gezielt auf allen lokalen Interfaces starten:

```bash
TC_HOST=0.0.0.0 TC_PORT=8080 python3 src/server.py
```

5. Bei Portkonflikt auf freien Port wechseln, z. B. `8100`:

```bash
TC_HOST=0.0.0.0 TC_PORT=8100 python3 src/server.py
curl -i http://localhost:8100/api/articles
```

6. Danach die neue URL im Browser verwenden: `http://localhost:8100/`

Der lokale Server nutzt die Beispiel-Daten aus `fixtures/`.

Fallback ohne lokalen Port (direkt):

```bash
cd /Users/riccardo.longo/projecthub/textchefs_artikelliste
python3 src/static_preview.py
```

Danach die erzeugte Datei direkt im Browser öffnen:

```text
file:///tmp/tc_artikelliste_preview.html
```

Dieser Weg braucht keinen laufenden Webserver und keine Portfreigabe.

## Hosting (für geteilte Nutzung)
Das Projekt kann als Container auf einem Hosting-Dienst (z. B. Render, Railway, Cloud Run, Fly.io) laufen.

### Wichtig für den geteilten Link
- Nicht den Link eines anderen Tools wiederverwenden.
- Für TC eine eigene Service-URL/Subdomain verwenden (z. B. eigener Dienstname `tc-artikelliste`).
- Für einen eindeutigen, kopierbaren Share-Link zusätzlich `TC_PUBLIC_BASE_URL` setzen (z. B. `https://tools.example.com/tc-artikelliste`).
- Wenn mehrere Tools unter einer Domain laufen, `TC_BASE_PATH` setzen (z. B. `/tc-artikelliste`). Der Server akzeptiert jetzt sowohl Requests mit Präfix als auch gestrippte Proxy-Requests ohne Präfix.
- Die UI berechnet Share-/API-/Health-Link zusätzlich aus der echten Browser-URL und hat einen `Link kopieren`-Button. Dadurch bleiben Links auch bei Proxy-Rewrites erreichbar.

### Verfügbare Endpunkte im Hosting
- Standard (ohne `TC_BASE_PATH`):
  - UI: `/`
  - JSON-API: `/api/articles`
  - Healthcheck: `/healthz`
- Mit `TC_BASE_PATH=/tc-artikelliste`:
  - UI: `/tc-artikelliste/`
  - JSON-API: `/tc-artikelliste/api/articles`
  - Healthcheck: `/tc-artikelliste/healthz`

### Umgebungsvariablen für Hosting
- `PORT`: Primärer Hosting-Port (z. B. von Render/Railway/Cloud Run gesetzt), wird automatisch genutzt.
- `TC_PORT`: Optionaler Override für Sonderfälle; nur setzen, wenn bewusst nötig.
- `TC_HOST`: Standard `0.0.0.0`.
- `TC_BASE_PATH`: Optionaler URL-Präfix für dedizierte Route hinter Shared-Domain, z. B. `/tc-artikelliste`. Standard: `/`.
- `TC_PUBLIC_BASE_URL`: Optionale öffentliche Basis-URL für klare Share-Ausgaben im Server-Log, z. B. `https://tools.example.com/tc-artikelliste`.
- `TC_ADOBE_SOURCE`: Adobe-Quelle (Dateipfad oder HTTP-URL). Standard: `fixtures/adobe_sample.json`.
- `TC_RSS_SOURCE`: RSS-Quelle (Dateipfad oder HTTP-URL). Standard: `fixtures/rss_sample.xml`.
- `TC_HOME_SOURCE`: Home-Positionen (Dateipfad oder HTTP-URL). Standard: `fixtures/home_positions_sample.json`.
- `TC_API_CHECK`: `true/false`, optionaler API-Vorabcheck für HTTP-Quellen. Standard: `false`.
- `TC_CACHE_SECONDS`: Cache-Dauer für API-Antworten in Sekunden. Standard: `30`.
- `TC_EDITORIAL_ONE_ENABLED`: Nutzt den vorhandenen Editorial-One-Fetcher (`bild_published`) statt direkter Quellen. Standard: `false`.
- `TC_EDITORIAL_ONE_STRICT`: Bei Fehler im Editorial-One-Fetcher nicht auf lokale Quellen zurückfallen. Standard: `false`.
- `TC_EDITORIAL_ONE_HOURS`: Lookback-Fenster für Editorial-One-Liste. Standard: `24`.
- `TC_EDITORIAL_ONE_LIMIT`: Max. Anzahl Artikel aus Editorial-One-Fetcher. Standard: `300`.
- `TC_EDITORIAL_ONE_PYC`: Pfad zur `bild_published`-PyC-Datei. Standard: `/Users/riccardo.longo/editorial-intel/__pycache__/bild_published.cpython-313.pyc`.

### Container lokal prüfen
```bash
cd /Users/riccardo.longo/projecthub/textchefs_artikelliste
docker build -t tc-artikelliste .
docker run --rm -p 8080:8080 tc-artikelliste
```

Danach im Browser öffnen:
- `http://localhost:8080/`
- `http://localhost:8080/api/articles`
- `http://localhost:8080/healthz`

Optional mit eigenem Basispfad (simuliert Shared-Domain):
```bash
docker run --rm -p 8080:8080 -e TC_BASE_PATH=/tc-artikelliste tc-artikelliste
```

Dann im Browser öffnen:
- `http://localhost:8080/tc-artikelliste/`
- `http://localhost:8080/tc-artikelliste/api/articles`
- `http://localhost:8080/tc-artikelliste/healthz`

Optional mit klarem Share-Link im Log:
```bash
docker run --rm -p 8080:8080 \
  -e TC_BASE_PATH=/tc-artikelliste \
  -e TC_PUBLIC_BASE_URL=https://tools.example.com/tc-artikelliste \
  tc-artikelliste
```

Dann zeigt der Server beim Start explizit:
- `Share-Link: https://tools.example.com/tc-artikelliste/`
- `Share-API: https://tools.example.com/tc-artikelliste/api/articles`
- `Share-Health: https://tools.example.com/tc-artikelliste/healthz`

### Auf Hosting deployen (kurz)
1. Repository im Hosting-Dienst als eigenen Service `tc-artikelliste` verbinden (nicht in bestehendem Tool-Service mitverwenden).
2. Docker-Deployment aktivieren (Root mit `Dockerfile`).
3. Falls mehrere Tools unter derselben Domain laufen: `TC_BASE_PATH=/tc-artikelliste` setzen.
4. Öffentliche Ziel-URL als `TC_PUBLIC_BASE_URL` setzen (z. B. `https://<dein-host>/tc-artikelliste`).
5. Falls nötig Datenquellen setzen (`TC_ADOBE_SOURCE`, `TC_RSS_SOURCE`, `TC_HOME_SOURCE`).
6. Nach Deploy den Healthcheck auf genau dem Zielpfad prüfen (z. B. `/tc-artikelliste/healthz`).
7. Den im Log ausgegebenen `Share-Link` oder den in der UI angezeigten `Share-Link` (inkl. Copy-Button) teilen.

## Ausführung
```bash
cd /Users/riccardo.longo/projecthub/textchefs_artikelliste
PYTHONPATH=src python3 -m cli \
  --adobe https://example.internal/adobe/live.json \
  --rss https://example.internal/rss/live.xml \
  --home fixtures/home_positions_sample.json \
  --timeout 5 \
  --retries 3 \
  --backoff 0.5 \
  --backoff-factor 2 \
  --backoff-max 8 \
  --out fixtures/output.json
```

Für lokale Testdaten können `--adobe` und `--rss` weiterhin auf Dateien zeigen.
Für HTTP-Quellen wird vor der Ingestion standardmäßig ein kurzer API-Check für Adobe/RSS durchgeführt. Mit `--no-api-check` kann er deaktiviert werden.

## Tests
```bash
cd /Users/riccardo.longo/projecthub/textchefs_artikelliste
PYTHONPATH=src python3 -m pytest -q
```

## Folgeticket
- Endpoint-Monitoring/Health-Metriken: `tickets/TC-3_endpoint_monitoring.md`
