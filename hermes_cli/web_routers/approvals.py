"""Approvals-View (DUET-FEAT-002, TASK-009): read-only SSR-Liste aller offenen Approval-Gates.

Mount: web_server.py include_router. Route: GET /approvals → SSR-HTML (Cookie-Auth greift
zentral über die Middleware; diese Route braucht KEINE eigene Auth-Logik).

Quellen (Ergebnis DUET-TASK-008):
  1. Hermes TG-Pairing:  GET http://127.0.0.1:9119/api/pairing  → {"pending": [...]}
  2. Workbench blocked:  GET http://127.0.0.1:8500/api/v1/projects/{slug}/items (status==blocked)
  3. Klaus-Fragen-Queue: ~/.hermes/state/approvals/pending.json (TG-Approvals sind ephemeral)
"""
from __future__ import annotations

import html
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

PAIRING_URL = "http://100.72.227.13:9119/api/pairing"  # Dashboard bindet NUR aufs Tailnet-IF, 127.0.0.1 refused
WB_BASE = "http://127.0.0.1:8500/api/v1"
QUEUE_FILE = Path.home() / ".hermes/state/approvals/pending.json"
PROJECTS = ["klaus-duet", "hermes-conduit", "kopffrei", "alk", "kaufkumpel"]

def _fetch(url: str, timeout: float = 4.0) -> Any | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None

def _collect_gates() -> list[dict]:
    gates: list[dict] = []
    now = datetime.now(timezone.utc)

    # 1. Pairing-Gates (live)
    pairing = _fetch(PAIRING_URL)
    if pairing:
        for p in pairing.get("pending", []):
            gates.append({
                "quelle": "TG-Pairing",
                "was": str(p.get("platform", "?")) + " Pairing-Anfrage",
                "detail": json.dumps(p, ensure_ascii=False)[:200],
                "seit": "",
                "aktion": "Dashboard → Geräte (oder Klaus)",
                "pflicht": False,
            })

    # 2. Workbench blocked-Items
    for slug in PROJECTS:
        data = _fetch(f"{WB_BASE}/projects/{slug}/items")
        items = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(items, dict):
            items = items.get("items", [])
        for i in items or []:
            if i.get("status") == "blocked":
                gates.append({
                    "quelle": "Workbench",
                    "was": f"{i.get('key')}: {i.get('title', '')[:70]}",
                    "detail": i.get("body", "")[:200],
                    "seit": str(i.get("updated", ""))[:16].replace("T", " "),
                    "aktion": "Workbench öffnen oder Klaus anfunken",
                    "pflicht": False,
                })

    # 3. Klaus-Fragen-Queue
    if QUEUE_FILE.exists():
        try:
            for q in json.loads(QUEUE_FILE.read_text()):
                gates.append({
                    "id": q.get("id", ""),
                    "quelle": "Klaus-Frage",
                    "was": q.get("frage", "?"),
                    "detail": q.get("kontext", ""),
                    "seit": q.get("seit", ""),
                    "aktion": "Antworten an Klaus (Telegram oder Card)",
                    "pflicht": False,
                    "beantwortbar": True,
                })
        except Exception:
            pass
    return gates

_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<title>Approvals — hahn.cloud</title>
<style>
 body {{ font-family: system-ui, sans-serif; background: #FFFBF5; color: #1c1917; margin: 2rem; }}
 h1 {{ color: #F97316; }}
 table {{ border-collapse: collapse; width: 100%; background: #fff; border-radius: 8px; overflow: hidden; }}
 th, td {{ text-align: left; padding: 10px 14px; border-bottom: 1px solid #f0e7dc; font-size: 14px; }}
 th {{ background: #F97316; color: #fff; }}
 tr:hover td {{ background: #fff7ed; }}
 .pill {{ display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; background: #22C55E22; color: #15803d; }}
 .empty {{ padding: 2rem; text-align: center; color: #78716c; }}
 .meta {{ margin-top: 1.5rem; font-size: 12px; color: #78716c; }}
</style></head><body>
<h1>🚦 Offene Approvals</h1>
{table}
<p class="meta">Read-only · Quellen: TG-Pairing (live), Workbench blocked-Items, Klaus-Fragen-Queue · Stand {now} UTC ·
Deploy-Gates laufen ausschließlich über Telegram (Pflicht-Gate, per Konzept DUET-FEAT-002 FR3)</p>
</body></html>"""

@router.get("/approvals", response_class=HTMLResponse)
async def approvals_view():
    gates = _collect_gates()
    if not gates:
        table = '<div class="empty">🟢 Keine offenen Approvals — nichts wartet auf dich.</div>'
    else:
        rows = []
        for g in gates:
            rows.append(
                "<tr>"
                f"<td><span class='pill'>{html.escape(g['quelle'])}</span></td>"
                f"<td><strong>{html.escape(g['was'])}</strong><br><small>{html.escape(str(g.get('detail','')))}</small></td>"
                f"<td>{html.escape(str(g.get('seit','')) or '—')}</td>"
                f"<td>{html.escape(g['aktion'])}</td>"
                "</tr>"
            )
        table = ("<table><tr><th>Quelle</th><th>Was</th><th>Offen seit</th><th>Wie entscheiden?</th></tr>"
                 + "".join(rows) + "</table>")
    now = datetime.now(timezone.utc).strftime("%d.%m. %H:%M")
    return HTMLResponse(_PAGE.format(table=table, now=now))

@router.get("/api/approvals")
async def approvals_api():
    return {"gates": _collect_gates(), "count": len(_collect_gates())}

@router.post("/api/approvals/answer")
async def approvals_answer(body: dict):
    """Antwort auf eine Klaus-Frage aus der Queue (Conduit-Card / Dashboard).

    Body: {"id": "...", "answer": "..."} — trägt die Antwort in answered.json ein,
    entfernt die Frage aus pending.json. Keine Deploy-/System-Aktionen (FR3).
    """
    qid = str(body.get("id", ""))
    answer = str(body.get("answer", "")).strip()
    if not qid or not answer:
        return {"ok": False, "error": "id und answer erforderlich"}
    if QUEUE_FILE.exists():
        try:
            queue = json.loads(QUEUE_FILE.read_text())
        except Exception:
            queue = []
        rest, hit = [], None
        for item in queue:
            if item.get("id") == qid:
                hit = item
            else:
                rest.append(item)
        if not hit:
            return {"ok": False, "error": f"id {qid} nicht in der Queue"}
        from datetime import datetime, timezone
        hit["antwort"] = answer[:500]
        hit["beantwortet"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        afile = QUEUE_FILE.parent / "answered.json"
        answered = json.loads(afile.read_text()) if afile.exists() else []
        answered.append(hit)
        QUEUE_FILE.write_text(json.dumps(rest, ensure_ascii=False, indent=1))
        afile.write_text(json.dumps(answered, ensure_ascii=False, indent=1))
        # Maschinen-Event für Conduit (conduit-events) — nie den Answer-Pfad blockieren
        try:
            import subprocess, os
            subprocess.run(
                ["python3", str(Path.home() / ".hermes/scripts/event_publish.py"),
                 "--event", "approval.answered", "--title", f"Antwort: {hit.get('frage','?')[:60]}",
                 "--deep-link", "conduit://open?tab=approvals"],
                env=os.environ, capture_output=True, text=True, timeout=15)
        except Exception:
            pass
        return {"ok": True, "open": len(rest)}
    return {"ok": False, "error": "Queue-Datei fehlt"}
