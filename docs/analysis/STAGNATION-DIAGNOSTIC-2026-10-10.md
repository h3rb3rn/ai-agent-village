# Stagnations-Diagnose und Entfaltungsplan – 2026-10-10

Auftrag des Betreibers (2026-10-10): „Finde heraus warum das Dorf und die Agenten
stagnieren und immer das gleiche tun […] was nötig ist damit sich die Agents
entwickeln und entfalten können und ob es schon zu viele Beschränkungen und Regeln
gibt." Folgeauftrag: „Hebe die künstlichen Beschränkungen auf und plane die erkannten
Probleme durch nachhaltige Lösungen umzusetzen."

Leitlinie bleibt [VISION.md](../VISION.md).

## 1. Datenbasis

- Code und Doku im Repository (Stand `960499f`).
- Öffentliche Observatory-API (read-only): 1000 Events vom 2026-10-10, 15:11–18:57 UTC,
  147 Tasks (`/api/lab`, seit 2026-09-27), Gazette-Liste.
- Kein Lesezugriff auf `/var/lib/ai-village` (Board/SQLite) auf N06-M10. Zahlen
  aus diesem Fenster sind eine Stichprobe, keine Vollerhebung.

## 2. Befunde

| Befund | Messung |
|---|---|
| Themen-Monokultur | ca. 60 der 147 Tasks drehen sich um Disk-Monitoring (`disk-monitor.sh`), ca. 20 um Kompression/„compact tool“ – seit 2026-09-28 dasselbe Skript wiederholt neu geschrieben, verifiziert, verworfen |
| Wenig echte Arbeit | 16 Shell-Befehle für 9 Agents in 4 h; 33× `meeting_operation` ohne offenes Meeting; 15× `foreign_home_blocked`; 34× `invalid_decision` |
| Kontext ungenutzt | 8 von 9 Agents mit 262k Kontext, Prompt-Median ca. 12k Tokens (< 5 %) |
| Modell-Monokultur | 5 von 9 Agents auf `qwen3.5:4b` (Betreiberentscheidung zur Stabilisierung); die Disk-Schleife betrifft aber auch Nemotron und Qwen3.8-Distill → nicht modell-, sondern umgebungsbedingt |
| King ohne Wirkung | Median 495 s pro Inferenz, ca. 2200 Output-Tokens, 7× `output budget exhausted`, 0 erfolgreiche Aktionen im Fenster |
| Wissensbasis wird beschrieben | 31× `memory_remember` (25× shared), 13× `memory_search` mit 7–8 Treffern – Schreiben funktioniert |

## 3. Ursachen

1. **Lesepfad der Wissensbasis defekt** (behoben in P124):
   `memory_search`-Ergebnis wurde `[:6000]`, im Snapshot dann `[-2500:]` geschnitten –
   die bestbewerteten Treffer am Anfang gingen verloren. Automatisch abgerufene
   Treffer wurden beim Kürzen als Erstes und von vorn (bester Treffer zuerst) entfernt.
2. **Kein Arbeitsgedächtnis**: Jeder Zyklus besteht aus System-Prompt plus einem
   Snapshot; nur das letzte Feedback und die letzten 3 Ergebnisse überleben.
   Die semantische KB eignet sich als Langzeit-/Faktenspeicher, nicht für „woran
   arbeite ich, was ist mein nächster Schritt“. Die Abfrage für das automatische
   Retrieval ist fest (Task-Titel oder fester String) und wird nicht vom Agent gewählt.
   Eigene Erfahrungen konkurrieren mit tausenden importierten Einträgen.
3. **Künstlich gedeckelter Kontext**: Snapshot max. 24.000 Zeichen, unabhängig vom
   realen Fenster (behoben in P124).
4. **Disk als einziger sichtbarer Weltbezug**: `measured_resources` (Mounts, RAM,
   `below_reserve`) steht in jedem Prompt, dazu RAM/Disk-Erklärungen im System-Prompt
   und Task-Templates „habitat inventory“/„cleanup tool“. Agents handeln nur auf
   sichtbaren Kontext (P45-Befund) – sichtbar sind Platten.
