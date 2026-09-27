# Plan: Deterministische MCP-Werkzeuge, Pflicht-Knowledgebase und GraphRAG-Anreicherung

Date: 2026-09-27
Status: **Plan, nichts umgesetzt.** Modelle, Kontextfenster, GPU-Zuordnung und Host-`.env`
bleiben unverändert. Kein neuer externer Netzwerkzugriff, kein Dataset-Download, kein neuer
Prozess wurde in diesem Schritt gestartet.

Auftrag: „Plane die Integration eines MCP-Servers für deterministische Aufgaben (Mathe,
Subnetz-Berechnung). Zwinge jeden Agent, die Knowledgebase zu nutzen und Erkenntnisse zu
teilen; reichere GraphRAG damit an. Importiere relevante Datasets, z. B. von HuggingFace;
recherchiere und entscheide selbst, was importiert wird.“

## 0. Warum das hier ein Plan ist und keine sofortige Umsetzung

Vier Punkte aus den Projektregeln greifen direkt:

1. **Ein Paket pro Zyklus** (`AGENTS.md`): Diese Anfrage ist mindestens vier Pakete
   (Werkzeuge, Pflicht-KB, GraphRAG-Schema, Dataset-Import). Sie werden unten getrennt.
2. **„Standardbibliothek und SQLite bevorzugt“, keine unautorisierten externen Frameworks**
   (`AGENTS.md`): Ein MCP-SDK ist ein neues externes Framework. Es gibt eine
   dokumentierte Alternative ohne jede Abhängigkeit (siehe Abschnitt 1).
3. **Mandatory-Knowledgebase ist laut README explizit ein eigenes, separat zu messendes
   Paket:** „A mandatory knowledge-base gate is intentionally a separate work package so
   that its effect can be measured without silently changing the experiment.“ Ich kann das
   also nicht nebenbei erzwingen, ohne die Forschungsfrage selbst zu verändern.
4. **Externe Artefakte brauchen vorher `village-propose`** (pinned source, Ressourcenplan,
   Evaluationsplan, Cleanup-Plan) und **HuggingFace steht nicht auf der erwarteten
   Squid-Allowlist** des Pi-NAT-Layers (README: „Debian package mirrors, GitHub, Docker Hub,
   explicitly allowed Ollama endpoints, and optionally one Wikipedia language subdomain“).
   Ich habe ohnehin keinen Zugriff auf die Pi/Squid-Ebene.

Ich habe die technische Machbarkeit recherchiert (Abschnitt 6 nennt Quellen) und liefere
konkrete Empfehlungen, aber **Import und Netzwerk-Freigabe brauchen deine Entscheidung**.

## 1. P30 – Deterministische Werkzeuge (Mathe, Subnetz, Einheiten)

### Architekturfrage: echter MCP-Server vs. neue Inline-Aktion

| | Inline-Aktion (`calc_operation`) | Echter MCP-Server (JSON-RPC/stdio) |
|---|---|---|
| Abhängigkeiten | keine (Standardbibliothek) | keine, wenn selbst geschrieben (belegt: ein reiner Python-Stdlib-MCP-Server mit JSON-RPC-über-stdio ist mit ~300 Zeilen machbar, ohne SDK) |
| Passt zu den Modellen | ja – nutzt den bestehenden Aktions-/Schema-Vertrag (`village/actions.py`), den alle 9 Modelle bereits befolgen (nach P21.13) | riskant – natives Ollama-Tool-Calling ist für keines der 9 aktuell geladenen Modelle bestätigt (Qwen3/Llama-3.1+/Mistral-Small gelten als verlässlich; `ornith`, `granite4.2`, `mistral:7b`, `gemma3`, `nemotron-3-nano`, `olmo-3`, `phi4-mini-reasoning`, `llama3.2:3b` sind nicht in dieser Liste) |
| Externe Wiederverwendbarkeit | nein, nur die Village-Runtime | ja – andere MCP-Clients (auch diese Claude-Code-Session) könnten dieselben Werkzeuge nutzen |
| Neuer Prozess/Angriffsfläche | keine | ein zusätzlicher Dienst, eigenes Unit, eigene Betriebsverantwortung |

