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