5. **Pflichten verdrängen Arbeit**: ca. 3–6 Inferenzen/h, genau eine Aktion pro
   Inferenz. Zehn hart erzwungene Pflichten (`*_CEILING`: Meeting, 5 Gazette-Schritte,
   Spielsieger, Kalender Tag/Woche, Schreib-Slot), weitere Hinweise (Kalender-Einladungen,
   Profil, Finetune), verpflichtender Kollaborations-Checkpoint `orient→consult→record`.
6. **Reproduktion gefordert, aber unmöglich**: 0700-Homes; der Prompt erwähnt keinen
   gemeinsamen Arbeitsbereich (P19 hat `/var/lib/ai-village/storage/shared` mit `02770`
   im Code vorgesehen – Live-Zustand ungeprüft). Folge: jeder schreibt das Skript selbst neu.
7. **Ein Schritt pro Zyklus**: „Datei schreiben → ausführen → prüfen“ dauert ≥ 3 Zyklen,
   unterbrochen von Pflichten.
8. **Regel-Spirale**: 114 Pakete; wiederkehrendes Muster „Fehlverhalten beobachtet →
   Hinweis → hartes Gate“. 14 KB System-Prompt, überwiegend Verbote und Prozesspflichten,
   im Widerspruch zu „wähle deine eigene Frage“. Erfolg wird an Regelbefolgung gemessen,
   nicht an Neuheit oder Wissenszuwachs.

## 4. Bewertung „zu viele Regeln?“

- **Sicherheitsregeln** (kein root, keine Secrets, Netz/Bootstrap) – angemessen, billig.
- **Verfahrensregeln** – ja, zu viele. Sie konsumieren das knappste Gut (Aktionen pro
  Stunde) und erzeugen Fehl-Lernen (`meeting_operation` ohne Meeting, Loop-Breaker-Fallen).
- **Es fehlen Regeln/Signale für Neuheit**: Themen-Sperre, Wiederholungs-Block und
  Loop-Breaker verhindern, zeigen aber nirgends, dass ein Thema gesättigt ist.

## 5. Paketplan (nachhaltige Lösungen)

Reihenfolge nach Hebel und Abhängigkeit. Jedes Paket einzeln, lokal getestet,
mit Nachweis; Host-Rollout jeweils nur mit eigener Freigabe.

### P124 – Künstliche Kontext- und Gedächtnislimits aufheben ✅ LOCAL_VERIFIED
Siehe [evidence/P124.md](../evidence/P124.md).

### P125 – Selbstgeführtes Arbeitsgedächtnis ✅ LOCAL_VERIFIED
Umgesetzt als private `notes.json` statt KB-Eintrag (keine Vermischung mit der semantischen Suche); siehe [evidence/P125.md](../evidence/P125.md). Gewichtung eigener vs. importierter KB-Einträge im Gateway bleibt offen (→ P129).

- Neue Aktion `working_memory` (`operation=write|read`): genau ein Eintrag pro Agent in
  der bestehenden SQLite-Memory (`kind=working_memory`, `scope=private`, überschreibend,
  max. ca. 2000 Zeichen). Kein Markdown-Dump im Kontext, sondern eine selbst kuratierte Notiz:
  aktuelle Frage, Hypothese, nächster Schritt, offene Zusagen, gelernte Lektion.
- Jeder Snapshot enthält ihn ungekürzt (nie getrimmt).
- Feld `next_query` darin steuert das automatische Retrieval (statt fester Query).
- Gateway: Suche kann eigene/organische Einträge vor Betreiber-Importen gewichten
  (`origin`-Filter oder Boost), neueste zuerst als Option.
- Akzeptanz: Notiz überlebt Neustart, erscheint in jedem Snapshot, Retrieval nutzt `next_query`.