**Empfehlung:** Beides, aber in dieser Reihenfolge:
1. Zuerst `calc_operation` als neue Inline-Aktion – sofortiger Nutzen, kein Risiko, kein
   neuer Prozess, passt zum bestehenden Test-/Rollout-Muster dieser Session.
2. Danach optional ein **schlanker, abhängigkeitsfreier MCP-Server** (`scripts/mcp-tools-server.py`,
   Standardbibliothek `json`/`sys`, JSON-RPC 2.0 über stdio, keine externe MCP-SDK) als
   dünne Fassade **über dieselben Funktionen** – für externe Interoperabilität, ohne dass
   die Residents ihren Aktionsvertrag wechseln müssen.

### Funktionsumfang (rein deterministisch, keine LLM-Beteiligung, keine Netzwerkzugriffe)

- `calc`: Arithmetik/Ausdrucksauswertung ohne `eval()` (eigener kleiner Parser oder
  `ast.parse` mit strikter Knoten-Allowlist – kein beliebiger Code).
- `unit_convert`: feste Tabelle (Byte/GiB, Sekunden/Stunden, Celsius/Fahrenheit).
- `subnet_info`: CIDR → Netz-/Broadcast-Adresse, Hostanzahl, Nachbarnetze
  (Standardbibliothek `ipaddress`, kein neues Paket).
- `hash_digest`: SHA-256/512 eines übergebenen Strings (nützlich für Provenienz-Angaben,
  die der Prompt bereits von Agenten verlangt).
- `stats_summary`: Median/Mittelwert/Stddev einer Zahlenliste (Standardbibliothek `statistics`).

Jede Funktion ist rein, zustandslos, ohne Dateisystem-/Netzwerkzugriff, mit Zeit- und
Größenlimit. Ergebnisse werden nie als „vom Modell bewiesen“ behandelt – sie sind exakt,
aber die Kette „welches Problem, welche Eingabe“ bleibt Agentenverantwortung.

### Integration

- Neuer Eintrag in `village/actions.py` (`ACTION_SPECS['calc_operation']`), analog zu den
  bestehenden Operationen, mit Schema-Validierung (keine freien Strings, sondern
  `operation ∈ {calc, unit_convert, subnet_info, hash_digest, stats_summary}` plus typisierte
  Argumente).
- Neues Modul `village/tools.py` mit den fünf reinen Funktionen + Unit-Tests
  (Eingabe-Grenzfälle: Division durch Null, ungültiges CIDR, zu lange Ausdrücke).
- `Resident.execute()` bekommt einen `calc_operation`-Zweig, der `village/tools.py` aufruft
  und das Ergebnis wie jede andere Aktion protokolliert (`event('calc_result', …)`).
- Rollenpolicy (`config/runtime-policy.json`): `calc_operation` zunächst für alle Rollen
  erlauben – es ist ungefährlich und adressiert direkt eine beobachtete Schwäche
  (Logician/Operator scheiterten wiederholt an einfachen Formatfragen, nicht an Rechnen,
  aber ein verlässliches Rechenwerkzeug ist unabhängig davon nützlich, z. B. für
  Subnetz-Aufgaben, die laut deiner Anfrage im Fokus stehen).

### Akzeptanzkriterien
- Lokale Tests: korrekte Ergebnisse, harte Fehler bei ungültiger Eingabe (kein Crash,
  kein `eval`-Fallback), Zeitlimit greift.
- Kein neues Netzwerk, kein neuer Prozess für die Inline-Variante.
- Für den optionalen MCP-Server zusätzlich: Start/Stop als eigenes systemd-Unit
  (`ai-village-mcp-tools.service`), `NoNewPrivileges`, `ProtectSystem=strict`, kein
  Netzwerk-Socket (nur stdio oder `AF_UNIX`, kein `0.0.0.0`-Bind).

## 2. P31 – Pflicht-Knowledgebase-Gate (eigener Versuchsarm)

Aktuell ist Speicher-/Wissensnutzung **weich** (Checkpoint `orient`/`record`, ab drittem Verstoß
blockierend, siehe P21.6). Ein echtes „Zwingen“ verändert das Forschungsdesign – deshalb laut
README explizit ein eigenes, gemessenes Paket, kein Stiller Default.

