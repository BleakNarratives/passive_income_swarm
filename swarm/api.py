"""
swarm.api — HTTP surface for the control plane
==============================================

The same operations the CLI offers, exposed over HTTP so the swarm can be
driven by a dashboard, a cron job, or another agent. FastAPI is an *optional*
dependency: this module imports cleanly without it and only raises when you
actually try to build the app. That keeps the core importable on the bare
Python the daemons run on.

    queue = app.router  (mounted)          # or standalone:
    uvicorn swarm.api:app
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .cli import Context, seed_skills
from .forecast import PredictiveEngine
from .scouts import ScoutEngine
from .supervisor import Supervisor

try:  # pragma: no cover - exercised only when fastapi is installed
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel

    HAVE_FASTAPI = True
except Exception:  # pragma: no cover
    FastAPI = None  # type: ignore
    HTTPException = None  # type: ignore
    BaseModel = object  # type: ignore
    HAVE_FASTAPI = False

__all__ = ["create_app", "app", "HAVE_FASTAPI"]


def _require_fastapi() -> None:
    if not HAVE_FASTAPI:
        raise RuntimeError(
            "FastAPI is not installed. Install it (pip install fastapi uvicorn) "
            "or use the CLI: python3 -m swarm.cli"
        )


if HAVE_FASTAPI:  # pragma: no cover - only compiled when fastapi exists

    class PullRequest(BaseModel):
        slot: str
        payload: Dict[str, Any] = {}
        idem: Optional[str] = None
        force: bool = False

    class RunRecord(BaseModel):
        skill: str
        reward: float
        ok: bool = True

    class CompleteRequest(BaseModel):
        ticket: str
        result: Dict[str, Any] = {}
        ok: bool = True


def create_app(root: str | Path = ".", manifest: Optional[str] = None) -> Any:
    """Build the FastAPI app bound to a project root and optional manifest."""
    _require_fastapi()
    ctx = Context(Path(root), Path(manifest) if manifest else None)
    seed_skills(ctx)

    application = FastAPI(title="Norexa Swarm Control Plane", version="0.1.0")

    @application.get("/health")
    def health() -> Dict[str, Any]:
        return {
            "status": "online",
            "occupants": len(ctx.registry),
            "events": ctx.ledger.count(),
            "skills": len(ctx.skills.all()),
        }

    @application.get("/cabinet")
    def cabinet() -> Dict[str, Any]:
        return {"rendered": ctx.machine.render_cabinet(), "stock": ctx.machine.stock()}

    @application.post("/pull")
    def pull(req: "PullRequest") -> Dict[str, Any]:
        try:
            ticket = ctx.machine.pull(req.slot, payload=req.payload, idem=req.idem, force=req.force)
        except Exception as exc:  # surface vend errors as 400
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ticket.__dict__

    @application.get("/tickets")
    def tickets() -> Dict[str, Any]:
        return {"pending": [t.__dict__ for t in ctx.machine.pending()]}

    @application.post("/complete")
    def complete(req: "CompleteRequest") -> Dict[str, Any]:
        try:
            ticket = ctx.machine.complete(req.ticket, result=req.result, ok=req.ok)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ticket.__dict__

    @application.get("/skills")
    def skills() -> Dict[str, Any]:
        return {
            "summary": ctx.skills.summary(),
            "leaderboard": [
                {"id": s.id, "level": s.level, "score": round(s.score, 4), "runs": s.runs}
                for s in ctx.skills.leaderboard(20)
            ],
        }

    @application.post("/skills/record")
    def record(req: "RunRecord") -> Dict[str, Any]:
        try:
            skill = ctx.skills.record_run(req.skill, req.reward, ok=req.ok)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": skill.id, "score": skill.score, "level": skill.level, "runs": skill.runs}

    @application.get("/scout")
    def scout(apply: bool = False) -> Dict[str, Any]:
        engine = ScoutEngine(ctx.skills, ctx.ledger, registry=ctx.registry, forecast=ctx.engine)
        result = engine.run(apply=apply)
        return {k: [f.to_dict() for f in v] for k, v in result.items()}

    @application.get("/forecast")
    def forecast(metric: str = "reward", horizon: int = 3) -> Dict[str, Any]:
        series = ctx.engine.reward_series() if metric == "reward" else ctx.engine.revenue_series()
        return ctx.engine.forecast(series, horizon=horizon, metric=metric)

    @application.get("/supervise")
    def supervise() -> Dict[str, Any]:
        sup = Supervisor(ctx.registry, ctx.ledger, state_path=ctx.root / "logs" / "supervisor_state.json")
        return sup.tick()

    return application


#: Convenience app for `uvicorn swarm.api:app`.
try:  # pragma: no cover
    app = create_app(".") if HAVE_FASTAPI else None
except Exception:  # pragma: no cover
    app = None
