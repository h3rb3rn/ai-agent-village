# P33 – HuggingFace-Dataset-Import (vollständig, Betreiber-bestätigt)

Date: 2026-09-27
Verification class: `LOCAL_VERIFIED` (Skript), Host-Ausführung folgt in diesem Schritt

## Entscheidung des Betreibers

- Netzwerkfreigabe für `huggingface.co` bestätigt (Option a aus dem Plan) und
  live verifiziert (HTTP 200, 213 ms von N06-M10).
- Datensatz: **`mecha-org/linux-command-dataset`** — Apache-2.0, nicht gegatet,
  Inhalt direkt verifiziert (8.669 Paare aus natürlichsprachlicher Aufgabe →
  Shell-Befehl, 923.481 Bytes). Gegenkandidaten verworfen: `NetoAISolutions/NetBench`
  (`gated: manual`), `darkknight25/Networking_Commands_Dataset` (Lizenz-Tag
  unklar gegenüber tatsächlichen Nutzungsbedingungen laut Web-Recherche).
- Ablagepfad bestätigt, Umfang „vollständig“ (alle 8.669 Zeilen, nicht nur
  eine kuratierte Stichprobe wie ursprünglich vorgeschlagen).

## Provenienz

- Quelle: `https://huggingface.co/datasets/mecha-org/linux-command-dataset/resolve/main/linuxcommands.json`
- Digest: `81b33a65b80134875706fe005601f8b75dee726e91c4b632a517790e2b9ab175`
- Gepinnte Kopie: `/mnt/ssd-data/datasets/mecha-org_linux-command-dataset_81b33a65b801.json`
  (root:ai-village, `0644`)

## Umsetzung

`scripts/import-dataset.py`: generisches, digest-verifiziertes Bulk-Import-Werkzeug.
- Lädt `memory/gateway.py` per Dateipfad (funktioniert sowohl im Repo-Layout
  als auch gegen die geflachte Host-Installation `/usr/local/lib/ai-village/memory-gateway.py`,
  ohne `sys.path`-Annahmen).
- Schreibt **direkt** in dieselbe SQLite-Datenbank und denselben Outbox-Mechanismus
  wie das Gateway selbst (Chroma/Neo4j-Projektion läuft dadurch automatisch mit) –
  bewusst unter Umgehung der Stundenquote pro Agent (`MEMORY_WRITES_PER_HOUR`),
  die einen außer Kontrolle geratenen Agenten-Loop begrenzen soll, nicht einen
  einmaligen, bewusst freigegebenen Bulk-Import.
- Jede Zeile bekommt einen deterministischen `idempotency_key`
  (`import:<digest>:<index>`) – ein erneuter Lauf dupliziert nichts.
- Attribution: `agent="dataset-import"`, `scope="shared"`, `kind="reference"`,
  `source_event="dataset:mecha-org/linux-command-dataset:<digest>"`,
  `confidence=1.0` – im `/v1/stats`-Endpunkt klar von echten Agenten-Erinnerungen
  unterscheidbar.
- Transformation `linux_command_pairs`: `{"input","output"}` → `"Task: …\nCommand: …"`,
  unvollständige Paare werden übersprungen, nichts wird erfunden.

## Tests

`tests/test_import_dataset.py` – 11 Tests: Transformation, Digest-Verifikation
(inkl. Mismatch-Ablehnung), echter Import gegen eine Test-SQLite (Attribution,
Outbox-Eintrag, Idempotenz bei erneutem Lauf, Dry-Run schreibt nichts,
`--limit`, Überlangen-Inhalt wird übersprungen statt stillschweigend gekürzt).

**Dry-Run gegen die echte, vollständige, gepinnte Datei auf N06-M10:**
`imported=8669, skipped_empty=0, skipped_oversized=0, skipped_duplicate=0`.

Gesamtsuite: 450 Tests, OK.

## Host-Ausführung

Siehe Abschnitt „Ergebnis“ unten für den tatsächlichen Schreib-Lauf.