### Vorschlag
- Neuer Policy-Schalter `knowledgebase_gate: mandatory|advisory` (Default weiterhin `advisory`,
  identisch zum heutigen Verhalten – keine stille Verhaltensänderung).
- Im `mandatory`-Modus verlangt `guard()`: Vor jedem `execute_bash`/`start_job`/`task_operation
  complete` **muss** in den letzten N Minuten ein `memory_search` **und** nach jedem
  abgeschlossenen Arbeitsschritt ein `memory_remember` mit `scope=shared` und einem
  Provenienzfeld (Quelle, Zeitstempel, Unsicherheit – der Prompt fordert das inhaltlich
  schon, aber nicht technisch erzwungen) vorliegen. Das ist eine Verschärfung der
  bestehenden `village/collaboration.py`-Checkpoints, kein neuer Mechanismus.
- **Messdesign (Pflicht laut `docs/RESEARCH-PROTOCOL.md`-Stil):** zwei Bedingungen
  (`advisory` vs. `mandatory`) über einen definierten Zeitraum, gleiche Modelle, verglichen
  auf: Anteil `memory_remember` mit Scope `shared`, tatsächliche Retrieval-Trefferquote in
  `retrieved_memory_untrusted`, verifizierte Task-Abschlüsse, `invalid_decision`-Rate (Risiko:
  ein hartes Gate kann kleine Modelle in Sackgassen zwingen – deshalb weiterhin mit der
  bestehenden Drei-Fehlversuche-Kulanz aus P21.6, nicht sofort blockierend).
- **Nie fabrizieren:** Das Gate erzwingt die *Handlung* (Suche/Schreiben), nie den *Inhalt*.
  Ein Agent, der ehrlich „nichts Neues gefunden“ schreibt, erfüllt die Pflicht.

### Akzeptanzkriterien
- Default (`advisory`) ändert nichts am aktuellen Verhalten – Regressionstest dafür.
- `mandatory` blockiert nachweislich nur bei fehlender Suche/Aufzeichnung, nie bei
  inhaltlicher Ablehnung eines Vorschlags.
- Getrennter Nachweis `docs/evidence/P31.md` mit A/B-Messwerten, bevor `mandatory` zum
  neuen Default wird (das wäre eine eigene, spätere Betreiberentscheidung).

## 3. P32 – GraphRAG-Anreicherung aus gewonnenen Erkenntnissen

Die Neo4j-Anbindung existiert bereits (`memory/neo4j_adapter.py`, P17): Knoten `Agent`,
`Memory`, `Task`, `Artifact`, Kanten `observed`/`claimed`/`inferred`, reine
Standardbibliotheks-HTTP-Anbindung an die Cypher-Transactional-API, kein Treiber.

### Was fehlt für „GraphRAG“ im eigentlichen Sinn (Retrieval über Graph-Relationen, nicht nur Knoten)
1. **Beziehungen zwischen Erkenntnissen**, nicht nur Erkenntnis→Autor. Neue Kantentypen:
   `relates_to` (Memory→Memory, mit `similarity`- oder `explicit_reference`-Grund),
   `about_task` (Memory→Task), `verified_by` (Artifact→Agent, existiert konzeptuell
   schon über Artifact-Store, aber nicht im Graphen).
2. **Graph-gestützte Retrieval-Erweiterung** in `Resident.snapshot()`: zusätzlich zur
   heutigen Vektor-/Lexikalsuche (`/v1/search`) ein optionaler zweiter Aufruf
   `/v1/graph/related?memory_id=…`, der 1-2 Hops im Graphen folgt (z. B. „Memories, die
   dasselbe Task betreffen, auch wenn der Wortlaut nicht ähnlich ist“). Rückgabe bleibt
   im bestehenden `retrieved_memory_untrusted`-Format, gleiches Zeichenbudget.
