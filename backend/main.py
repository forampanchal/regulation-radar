"""FastAPI app: REST API for the dashboard + serves the single-file React frontend.

Endpoints
  GET  /api/regulations              list tracked regulations (+ staged proposals)
  GET  /api/runs                     recent agent-run audit log
  POST /api/regulations/{id}/check   run the fetch->diff->decide->update loop
  POST /api/regulations/{id}/approve human accepts the staged proposal (guidance updates)
  POST /api/regulations/{id}/reject  human rejects it (guidance kept, baseline moved fwd)
  POST /api/regulations/{id}/simulate-drift   DEMO ONLY: rewind baseline to pre-2009 text
"""
import os
import re

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from . import agent
from . import seed

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")

app = FastAPI(title="Regulation Radar")


@app.on_event("startup")
def _startup():
    seed.seed_if_empty()


@app.get("/api/regulations")
def get_regulations():
    return db.list_regulations()


@app.get("/api/runs")
def get_runs():
    return db.list_runs()


@app.post("/api/regulations/{reg_id}/check")
def check(reg_id: int):
    if not db.get_regulation(reg_id):
        raise HTTPException(404, "regulation not found")
    return agent.run_check(reg_id)


@app.post("/api/regulations/{reg_id}/approve")
def approve(reg_id: int):
    reg = db.get_regulation(reg_id)
    if not reg:
        raise HTTPException(404, "regulation not found")
    if reg["status"] != "needs review" or reg["pending_summary"] is None:
        raise HTTPException(400, "nothing staged to approve")
    db.update_regulation(
        reg_id,
        plain_summary=reg["pending_summary"],
        raw_excerpt=reg["pending_excerpt"],
        content_hash=reg["pending_hash"],
        status="current",
        human_reviewed=1,
        last_reviewed=db.now_iso(),
        pending_summary=None,
        pending_excerpt=None,
        pending_hash=None,
        pending_reason=None,
    )
    db.log_run(
        reg_id, "approved",
        "Human approved the proposed change — it is now the live guidance.",
        changed=True, material=True, model_mode="human", actor="human",
    )
    return {"ok": True, "status": "current"}


@app.post("/api/regulations/{reg_id}/reject")
def reject(reg_id: int):
    reg = db.get_regulation(reg_id)
    if not reg:
        raise HTTPException(404, "regulation not found")
    if reg["status"] != "needs review" or reg["pending_summary"] is None:
        raise HTTPException(400, "nothing staged to reject")
    # Keep the human guidance, but move the source baseline forward so we don't
    # re-flag the same diff on every future check.
    db.update_regulation(
        reg_id,
        content_hash=reg["pending_hash"],
        raw_excerpt=reg["pending_excerpt"],
        status="current",
        pending_summary=None,
        pending_excerpt=None,
        pending_hash=None,
        pending_reason=None,
    )
    db.log_run(
        reg_id, "rejected",
        "Human rejected the proposed change — existing guidance kept; baseline advanced.",
        changed=True, model_mode="human", actor="human",
    )
    return {"ok": True, "status": "current"}


@app.post("/api/regulations/{reg_id}/simulate-drift")
def simulate_drift(reg_id: int):
    """DEMO ONLY. Rewind the stored baseline to the historical pre-2009 600 ppm text so
    the next real check detects the tightening to 90 ppm as a material change. Lets you
    show the changed -> needs-review -> approve flow on camera without waiting for the CFR
    to actually change. Not part of the production loop."""
    import hashlib

    reg = db.get_regulation(reg_id)
    if not reg:
        raise HTTPException(404, "regulation not found")
    if not reg["raw_excerpt"]:
        raise HTTPException(400, "no baseline to drift; run a check first")
    if "0.009 percent" in reg["raw_excerpt"]:
        # Lead rule: rewind to the realistic historical pre-2009 600 ppm limit.
        old_text = re.sub(r"0\.009\s*percent", "0.06 percent", reg["raw_excerpt"])
        old_text += " [demo: baseline rewound to historical pre-2009 limit]"
    else:
        # Any other rule: inject a stale numeric threshold so the next real fetch
        # (which lacks it) registers a material numeric-limit change.
        old_text = reg["raw_excerpt"] + " Note: a prior 25 ppm threshold applied. [demo: stale baseline]"
    norm = re.sub(r"\s+", " ", old_text.lower()).strip()
    db.update_regulation(
        reg_id,
        raw_excerpt=old_text,
        content_hash=hashlib.sha256(norm.encode("utf-8")).hexdigest(),
        status="current",
    )
    return {"ok": True, "note": "baseline rewound to 0.06% (600 ppm); run a check to detect drift"}


# --- static frontend (mounted last so it doesn't shadow /api routes) --------- #
@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