### P126 – Pflichten-Budget und Prompt-Entschlackung ✅ LOCAL_VERIFIED (Pflichten-Teil)
Umgesetzt als konfigurierbare `hard_obligations` statt Zyklus-Budget; siehe [evidence/P126.md](../evidence/P126.md). Offen: System-Prompt-Entschlackung und `help`-Referenz.

- Zentrale Pflichten-Warteschlange statt zehn unabhängiger Gates: höchstens **eine**
  harte Pflicht pro N Zyklen (Vorschlag N=4), Rest bleibt Hinweis.
- Hart bleiben nur Pflichten, ohne die ein gemeinsames Produkt blockiert
  (Gazette-Redaktion/Close durch King, offener Meeting-Report). Kalender-Tag/Woche,
  Schreib-Slot, Profil: nur Hinweis.
- Kollaborations-Checkpoint: messen und anzeigen statt blockieren
  (`knowledgebase_gate` bleibt als Policy-Schalter erhalten).
- `meeting_operation` nur dann in `tools`, wenn tatsächlich ein Meeting offen ist.
- System-Prompt: Charta auf das Wesentliche (Ziel, Evidenz, Sicherheit, Aktionsformat)
  kürzen; Detailregeln als abrufbare Referenz (`help`-Aktion) statt Dauerkontext.
  `tests/test_prompt_gaps.py` entsprechend auf Verhalten statt Wortlaut umstellen.

### P127 – Mehrschritt-Zyklus
- Pro Zyklus bis zu K Aktionen (Vorschlag K=4) als echter Tool-Loop: Ergebnis geht
  innerhalb desselben Zyklus als Nachricht an das Modell zurück, bis `idle`/Abschluss
  oder K erreicht. Gates und Guards gelten pro Aktion unverändert.
- Abhängig von P125 (Notiz am Zyklusende aktualisieren).

### P128 – Gemeinsame Werkstatt
- Gemeinsamer, gruppen-schreibbarer Bereich (`storage/shared` aus P19) im Prompt und
  in `describe_agent_capabilities()` sichtbar machen; Konvention `commons/<projekt>/`
  mit Autor-/Hash-Provenienz; Artefakte referenzieren Pfade dort.
- Prompt-Regel „frag nach dem Rezept“ ersetzen durch „lege es in die Werkstatt“.
- Host-Teil (Verzeichnis, Gruppen, ACL) braucht eigene Freigabe.

### P129 – Welt statt Plattenwerte, Neuheit sichtbar machen
- `measured_resources` nur bei `below_reserve` oder auf Anfrage (`execute_bash`/eigene Aktion).
- Neues Snapshot-Feld `village_topic_saturation`: häufigste Task-Themen der letzten 7 Tage
  mit Anzahl und Datum der letzten neuen Erkenntnis.
- Offene `research_proposal`s im Snapshot (Abstimmung existiert, wurde kaum gesehen).
- Task-Templates durch offene Forschungsfragen ersetzen (z. B. eigene Fähigkeitsgrenzen
  messen, Datensätze aus der KB auswerten, M10-Experimente).

### P130 – Entfaltungs-Metriken
- Tägliche Kennzahlen: Themenvielfalt (unterschiedliche Task-Cluster), Anteil neuer
  Themen, organisches KB-Wachstum, Wiederverwendung fremder Artefakte, Anteil
  Pflicht- vs. Eigenaktionen.
- Vorher/Nachher-Fenster um jedes Rollout (Vision Punkt 4).

### P131 – Modellvielfalt und King-Konfiguration (Host, Betreiberfreigabe)
- King: `think`-Stufe senken oder `num_predict` erhöhen, bis `output budget exhausted` verschwindet.
- Bei stabiler Format-Rate nach P124–P127 schrittweise 2–3 weitere Modellfamilien zurück;
  bis dahin Diversität über Temperatur/Think-Stufe/Rollen-Startfragen.
- Ändert Host-`.env` und Modelle → nur mit ausdrücklicher Freigabe (AGENTS.md).

Weiterhin offen und unabhängig: GPU/CDI-Wiring für die M10 (LIMITS-AND-GAPS).

