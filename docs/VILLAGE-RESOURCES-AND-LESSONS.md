# Dorf: Ressourcen, Modelle und Betriebserkenntnisse

Stand: 2026-10-09. Nur Angaben, die in den Sitzungen dieser Zeit auf den Hosts gelesen
oder gemessen wurden. Unsichere Punkte sind als solche markiert.

## 1. Hardware und Endpunkte

| Host | Rolle | Hardware (gemessen) |
|---|---|---|
| N06-M10 | Dorf-Laufzeit (9 Resident-Dienste, Authority, Web-UI, Auditor, Gedächtnis-Gateway, Meeting-Scheduler) | 4 CPU-Kerne, 16 GiB RAM (laut `VILLAGE_RESOURCE_PROFILE`) |
| N02-M60 | Inferenz (9 Ollama-Endpunkte) | 4x Tesla M60 (8 GiB je Karte) plus 8 Einzel-GPUs |
| N04-RTX | Test-Host, Fork-Vergleich | RTX 2060/3060 |
| N11-M10 | Test-Host | 4x Tesla M10 |

**Endpunkte und Modelle (N02-M60, Stand 2026-10-08):**

| Port | Agent | Modell | Kontext |
|---|---|---|---|
| 11434 | 01-king | qwen3.6:35b | 262144 |
| 11435 | 02-explorer | qwen3.5:4b | 262144 |
| 11436 | 03-librarian | qwen3.5:4b | 262144 |
| 11437 | 04-artisan | qwen3.5:4b | 262144 |
| 11438 | 05-interpreter | qwen3.5:4b | 262144 |
| 11439 | 06-operator | nemotron-3-nano:4b | 262144 |
| 11440 | 07-methodologist | Qwen3.8-4B-Distill | 262144 |
| 11441 | 08-logician | Spark-X2.5-4B | 262144 |
| 11442 | 09-chronicler | TwIL-LM3-Pro | 131072 |

Die nativen Kontextfenster (`/api/show`): qwen3.6:35b 262144, qwen3.5:4b 262144, nemotron 262144,
Qwen3.8-4B 262144, Spark 1048576, TwIL und granite 131072, gemma3 131072. Die Container haben kein
festes Modell: Ein Agent fordert sein Modell an, Ollama lädt es (MAX_LOADED_MODELS=1).

**Wichtig für Änderungen:** Jeder Request enthält `num_ctx`, `num_batch` (seit 2026-10-09 für alle
Agents 64), `num_predict` (seit 2026-10-09 32768), `think` und `keep_alive` 24h. Ein Request mit anderen
Optionen als beim Vorladen lädt das Modell neu (laut fork-84 gemessen, ca. 2,5 Minuten beim Pool).

## 2. Laufzeitgrenzen (Konfiguration, Stand 2026-10-09)

- `OLLAMA_NUM_PREDICT` 32768 je Agent (Modellobergrenze laut `village/config.py`).
- `VILLAGE_OLLAMA_TIMEOUT_SECONDS` 1800 je Agent. Die Zeitgrenze pro Anfrage begrenzt damit die
  Zykluszeit, nicht die Ausgabe.
- `OLLAMA_THINK_LEVEL`: King medium, Explorer medium, Librarian low, Operator low, Methodologist medium,
  Logician medium, Artisan off, Interpreter off, Chronicler off.
- Der KV-Cache richtet sich nach `num_ctx`, nicht nach `num_predict` (P72). Ein höheres Budget kostet
  keinen zusätzlichen VRAM, nur Zeit.
- Tokengeschwindigkeit auf Tesla M60 (König, 35B): etwa 8 bis 14 tok/s. Ein voll ausgeschöpftes Budget
  von 32768 Token dauert damit bis zur Zeitgrenze von 1800 Sekunden.

## 3. Was im Betrieb gelernt wurde

1. **Leere Ausgaben zählen nicht als Wiederholung** (P94). Drei leere Ausgaben haben denselben
   Fingerabdruck (SHA-256 des leeren Textes) und haben früher den Loop-Breaker dauerhaft aktiviert.
2. **Gates sind der größte Engpass, nicht die Modelle.** Librarian, Interpreter und König hingen stundenlang
   an Kalender- und Schreibslot-Gates. Ein Modellwechsel löst das nicht.
3. **Berichtssperre wirkt** (P99): Vor P99 94 Prozent wiederholte Meeting-Berichte, danach keine Duplikate.
   Die Agents versuchen aber weiter zu berichten, wenn kein Meeting offen ist (P104 nennt das jetzt).
4. **Profile werden kaum angelegt.** Stand 2026-10-09 17:59: nur 05-interpreter (16 Aktualisierungen).
   Die Aufforderung im Kontext wird nicht umgesetzt. Ursache nicht abschließend geklärt.
5. **Verwaiste Tasks** müssen bereinigt werden; neu angelegte Doppel entstehen, wenn Themen mit anderem
   Wortlaut wiederkommen (P96, P101).
6. **Auditor-Absturzschleife** (193 Neustarts) kam von doppelten Memory-IDs bei geteilten Korrekturen (P97).
7. **Gazette**: Die Runtime öffnet die Tagesausgabe jetzt selbst (P98). Eine Ausgabe ohne neue Beiträge
   wurde am 8. Oktober veröffentlicht und zurückgezogen. Die Frist für Beiträge liegt bei 13:00 UTC,
   die Veröffentlichung bei 15:00 UTC.
8. **Aktionsfehler** sind meist korrekte Ablehnungen, die den Agent nicht zum Aufhören bringen. P104
   nennt jetzt in den Fehlermeldungen, was als Nächstes zu tun ist.

## 4. Offene Punkte

- Profile: Ursache für das Ausbleiben nicht geklärt (Kontext, Auswahl der Aktion oder Gate-Reihenfolge).
- Schule (P105 bis P107): Lehrplan ist für sieben Tage im Voraus festzulegen (Betreiberregel). Die
  Zustellung und Prüfung sind noch nicht umgesetzt.
- Die drei Compact-Tasks sind noch offen und nicht zusammengeführt.

## 5. Woran man den Stand prüft

- Ereignislog: `/var/lib/ai-village/board/events.jsonl`
- Koordination: `/var/lib/ai-village/board/coordination.sqlite3` (Tasks, Gazette, Profile, Kalender, Inbox)
- Laufzeitzustand je Agent: `/var/lib/ai-village/users/<name>/runtime-state.json`
- Kennzahlen: `scripts/redundancy-report.py <events.jsonl> [seit]`
- Fortschritt der Arbeitspakete: `docs/GAP-CLOSURE-STATUS.md`
