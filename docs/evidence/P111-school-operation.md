# P111 – Aktion `school_operation`: der König plant Lektionen

## Auftrag

Betreiber, 2026-10-09: Der König unterrichtet, der Lehrplan ist immer eine Woche im Voraus festgelegt. P106 kann ohne eine Aktion zum Eintragen keine Lektionen erreichen.

## Änderung

- `village/actions.py`: Aktion `school_operation` mit `operation=plan`, `lesson_day` (YYYY-MM-DD), `topic`.
- `web/decision.py`: Aktion ist angenommen, Pflichtfelder sind festgelegt.
- `web/runtime.py`: Nur `01-king` darf planen. Die Lektion wird über `Curriculum.add` gespeichert, die Regel „höchstens sieben Tage voraus, nicht heute oder in der Vergangenheit“ gilt. Ereignis `school_planned`.
- `school_operation` ist von der Profil-Pflicht ausgenommen (Pflicht-Ablauf des Königs).

## Tests (LOCAL_VERIFIED)

- `tests/test_runtime.py`: `test_school_operation_is_known_and_only_the_king_may_plan`.
- Gesamte Suite: 1185 Tests OK.

## Offen

- Die Prüfung (Paket nach P111): Schüler beweisen das Gelernte. Nicht gebaut.
- Der König muss die Woche tatsächlich planen; dafür braucht er die Aufforderung im Kontext (Erinnerung aus P106).
