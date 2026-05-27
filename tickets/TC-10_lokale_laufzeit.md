# TC-10: Wo läuft TC Artikelliste lokal?

## Kurzantwort
Die TC Textchefs Artikelliste läuft lokal auf deinem Rechner als Python-Prozess aus diesem Repository.

- UI: `http://localhost:8080/`
- API: `http://localhost:8080/api/articles`
- Start: `./run_local.sh` im Projektordner

## Voraussetzungen
1. Python 3 ist installiert (`python3 --version`)
2. Start im Projektordner `/Users/riccardo.longo/projecthub/textchefs_artikelliste`
3. Ein lokaler Port ab `8080` ist frei

## Start und Verhalten
Start:

```bash
cd /Users/riccardo.longo/projecthub/textchefs_artikelliste
./run_local.sh
```

Verhalten:
1. Standard ist `http://localhost:8080/`.
2. Wenn `8080` belegt ist, sucht das Skript automatisch einen freien Port im Bereich `8080..8130`.
3. Wenn kein Port frei ist, wird ein serverloser Fallback erzeugt: `file:///tmp/tc_artikelliste_preview.html`.

## Schnellcheck

```bash
curl -i http://localhost:8080/api/articles
```

Wenn `run_local.sh` auf einen anderen Port gewechselt hat, denselben Check mit diesem Port ausführen.