3. **Anti-Fabrikations-Grenze (kritisch):** Der Graph darf nur Kanten erhalten, die aus
   *bestätigten* Runtime-Events stammen (z. B. „Agent X hat Memory Y in Task Z geschrieben“
   – aus dem Outbox-Eintrag ableitbar), nie aus einer Heuristik, die selbst wieder ein
   LLM wäre. Kein automatisches „Ähnlichkeits“-Kantenschema ohne harten Schwellenwert und
   Nachvollziehbarkeit (sonst entsteht ein zweiter, unkontrollierter „Wissens“-Layer, der
   dem Prinzip „SQLite ist die einzige Autorität“ widerspricht).
4. **Verifikation bleibt SQLite-first:** genau wie heute – jede Graph-Antwort wird vor
   Auslieferung gegen SQLite geprüft (Muster existiert schon in `neo4j_adapter.py` für
   Provenienzabfragen).

### Akzeptanzkriterien
- Neue Kantentypen mit Migrations-/Rebuild-Test (wie P15/P17 bereits vorschreiben:
  vollständiger Rebuild aus SQLite ohne Datenverlust).
- Graph-Fallback: bei Neo4j offline liefert `snapshot()` exakt das heutige Verhalten
  (reine Vektor-/Lexikalsuche), kein Fehler, kein Blockieren.

## 4. P33 – Dataset-Import (Governance vor Ausführung)

### Netzwerklage (harte Grenze)
HuggingFace (`huggingface.co`, CDN-Hosts) steht **nicht** auf der im README beschriebenen
Squid-Allowlist. Ich habe keinen Zugriff auf die Pi/NAT/Squid-Ebene und kann diese Freigabe
nicht selbst erteilen. Zwei Wege, keiner davon impliziert durch diese Anfrage bereits erledigt:

- **(a) Freigabe des Egress** für einen gepinnten HuggingFace-Host/-Pfad (analog zur
  bestehenden Wikipedia-Ausnahme) – deine Entscheidung, betrifft die externe Firewall.
  Dann Erweiterung von `village/research.py`: neuer `source="huggingface"`, gleiche
  Read-only-/Größen-/Zeitlimit-Disziplin wie bei Wikipedia/GitHub/Docker Hub.
- **(b) Ich lade außerhalb der Village** (in dieser Sitzung, mit separatem Internetzugriff)
  eine konkret benannte, kleine, lizenzgeprüfte Datei herunter und lege sie **nach
  Freigabe** unter `/mnt/…` in die Kommons ab – kein Agent greift je direkt auf
  huggingface.co zu. Das passt zum bestehenden „Kommons“-Modell des Repos
  (`/mnt`-Storage) und braucht keine Firewall-Änderung, aber weiterhin dein Ok zu
  Quelle, Lizenz und Speicherbudget, plus ein `village-propose`-Eintrag mit Digest.

### Recherchestand (Suchergebnisse, keine abschließende Prüfung)
Eine gezielte Suche nach Linux-/Netzwerk-/Subnetz-spezifischen Datasets ergab keinen
offensichtlichen Treffer mit klar passender Lizenz und Größe. Zwei Kandidaten für eine
**vertiefte** Prüfung (Lizenz, Größe, tatsächlicher Inhalt), nicht für einen Blind-Import:

| Dataset | Bezug zum Projekt | Zu prüfen |
|---|---|---|
| `NetoAISolutions/NetBench` | Netzwerk-/Telekom-Diagnostik für kleine Modelle, nennt explizit Governance/Bias/Erklärbarkeit | Lizenz, Größe, ob Inhalt (Telekom-Support) zur Subnetz-/Sysadmin-Aufgabe passt oder zu domänenfremd ist |
| HuggingFaceTB „Reasoning datasets“-Kollektion | Allgemeine Reasoning-/Mathe-Aufgaben, könnte die neuen `calc_operation`-Werkzeuge ergänzen (Trainingsdaten für spätere Auswertung, nicht für Fine-Tuning der festen Modelle) | Lizenz je Unterdataset unterschiedlich, Größe stark variierend |

**Ich empfehle explizit, noch nichts zu importieren**, bis (a) oder (b) entschieden ist und
ein konkretes Dataset mit Lizenz/Digest/Größe vorliegt. Ein Import in eine „Wissensbasis“,
die Agenten als Grundlage lesen, ist ein Vergiftungsrisiko (Prompt-Injection über
Trainingsdaten, falsche „Fakten“) – deshalb dieselbe Sorgfalt wie bei jedem externen
Artefakt (`village-propose`, Hash-Pin, Stichprobenprüfung vor Freigabe für Agenten).

