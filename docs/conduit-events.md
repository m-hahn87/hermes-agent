# Conduit Events — ntfy JSON-Schema v1

Maschinen-Events für Conduit Android auf Topic **`conduit-events`** (Menschen-Notifications
bleiben auf `conduit` via notify.sh — niemals mischen).

## Envelope
```json
{"event": "approval.new|approval.answered|cron.done|brief|reminder",
 "title": "...", "deep_link": "conduit://open[?...]", "session": "", "ts": "<ISO-UTC>"}
```

## Events
| event | Quelle | deep_link |
|---|---|---|
| `brief` | daily_brief.py --send | `conduit://open` |
| `approval.new` | pending.json-Writer (bald: Klaus-Frage-Queue) | `conduit://open?tab=approvals` |
| `approval.answered` | approvals.py Answer-Endpoint (web_routers) | `conduit://open?tab=approvals` |
| `cron.done` | dispatch-app.sh Review-Hook; später cron-Finish | `conduit://open` |
| `reminder` | reserviert | — |

## Werkzeug
`~/.hermes/scripts/event_publish.py --event <name> --title <t> [--deep-link <dl>] [--session <s>]`
(Validiert event-Namen, Creds aus ~/.hermes/.env, Title-Header latin-1-safe.)

## Empfänger-Kontrakt (App)
- JSON-Body parsen; unbekannte `event`-Werte ignorieren (Vorwärtskompatibilität).
- Notification: `title`, Click → `deep_link`. Fehlt `deep_link` → `conduit://open`.