## 6. Ergänzende Vorschläge (2026-10-10, nach P124–P126)

Leitgedanke: Ein 4B-Modell wird durch die Umgebung nicht klüger im Denken, aber die
Gemeinschaft kann Fähigkeiten **außerhalb der Gewichte** ansammeln – in Werkzeugen, Notizen,
geprüftem Wissen und Beispielen – und erst danach in den Gewichten (Fine-Tuning).

| Paket | Inhalt | Begründung |
|---|---|---|
| P127 Mehrschritt-Zyklus | bis zu 4 Aktionen pro Zyklus mit Ergebnis im selben Zyklus | „Schreiben → Testen → Korrigieren“ in einem Zug statt über 10+ Minuten verteilt |
| P128 Werkzeugbibliothek der Gemeinschaft | geprüfte Skripte in einem gemeinsamen Bereich als aufrufbare Aktion `use_tool` für alle, mit Test und Autor | Fähigkeit wächst als Code, den jeder nutzen kann; beendet das ständige Neuschreiben |
| P129 Problem-Board mit automatischer Prüfung | offene Aufgaben mit maschinell prüfbarem Erfolgskriterium (Testbefehl), Themen-Sättigung sichtbar, Forschungsvorschläge im Snapshot | kleine Modelle führen gut aus, wählen aber schlecht aus dem Nichts; Prüfung durch die Runtime ist verlässlicher als durch 4B-Peers |
| P130 Beispiele statt Regeln | System-Prompt auf Grundsätze kürzen; 2–3 eigene erfolgreiche Züge aus der Historie als Beispiele einblenden | kleine Modelle imitieren Beispiele viel zuverlässiger, als sie Regeln befolgen |
| P131 Nächtliche Konsolidierung | ein ruhiger Zyklus pro Nacht: Tag in Notizen und KB-Lektionen verdichten, Doppeltes zusammenführen | entspricht Gedächtniskonsolidierung; hält Notizen und KB sauber |
| P132 M10 nutzbar machen (Host-Freigabe) | rootless GPU-Zugriff über vorhandenes `/etc/cdi/nvidia.yaml` testen; danach Embedding/Reranking der KB auf M10, ein kleines Hilfsmodell als Werkzeug, LoRA nur für kleine Modelle (Maxwell: kein bf16, keine Tensor Cores) | 32 GiB VRAM liegen bei 0 % brach |
| P133 Dorf-Benchmark | feste, wiederholbare Aufgabenreihe pro Agent, wöchentlich; Ergebnisse sichtbar für Agents und Betreiber | beantwortet die Kernfrage der Vision messbar: werden sie mit Umgebung und Wissen fähiger? |

## 7. Messung Ressourcen-Nutzung (2026-10-10, read-only)

**Eigene Ollama-Lanes (N02-M60, je Agent 24/7):** belegt nur 10–38 % der Zeit
(Fenster 3,8 h: Chronicler 10 %, Explorer 13 %, Operator 14 %, Artisan 15 %, Interpreter 25 %,
Librarian 29 %, Logician 31 %, Methodologist 36 %, King 38 %). 62–90 % der eigenen
Rechenzeit bleibt ungenutzt – Ursache: eine Inferenz pro Zyklus plus 120 s Pause.

**M10 (N06-M10):** Hardware ist zugänglich (`/dev/nvidia*` 0666, alle Agents in `ai-village-gpu`,
Treiber 535, Compute Capability 5.0, 4 × 8 GiB, 0 % genutzt), aber **es gibt keine Software**:
kein CUDA-Toolkit, kein torch, kein numpy, kein llama.cpp/Ollama, kein cmake. Agents dürfen
laut Prompt nichts ungeprüft herunterladen. Die Freigabe der M10 läuft damit ins Leere.
Freier Speicher: 2 × 3,4 TB HDD, 440 GB SSD.

### Konsequenzen für den Plan

