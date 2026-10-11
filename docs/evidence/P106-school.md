# P106 – Schule: tägliche Ankündigung und Planungs-Erinnerung

## Auftrag

Betreiber, 2026-10-09: Schule für alle verbindlich, King unterrichtet, Lehrplan immer eine Woche im Voraus.

## Änderung

- `village/school.py`: `daily_announcement()` (Lektion des Tages als Text an alle), `planning_reminder()` (nennt die ungeplanten Tage der nächsten sieben).
- `web/runtime.py`: Im Zyklus des Königs wird die Lektion des Tages einmal täglich als Board-Nachricht gepostet; bei Lücken in der Wochenplanung bekommt der König einmal täglich eine Erinnerung. Fehler stoppen den Zyklus nicht (`school_failed`).
- `village/release.py` und `scripts/install-runtime.py`: `school.py` ist in der Installationsliste.

## Tests (LOCAL_VERIFIED)

- `tests/test_school.py`: acht Tests (Lücken, Wochengrenze, Ankündigung, Erinnerung).
- Gesamte Suite: 1182 Tests OK.

## Offen

- **Der König kann noch keine Lektionen eintragen.** Dafür braucht es eine Aktion `school_operation` (Paket P111). Ohne sie bleibt der Plan leer, und die Erinnerung meldet die volle Woche als offen.
- Prüfung (P107 nach dem Schulplan) ist nicht gebaut.