### Wie ein freigegebenes Dataset in die Knowledgebase kommt (Zielbild)
1. Datei liegt unter `/mnt/ssd-data/datasets/<name>-<digest>.jsonl` (unveränderlich, Digest
   im Dateinamen).
2. Ein neues, kleines Offline-Skript (`scripts/import-dataset.py`, Standardbibliothek) liest
   die Datei zeilenweise und schreibt über den **bestehenden** Memory-Gateway-Endpunkt
   `/v1/memories` mit `scope=shared`, `kind=reference`, `source_event="dataset:<name>:<digest>"` –
   also über denselben Pfad, den auch Agenten nutzen, keinen Seitenkanal in die DB.
3. Rate-/Quota-Limits des Gateways (`MEMORY_WRITES_PER_HOUR`) gelten unverändert; ein
   Bulk-Import braucht einen expliziten, befristeten Quota-Ausnahme-Parameter, kein
   generelles Aufweichen der Agenten-Quote.
4. Projektion nach Chroma/Neo4j läuft über die bestehende Outbox – keine neue Pipeline.

## 5. Reihenfolge und Abhängigkeiten

```
P30 (Werkzeuge, kein Netzwerk, sofort testbar)
  → P32 (GraphRAG-Kantenschema, baut auf bestehendem Neo4j-Adapter)
      → P31 (Pflicht-Gate, A/B-Messung, verändert das Experiment bewusst)
P33 (Dataset-Import) läuft parallel, aber blockiert auf deine Entscheidung zu
  Netzwerk-Freigabe (a) vs. Offline-Download durch mich (b) plus Lizenz-/Digest-Prüfung.
```

## 6. Recherchequellen (WebSearch, 2026-09-27)

- Zero-Dependency-MCP-Server in reinem Python: [dev.to/ameobius](https://dev.to/ameobius/building-zero-dependency-mcp-servers-in-pure-python-42cd)
- MCP-Spezifikation (JSON-RPC/stdio-Transport): [modelcontextprotocol.io](https://modelcontextprotocol.io/specification/2025-03-26/basic), [modelcontextprotocol.info/docs/concepts/transports](https://modelcontextprotocol.info/docs/concepts/transports/)
- Offizielles Python-SDK (Alternative, falls doch gewünscht): [github.com/modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk)
- Ollama-Tool-Calling-Reife/Modellabdeckung: [localaimaster.com/blog/ollama-function-calling-tools](https://localaimaster.com/blog/ollama-function-calling-tools), [localaimaster.com/blog/best-ollama-models-tool-calling](https://localaimaster.com/blog/best-ollama-models-tool-calling)
- HuggingFace-Datasets allgemein: [huggingface.co/docs/datasets](https://huggingface.co/docs/datasets/en/index)
- NetBench-Dataset: [huggingface.co/datasets/NetoAISolutions/NetBench](https://huggingface.co/datasets/NetoAISolutions/NetBench)
- Reasoning-Datasets-Kollektion: [huggingface.co/collections/HuggingFaceTB/reasoning-datasets](https://huggingface.co/collections/HuggingFaceTB/reasoning-datasets)

## 7. Von dir zu entscheiden, bevor ich weiter umsetze

1. **P30 starten?** Risikoarm, keine Freigabe nötig außer der üblichen Host-Rollout-Freigabe.
2. **MCP-Server wirklich als eigener Prozess/Protokoll**, oder reicht die Inline-Aktion
   (Empfehlung: zuerst Inline, MCP-Fassade später optional)?
3. **P31 (Pflicht-Gate):** als eigener, klar dokumentierter Versuchsarm – einverstanden mit
   der A/B-Messung statt sofortigem harten Default?
4. **P33 Netzwerk:** Egress-Freigabe für einen gepinnten HuggingFace-Host (a) oder Download
   durch mich außerhalb der Village mit anschließender manueller Freigabe (b)?
5. **P33 Umfang:** Reicht die Kandidatenliste zur vertieften Prüfung, oder hast du bereits
   ein konkretes Dataset im Kopf?