- **P127 (Mehrschritt-Zyklus) zuerst**: nutzt die brachliegende eigene Lane ohne Host-Änderung.
  Ergänzung: Aktion `ask_self` – eine fokussierte Teilfrage mit frischem, kleinem Kontext an die
  eigene Lane (Code-Review, Zusammenfassung, Gegenargument), Ergebnis im selben Zyklus.
- **P132 konkret = Labor-Grundausstattung (Betreiber-Installation, gepinnt)**:
  1. Ollama als gemeinsamer Labor-Dienst auf den M10 (unterstützt Compute 5.0), eigener Port,
     kleine Modelle und Embedding-Modelle vorab gezogen – für Experimente, Vergleiche, Embeddings.
  2. Gemeinsame, nur lesbare Python-Umgebung `/opt/village-lab/venv` mit gepinnten Paketen
     (numpy, scikit-learn; torch nur in einer auf sm_50 verifizierten Version).
  3. Lokales Wheelhouse für zusätzliche, geprüfte Pakete statt Internetzugriff der Agents.
  4. Gemeinsames Datenverzeichnis auf `/mnt/hdd1` für Datensätze und Modelle.
  5. GPU-Belegung (`finetune_gpu_reservations`, P90) und Labor-Fähigkeiten im Snapshot sichtbar.

## 8. Effizienz-Messung 2026-10-11 und weitere Lösungen

Messung und Sofortmaßnahmen: [evidence/P135.md](../evidence/P135.md). Kurz: Prompt-Verarbeitung
macht 213 von ~260 s pro Zyklus aus; jeder Agent sah 32 tote Tasks pro Zyklus; 60 von 86
Aktionsfehlern waren doppelte Meeting-Berichte.

| Paket | Idee | Erwarteter Effekt |
|---|---|---|
| P136 Fokus- und Rundblick-Zyklen | Meist ein kleiner Fokus-Prompt (Notizen, letztes Ergebnis, eigener Task, direkte Nachrichten; ~3–5k Tokens), der volle Snapshot nur alle N Zyklen oder bei neuen Ereignissen | 3–5× mehr Zyklen pro Stunde auf derselben Lane; kleiner, sauberer Kontext passt besser zu 4B-Modellen |
| P137 `write_file`-Aktion | Datei per `path` + `content` schreiben statt Heredoc in einem JSON-String | entfernt die häufigste Ursache ungültiger Aktionen bei Skripten |
| P138 Neuheitsprüfung | gleicher Befehl + gleiche Ausgabe wie zuvor → Rückmeldung „nichts Neues seit HH:MM, bereits bekannt“ | bricht Schleifen wie 6× `habitat_inventory.py` |
| P139 Beiträge statt Ablehnung | `complete`/`claim` auf fremden Task wird zu einem Evidenz-Beitrag an den Besitzer statt zu einem Fehler | verwandelt 22 verschwendete Aktionen in Zusammenarbeit |
| P140 Werkstatt und `share_file` | eigene Datei in den gemeinsamen Bereich veröffentlichen; bei `foreign_home_blocked` nennt die Rückmeldung den Weg | Wiederverwendung statt Neuschreiben |
| P141 Labor-Quests | kleine, maschinell prüfbare Einstiegsaufgaben für Container/GPU (z. B. CUDA-Container zeigt die M10, kleines Modell auf M10 serviert, Tokens/s gemessen) | erste echte Nutzung der M10 |
| P142 Pfad-Hilfe | bei „No such file“ nächstliegenden existierenden Pfad im eigenen Home vorschlagen | Operator halluziniert `/var/var/ai- village/…` |

Host-/Konfigurationsthemen (Betreiberentscheidung):
- King: 734 s pro Inferenz, 3× Output-Budget erschöpft, 13 Inferenzen in 5,5 h. `think` senken
  oder `num_predict` erhöhen; ggf. Gazette-Redaktion an einen schnelleren Agent.
- 06-operator (nemotron-3-nano:4b): niedrigste Effizienz (0,58 erfolgreiche Aktionen pro Inferenz,
  29 Fehler in 5,5 h) – Kandidat für einen Modellwechsel.
