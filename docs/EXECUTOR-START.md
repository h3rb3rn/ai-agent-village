# Startprompt für das ausführende LLM

Diesen Block in eine neue Sitzung im Repository `/opt/deployment/ai-village` kopieren.
Der Plan ist eine Spezifikation, kein Auftrag, den pausierten Produktionsbetrieb zu starten.

```text
Arbeite am AI-Village-Projekt nach docs/GAP-CLOSURE-PLAN.md.

1. Beachte die Repository-/AGENTS.md-Regeln. Falls verfügbar, zuerst den
   SessionMesh-Handoff lesen; historische Aussagen lokal verifizieren.
2. Lies die Abschnitte 1–3 und 5 des Plans sowie docs/GAP-CLOSURE-STATUS.md.
   Lies anschließend nur das nächste freigegebene Paket und dessen nötige Dateien.
3. Prüfe git status/diff. Hier liegen wichtige uncommittete Vorarbeiten.
   Nichts zurücksetzen oder fremde Änderungen übernehmen/überschreiben.
4. Die Simulation wurde vom Betreiber gestoppt, weil er die Host-.env bearbeitet.
   Nicht starten, keine echten LLM-Anfragen, keine Host-.env synchronisieren,
   keine Modelle/CTX-Werte ändern. Verlasse dich nicht darauf, dass ein Stop
   bereits rebootfest ist; das ist erst P01.
5. Implementiere genau EIN Paket oder einen klar benannten kleinen Teil davon.
   Schreibe passende Tests zuerst oder gemeinsam mit dem Patch. Keine Zusatzfeatures,
   keine pauschale Neuentwicklung des Dashboards, kein neuer Cluster-Stack.
6. Lokale Mock-Tests sind Standard. Host-Schreibzugriffe, Reboots, GPU-Tests,
   Änderungen am N02-Proxy, Cloudkosten und Wiederanlauf brauchen eigene Freigabe.
   Der Plan allein erteilt sie nicht. Nicht genehmigte Tests als offen dokumentieren.
7. Verwende nur die beabsichtigten Dateien. Falls die Aufgabe wesentlich mehr
   Komponenten umfasst, teile sie in Pxx.1/Pxx.2, beschreibe den Schnitt und stoppe
   am getesteten Übergabepunkt. Kein riesiger Sammelpatch.
8. Aktualisiere docs/GAP-CLOSURE-STATUS.md und einen echten Nachweis unter
   docs/evidence/Pxx.md. Erfinde keine Testausgaben oder Testerfolge.
   LOCAL_VERIFIED heißt nicht ausgerollt; active heißt nicht funktionierender Agent.
9. Bei fehlender Voraussetzung notiere den exakten Blocker und das benötigte
   nächste Ereignis. Kein unspezifisches „braucht mehr Zeit“ und kein stilles Skip.
10. Gib am Ende knapp an: Änderungen, Tests, offene Risiken, Deploymentstatus,
    nächstes Paket. Commit/Push nur, wenn dafür ausdrücklich ein Auftrag besteht.

Beginne mit P00. Setze spätere Tickets nicht aufgrund alter Chatberichte auf fertig.
```

## Empfohlene Aufträge danach

Für ein normales lokales Paket:

> Bearbeite Pxx aus `docs/GAP-CLOSURE-PLAN.md`. Prüfe zuerst die Abhängigkeiten und
> den Status. Nur lokale Implementierung und isolierte Tests; Simulation bleibt
> gestoppt. Stoppe nach dem dokumentierten Abnahmebericht dieses Pakets.

Für eine Review ohne Änderungen:

> Prüfe den Patch und die Nachweise für Pxx gegen dessen Akzeptanzkriterien.
> Suche insbesondere nach Datenverlust, Secret-Leaks, unbeabsichtigten Starts,
> übersprungenen Fehlerfällen und Tests, die nur die Implementierung nacherzählen.
> Keine Host-Änderungen. Nenne konkrete Befunde mit Datei und Reproduktion.

Ein Host-Auftrag muss zusätzlich Zielhost, erlaubte Dienste/Dateien, Wartungsfenster,
Backup, Rollback und erlaubte Tests benennen. Eine reine Installationsfreigabe ist
keine Freigabe zum Start aller Agenten oder zur Nutzung kostenpflichtiger APIs.

## Warum diese Aufteilung?

Ein kleines Modell soll möglichst wenig gleichzeitig im Kontext halten:
ein Defekt, ein begrenzter Patch, ein Testnachweis. Zustandsdatei und IDs verhindern,
dass es bereits erledigte Arbeit wiederholt oder historische Behauptungen für
einen aktuellen Test hält. Die Gates schützen die laufenden Daten und das Dashboard.
