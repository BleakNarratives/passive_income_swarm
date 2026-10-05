"""
swarm.cli — ``swarmctl``
========================

One command surface over the whole ecosystem. Every subcommand is a thin
wrapper over a library call, so anything the CLI does is scriptable and
testable without it.

    swarmctl cabinet
    swarmctl pull B3
    swarmctl skills leaderboard
    swarmctl scout run --apply
    swarmctl forecast revenue --horizon 12
    swarmctl supervise --ticks 3
    swarmctl doctor
    swarmctl demo
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .entities import Entity
from .forecast import ModelRegistry, PredictiveEngine
from .ledger import Ledger
from .registry import Registry, RegistryError
from .scouts import ScoutEngine
from .skills import MasteryGate, Skill, SkillStore
from .supervisor import Supervisor
from .breaker import IngestBreaker
from .contracts import Envelope, PayloadError, parse as parse_envelope
from .discovery import ScoutRegistry
from .graft import GraftGuard
from .incentives import ScoutIncentives
from .ingest import Ingestor
from .lineage import (
    PURPOSE_SELF_MODIFY,
    PURPOSE_TELEMETRY,
    LineageGate,
    reconcile_with_proofs,
)
from .vending import VendingMachine, VendError
from .whorl import WhorlBus, reconcile_bus_path

__all__ = ["main", "build_context", "SEED_SKILLS"]

#: Skills the shipped cabinet references. Seeded so the store is never empty.
SEED_SKILLS: List[Dict[str, Any]] = [
    {"id": "nip90_serve", "name": "Serve NIP-90 Job", "capabilities": ["net.nostr", "llm.generate"]},
    {"id": "groq_inference", "name": "Groq Inference", "capabilities": ["llm.generate"]},
    {"id": "lnurl_invoice", "name": "LNURL Invoice", "capabilities": ["pay.lnurl", "net.http"]},
    {"id": "settlement_poll", "name": "Settlement Poll", "capabilities": ["pay.lnurl"]},
    {"id": "task_route", "name": "Task Route", "capabilities": ["net.http", "data.ledger"]},
    {"id": "health_probe", "name": "Health Probe", "capabilities": ["data.ledger"]},
    {"id": "breaker_trip", "name": "Breaker Trip", "capabilities": ["data.ledger"]},
    {"id": "mutate_dna", "name": "Mutate DNA", "capabilities": ["shell.exec", "fs.write"]},
    {"id": "longform_writing", "name": "Longform Writing", "capabilities": ["llm.generate"]},
    {"id": "hook_craft", "name": "Hook Craft", "capabilities": ["llm.generate"]},
    {"id": "line_edit", "name": "Line Edit", "capabilities": ["llm.generate", "fs.read"]},
    {"id": "market_analysis", "name": "Market Analysis", "capabilities": ["llm.generate", "net.http"]},
    {"id": "opportunity_rank", "name": "Opportunity Rank", "capabilities": ["data.ledger"]},
    {"id": "profile_optimize", "name": "Profile Optimize", "capabilities": ["llm.generate", "fs.write"]},
    {"id": "listing_copy", "name": "Listing Copy", "capabilities": ["llm.generate"]},
    {"id": "pricing_tune", "name": "Pricing Tune", "capabilities": ["data.ledger"]},
    {"id": "summarize", "name": "Summarize", "capabilities": ["llm.generate"]},
    {"id": "source_rank", "name": "Source Rank", "capabilities": ["net.http"]},
    {"id": "ad_copy", "name": "Ad Copy", "capabilities": ["llm.generate"]},
    {"id": "channel_select", "name": "Channel Select", "capabilities": ["data.ledger"]},
    {"id": "smell_sniff", "name": "Smell Sniff", "capabilities": ["fs.read", "shell.exec"]},
    {"id": "doc_drift_detect", "name": "Doc Drift Detect", "capabilities": ["fs.read"]},
    {"id": "rival_scan", "name": "Rival Scan", "capabilities": ["market.search", "repo.gh"]},
    {"id": "pattern_extract", "name": "Pattern Extract", "capabilities": ["data.ledger"]},
    {"id": "lead_mine", "name": "Lead Mine", "capabilities": ["market.search"]},
    {"id": "signal_score", "name": "Signal Score", "capabilities": ["data.ledger"]},
    {"id": "pattern_mine", "name": "Pattern Mine", "capabilities": ["data.ledger"]},
    {"id": "skill_propose", "name": "Skill Propose", "capabilities": ["fs.write"]},
    {"id": "forecast", "name": "Forecast", "capabilities": ["data.ledger"]},
    {"id": "anomaly_detect", "name": "Anomaly Detect", "capabilities": ["data.ledger"]},
]


class Context:
    def __init__(
        self,
        root: Path,
        manifest: Optional[Path] = None,
        registry_path: Optional[str] = None,
        quarantine_dir: str = "/tmp/quarantine",
        bus_path: Optional[str] = None,
        allowed_roots: Optional[List[str]] = None,
        denied_roots: Optional[List[str]] = None,
    ):
        self.root = root
        self.ledger = Ledger(root / "logs" / "swarm.jsonl")
        if manifest:
            self.registry = Registry.load(manifest)
        else:
            local = root / "swarm" / "cabinet.json"
            self.registry = Registry.load(local) if local.exists() else Registry.default()
        self.skills = SkillStore(
            root / "skills" / "registry.json",
            ledger=self.ledger,
            gate=MasteryGate(),
        )
        self.models = ModelRegistry(root / "data" / "models" / "registry.json")
        self.machine = VendingMachine(self.registry, self.ledger, root=root)
        self.engine = PredictiveEngine(ledger=self.ledger, registry=self.models)

        # Relay 2 — one bus for telemetry and pheromones (path overridable so the
        # ingestor can be pointed at core_framework's real bus.jsonl).
        self.bus = WhorlBus(Path(bus_path) if bus_path else root / "bus.jsonl", ledger=self.ledger)
        # Relay 3 — live-lineage gate over self-modification (roots overridable
        # to match PROOFS_AND_GATES.md without a code edit).
        self.lineage = LineageGate(
            allowed=tuple(allowed_roots) if allowed_roots else None,
            denied=tuple(denied_roots) if denied_roots else None,
        )
        # Incentive model — reward novelty, rarity and marketable niche intel.
        self.incentives = ScoutIncentives(store=self.skills, registry=self.registry)
        # Janus Guard — hard-lock breaker (holds the bus so a trip can flush it).
        self.breaker = IngestBreaker(root / "circuit_breaker.json", bus=self.bus)
        # Enforce the telemetry contract inside the bus itself.
        self.bus.attach_breaker(self.breaker)
        # Dynamic registry auto-discovery (queryable in strict mode).
        self.scout_registry = ScoutRegistry(
            path=registry_path, quarantine_dir=quarantine_dir
        )
        # Auto-sync: register the digest + schema of any present, allowed scout
        # script so first-run registration needs no manual step.
        try:
            self.scout_registry.sync_entities(self.registry)
        except Exception:
            pass
        self.ingestor = Ingestor(
            self.bus,
            self.ledger,
            gate=self.lineage,
            store=self.skills,
            incentives=self.incentives,
            breaker=self.breaker,
            registry=self.scout_registry,
        )
        # Anti-lobotomy state preservation around winner/loser grafts.
        self.graft = GraftGuard(
            bus=self.bus, ledger=self.ledger, adaptive_memory_path=root / "adaptive_memory.json"
        )
        # Preserve decay/yield before any gate evaluation, not just deprecation.
        self.lineage.attach_guard(self.graft, preserve_into="live_lineage")


def build_context(args: argparse.Namespace) -> Context:
    root = Path(getattr(args, "root", ".") or ".").resolve()
    manifest = getattr(args, "manifest", None)
    return Context(
        root,
        Path(manifest) if manifest else None,
        registry_path=getattr(args, "registry_path", None),
        quarantine_dir=getattr(args, "quarantine_dir", "/tmp/quarantine") or "/tmp/quarantine",
        bus_path=getattr(args, "bus_path", None),
        allowed_roots=getattr(args, "allowed_root", None),
        denied_roots=getattr(args, "denied_root", None),
    )


def seed_skills(ctx: Context) -> int:
    added = 0
    for spec in SEED_SKILLS:
        if ctx.skills.get(spec["id"]) is not None:
            continue
        ctx.skills.register(
            Skill(
                id=spec["id"],
                name=spec["name"],
                capabilities=list(spec.get("capabilities", [])),
                provenance={"discovered_by": "seed", "source": "cabinet"},
            )
        )
        added += 1
    return added


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_cabinet(ctx: Context, args: argparse.Namespace) -> int:
    print(ctx.machine.render_cabinet())
    if args.stock:
        print()
        print(f"{'SLOT':<6}{'ID':<20}{'KIND':<10}{'RUNTIME':<10}{'STATE':<8}SKILLS")
        for row in ctx.machine.stock():
            state = "on" if row["enabled"] else "off"
            print(
                f"{row['slot']:<6}{row['id']:<20}{row['kind']:<10}"
                f"{row['runtime']:<10}{state:<8}{','.join(row['skills'])}"
            )
    return 0


def cmd_pull(ctx: Context, args: argparse.Namespace) -> int:
    payload: Dict[str, Any] = {}
    if args.payload:
        try:
            payload = json.loads(args.payload)
        except ValueError as exc:
            print(f"bad --payload JSON: {exc}", file=sys.stderr)
            return 2
    try:
        ticket = ctx.machine.pull(args.slot, payload=payload, idem=args.idem, force=args.force)
    except VendError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"✓ dispensed {ticket.slot} → {ticket.entity_id}  ticket={ticket.id}")
    if ticket.command:
        print(f"  staged: {ticket.command}")
    print(f"  ticket: {ticket.path}")
    return 0


def cmd_tickets(ctx: Context, args: argparse.Namespace) -> int:
    pending = ctx.machine.pending()
    if not pending:
        print("no pending tickets")
        return 0
    for t in pending:
        print(f"{t.id}  {t.slot:<4} {t.entity_id:<20} {t.created}")
    return 0


def cmd_finish(ctx: Context, args: argparse.Namespace) -> int:
    try:
        if args.action == "claim":
            ticket = ctx.machine.claim(args.ticket, worker=args.worker)
        else:
            result = json.loads(args.result) if args.result else {}
            ticket = ctx.machine.complete(args.ticket, result=result, ok=(args.action == "complete"))
    except VendError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"✓ {ticket.id} → {ticket.status}")
    return 0


def cmd_skills(ctx: Context, args: argparse.Namespace) -> int:
    if args.action == "list":
        for s in ctx.skills.all():
            flag = "★" if s.certified else ("!" if s.flagged else " ")
            print(f"{flag} {s.id:<28}{s.level:<12}score={s.score:.3f} runs={s.runs}")
        return 0
    if args.action == "leaderboard":
        for i, s in enumerate(ctx.skills.leaderboard(args.n), 1):
            print(f"{i:>2}. {s.id:<28}{s.level:<12}score={s.score:.3f} runs={s.runs}")
        return 0
    if args.action == "summary":
        print(json.dumps(ctx.skills.summary(), indent=2))
        return 0
    if args.action == "show":
        skill = ctx.skills.get(args.skill)
        if skill is None:
            print(f"unknown skill '{args.skill}'", file=sys.stderr)
            return 1
        print(json.dumps(skill.to_dict(), indent=2))
        return 0
    if args.action == "record":
        skill = ctx.skills.record_run(args.skill, args.reward, ok=not args.fail)
        print(f"✓ {skill.id}: score={skill.score:.3f} level={skill.level} runs={skill.runs}")
        return 0
    if args.action == "certify":
        evidence = ctx.skills.certify(args.skill)
        print(json.dumps(evidence, indent=2))
        return 0 if evidence["passed"] else 1
    if args.action == "retire":
        skill = ctx.skills.retire(args.skill)
        print(f"✓ retired {skill.id}")
        return 0
    if args.action == "seed":
        print(f"✓ seeded {seed_skills(ctx)} skills")
        return 0
    print("unknown skills action", file=sys.stderr)
    return 2


def cmd_scout(ctx: Context, args: argparse.Namespace) -> int:
    engine = ScoutEngine(ctx.skills, ctx.ledger, registry=ctx.registry, forecast=ctx.engine)
    if args.action == "discover":
        findings = engine.discover(min_occurrences=args.min, apply=args.apply)
    elif args.action == "evaluate":
        findings = engine.evaluate()
    elif args.action == "audit":
        findings = engine.audit_capabilities()
    else:
        result = engine.run(apply=args.apply, min_occurrences=args.min)
        for bucket, items in result.items():
            print(f"\n## {bucket}")
            if not items:
                print("  (nothing)")
            for f in items:
                print(f"  [{f.kind}] {f.subject}  (conf {f.confidence:.2f}) — {f.detail}")
        return 0
    if not findings:
        print("no findings")
        return 0
    for f in findings:
        print(f"[{f.kind}] {f.subject}  (conf {f.confidence:.2f}) — {f.detail}")
    return 0


def _series_for(ctx: Context, metric: str) -> List[float]:
    if metric == "revenue":
        return ctx.engine.revenue_series()
    if metric == "reward":
        return ctx.engine.reward_series()
    if metric in ("dispense", "complete", "skill_run", "skill_certify"):
        return ctx.engine.count_series(metric)
    return []


def cmd_forecast(ctx: Context, args: argparse.Namespace) -> int:
    if args.series:
        try:
            series = [float(x) for x in args.series.split(",")]
        except ValueError:
            print("--series must be comma-separated numbers", file=sys.stderr)
            return 2
        metric = "inline"
    else:
        series = _series_for(ctx, args.metric)
        metric = args.metric
    if not series:
        print(f"no observations for '{metric}' yet — nothing to forecast")
        return 0
    result = ctx.engine.forecast(series, horizon=args.horizon, metric=metric)
    print(f"series: {len(series)} obs  last={series[-1]:.4f}")
    print(f"model : {result['model']}  {result.get('params', {})}")
    print(f"next {args.horizon}: {result['predictions']}")
    bt = result.get("backtest") or {}
    if bt.get("fold_size"):
        print(f"backtest: MAE={bt['mae']} RMSE={bt['rmse']} sMAPE={bt['smape']} R2={bt['r2']}")
    anomalies = ctx.engine.anomalies(series)
    if anomalies:
        print(f"anomalies: {json.dumps(anomalies)}")
    return 0


def cmd_supervise(ctx: Context, args: argparse.Namespace) -> int:
    sup = Supervisor(ctx.registry, ctx.ledger, state_path=ctx.root / "logs" / "supervisor_state.json")
    for _ in range(max(1, args.ticks)):
        report = sup.tick()
    print(json.dumps(report, indent=2))
    return 0


def cmd_doctor(ctx: Context, args: argparse.Namespace) -> int:
    problems = ctx.registry.problems()
    print(f"manifest : {ctx.registry.source or '(default)'}")
    print(f"occupants: {len(ctx.registry)}  capabilities: {len(ctx.registry.capabilities)}")
    print(f"ledger   : {ctx.ledger.path}  events={ctx.ledger.count()}")
    print(f"skills   : {len(ctx.skills.all())}")
    if problems:
        print("\n✗ registry problems:")
        for p in problems:
            print(f"  - {p}")
    else:
        print("\n✓ registry valid")
    if not ctx.skills.all():
        print("! skill store empty — run `swarmctl skills seed`")
    return 1 if problems else 0


def cmd_demo(ctx: Context, args: argparse.Namespace) -> int:
    print("== demo: seed → pull → run → improve → certify → forecast ==")
    seed_skills(ctx)

    ticket = ctx.machine.pull("augur", payload={"horizon": 3})
    print(f"1. pulled augur → ticket {ticket.id}")

    skill_id = "forecast"
    for i in range(60):
        ctx.skills.record_run(skill_id, reward=0.9 if i % 7 else 0.6, ok=True)
    skill = ctx.skills.get(skill_id)
    print(f"2. trained '{skill_id}' → level={skill.level} score={skill.score:.3f} certified={skill.certified}")

    ctx.machine.claim(ticket.id, worker="demo")
    ctx.machine.complete(ticket.id, result={"revenue_msat": 5000})
    print("3. completed ticket → outbox")

    engine = ScoutEngine(ctx.skills, ctx.ledger, registry=ctx.registry, forecast=ctx.engine)
    findings = engine.run()
    print(f"4. scout: {sum(len(v) for v in findings.values())} findings")

    series = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    forecast = ctx.engine.forecast(series, horizon=3, metric="inline")
    print(f"5. forecast next 3: {forecast['predictions']} via {forecast['model']}")
    print("== demo complete ==")
    return 0


def cmd_contract(ctx: Context, args: argparse.Namespace) -> int:
    if args.action == "emit":
        try:
            payload = json.loads(args.payload) if args.payload else {}
        except ValueError as exc:
            print(f"bad --payload JSON: {exc}", file=sys.stderr)
            return 2
        try:
            env = Envelope.build(
                emitter=args.emitter,
                kind=args.kind,
                payload=payload,
                confidence=args.confidence,
                lineage=args.lineage or "",
                tags=args.tags or [],
            )
        except (PayloadError, ValueError) as exc:
            print(f"✗ rejected: {exc}", file=sys.stderr)
            return 1
        ctx.bus.publish(env)
        print(json.dumps(env.to_sidecar(), indent=2))
        return 0
    # validate
    text = args.json
    if not text and args.file:
        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"could not read {args.file}: {exc}", file=sys.stderr)
            return 2
    try:
        env = parse_envelope(text or "{}")
    except PayloadError as exc:
        print(f"✗ rejected: {exc}", file=sys.stderr)
        return 1
    print(f"✓ P2 digest valid — envelope {env.id} ({env.kind}) from {env.emitter}")
    return 0


def cmd_bus(ctx: Context, args: argparse.Namespace) -> int:
    if args.action == "ingest":
        result = ctx.bus.ingest()
        print(json.dumps(
            {
                "mode": result["mode"],
                "accepted": result["accepted"],
                "rejected": result["rejected"],
                "errors": result["errors"][:5],
            },
            indent=2,
        ))
        return 0 if result["mode"] == "OPEN" else 1
    if args.action == "pheromones":
        hottest = ctx.bus.hottest(args.n)
        if not hottest:
            print("no pheromone trails on the bus")
        for row in hottest:
            print(f"{row['strength']:>8.4f}  {row['trail']}")
        return 0
    records = ctx.bus.high_confidence(args.min) if args.action == "high" else ctx.bus.tail(args.n)
    if not records:
        print("bus is empty")
        return 0
    for e in records:
        print(f"{e.created}  {e.kind:<12}{e.emitter:<16}conf={e.confidence:.2f}  {e.id}  [{e.lineage or 'no-lineage'}]")
    return 0


def cmd_lineage(ctx: Context, args: argparse.Namespace) -> int:
    purpose = PURPOSE_SELF_MODIFY if args.self_modify else PURPOSE_TELEMETRY
    decided = ctx.lineage.check(args.path, purpose, ledger=ctx.ledger)
    print(json.dumps(decided.to_dict(), indent=2))
    return 0 if decided.permitted else 1


def cmd_incentive(ctx: Context, args: argparse.Namespace) -> int:
    try:
        signals = json.loads(args.signals) if args.signals else None
    except ValueError as exc:
        print(f"bad --signals JSON: {exc}", file=sys.stderr)
        return 2
    finding = ctx.incentives.score(
        subject=args.subject or "adhoc",
        text=args.text,
        signals=signals,
        confidence=args.confidence,
        lineage_ok=not args.dead_lineage,
    )
    ctx.incentives.record(finding, ledger=ctx.ledger, store=ctx.skills)
    print(json.dumps(finding.to_dict(), indent=2))
    return 0


def cmd_ingest(ctx: Context, args: argparse.Namespace) -> int:
    result = ctx.ingestor.run_once()
    print(json.dumps(result, indent=2))
    return 0


def cmd_breaker(ctx: Context, args: argparse.Namespace) -> int:
    if args.action == "status":
        print(json.dumps(ctx.breaker.status(), indent=2))
        return 0
    ctx.breaker.preflight_pass(by="operator", bus=ctx.bus)
    print(json.dumps(ctx.breaker.status(), indent=2))
    print("✓ preflight passed — bus unlocked, mode=OPEN")
    return 0


def cmd_discovery(ctx: Context, args: argparse.Namespace) -> int:
    if args.action in ("register", "check") and not args.script:
        print(f"'{args.action}' requires a script path", file=sys.stderr)
        return 2
    if args.action == "sync":
        registered = ctx.scout_registry.sync_entities(ctx.registry)
        print(json.dumps(
            {"registered": len(registered), "scripts": [e["script"] for e in registered]}, indent=2
        ))
        return 0
    if args.action == "register":
        try:
            entry = ctx.scout_registry.record_clean_run(args.script)
        except OSError as exc:
            print(f"could not read {args.script}: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(entry, indent=2))
        return 0
    status = ctx.scout_registry.gate_execution(args.script)
    print(json.dumps(status, indent=2))
    return 0 if status["status"] == "REGISTERED" else 1


def cmd_graft(ctx: Context, args: argparse.Namespace) -> int:
    if args.action == "inspect":
        decision = ctx.graft.pre_graft(args.winner, args.loser)
        print(json.dumps(decision.to_dict(), indent=2))
        return 0
    out = ctx.graft.execute(args.winner, args.loser, lambda: "graft-noop")
    print(json.dumps(out["decision"], indent=2))
    return 0


def cmd_reconcile(ctx: Context, args: argparse.Namespace) -> int:
    """JANUS checklist against whorl.py and lineage.py."""
    lineage_report = reconcile_with_proofs(
        args.proofs,
        allowed=tuple(args.allowed_root) if args.allowed_root else tuple(ctx.lineage.allowed),
        denied=tuple(args.denied_root) if args.denied_root else tuple(ctx.lineage.denied),
    )
    whorl_report = reconcile_bus_path(
        ctx.bus.path, args.expected_bus_path if args.expected_bus_path else None
    )
    report = {"lineage": lineage_report, "whorl": whorl_report}
    print(json.dumps(report, indent=2))
    if lineage_report["status"] == "source_absent":
        print("! PROOFS_AND_GATES.md not present — active roots used as-is (source_absent)")
    return 1 if (lineage_report["status"] == "drift" or whorl_report["status"] == "drift") else 0


RELAY_DIRECTIVES = [
    ("Relay 1 — Standardize Payload Contracts",
     "Standardize emitter schema to match ScoutLoader.write_sidecar. Payload MUST include "
     "content_sha256 (P2 verification). Reject all ad-hoc JSON without a valid P2 digest."),
    ("Relay 2 — Routing via Whorl Bus",
     "Stop file-based sidecar polling. Direct all telemetry and pheromone emissions to "
     "WhorlBus. Monitor bus.jsonl for high-confidence ScoutAgent signals."),
    ("Relay 3 — Gated Self-Improvement",
     "Do NOT ingest or self-modify from ._archive/ or UNPACKED/. Only accept updates from "
     "src/skill/ or projects/agents/scouts/ (live lineage confirmed)."),
]


def cmd_relay(ctx: Context, args: argparse.Namespace) -> int:
    for title, body in RELAY_DIRECTIVES:
        print(f"\n## {title}\n> {body}")
    env = Envelope.build(
        emitter="orchestrator",
        kind="telemetry",
        payload={
            "event": "SWARM_RELAY_SYNC",
            "contract": "ScoutLoader.write_sidecar",
            "bus": "WhorlBus",
            "require_sha256": True,
        },
        confidence=1.0,
        lineage="projects/agents/scouts/bootstrap",
    )
    ctx.bus.publish(env)
    print(f"\n✓ emitted SWARM_RELAY_SYNC  id={env.id}  digest={env.content_sha256[:16]}…")
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swarmctl", description="Norexa swarm control plane")
    parser.add_argument("--root", default=".", help="project root (default: cwd)")
    parser.add_argument("--manifest", default=None, help="override cabinet manifest path")
    parser.add_argument("--registry-path", default=None, help="SCOUT_REGISTRY.json path (default ~/.config/freebuff)")
    parser.add_argument("--quarantine-dir", default="/tmp/quarantine")
    parser.add_argument("--bus-path", default=None, help="override bus.jsonl path (core_framework)")
    parser.add_argument("--allowed-root", action="append", default=None, help="override an allowed lineage root")
    parser.add_argument("--denied-root", action="append", default=None, help="override a denied lineage root")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("cabinet", help="render the cabinet")
    p.add_argument("--stock", action="store_true", help="also list every occupant")
    p.set_defaults(func=cmd_cabinet)

    p = sub.add_parser("stock", help="list every occupant")
    p.add_argument("--stock", action="store_true", default=True)
    p.set_defaults(func=cmd_cabinet)

    p = sub.add_parser("pull", help="dispense a run for a slot or id")
    p.add_argument("slot")
    p.add_argument("--payload", default=None, help="JSON payload")
    p.add_argument("--idem", default=None, help="idempotency key")
    p.add_argument("--force", action="store_true", help="override disabled/non-runnable")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("tickets", help="list pending tickets")
    p.set_defaults(func=cmd_tickets)

    p = sub.add_parser("claim", help="claim a ticket")
    p.add_argument("ticket")
    p.add_argument("--worker", default="operator")
    p.set_defaults(func=cmd_finish, action="claim")

    p = sub.add_parser("complete", help="complete a ticket")
    p.add_argument("ticket")
    p.add_argument("--result", default=None, help="JSON result")
    p.set_defaults(func=cmd_finish, action="complete")

    p = sub.add_parser("skills", help="skill store")
    p.add_argument("action", choices=["list", "show", "record", "certify", "retire", "leaderboard", "summary", "seed"])
    p.add_argument("skill", nargs="?", default=None)
    p.add_argument("reward", nargs="?", type=float, default=1.0)
    p.add_argument("--fail", action="store_true")
    p.add_argument("-n", type=int, default=10)
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser("scout", help="run scouts")
    p.add_argument("action", nargs="?", choices=["discover", "evaluate", "audit", "run"], default="run")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--min", type=int, default=3)
    p.set_defaults(func=cmd_scout)

    p = sub.add_parser("forecast", help="forecast a ledger metric")
    p.add_argument("metric", nargs="?", default="reward")
    p.add_argument("--horizon", type=int, default=3)
    p.add_argument("--series", default=None, help="inline comma-separated series")
    p.set_defaults(func=cmd_forecast)

    p = sub.add_parser("supervise", help="run supervisor ticks")
    p.add_argument("--ticks", type=int, default=1)
    p.set_defaults(func=cmd_supervise)

    p = sub.add_parser("doctor", help="validate the installation")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("demo", help="end-to-end smoke run")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("contract", help="Relay 1: emit or validate a payload envelope")
    p.add_argument("action", choices=["emit", "validate"])
    p.add_argument("--emitter", default="operator")
    p.add_argument("--kind", default="telemetry", choices=["telemetry", "pheromone", "intel", "skill_update"])
    p.add_argument("--payload", default=None)
    p.add_argument("--confidence", type=float, default=0.5)
    p.add_argument("--lineage", default="")
    p.add_argument("--tags", nargs="*", default=None)
    p.add_argument("--json", default=None, help="raw JSON string to validate")
    p.add_argument("--file", default=None, help="path to JSON to validate")
    p.set_defaults(func=cmd_contract)

    p = sub.add_parser("bus", help="Relay 2: inspect / ingest the WhorlBus")
    p.add_argument("action", nargs="?", choices=["tail", "high", "pheromones", "ingest"], default="tail")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--min", type=float, default=0.75)
    p.set_defaults(func=cmd_bus)

    p = sub.add_parser("lineage", help="Relay 3: check a path against the lineage gate")
    p.add_argument("path")
    p.add_argument("--self-modify", action="store_true", help="gate for self-modification (fail-closed)")
    p.set_defaults(func=cmd_lineage)

    p = sub.add_parser("incentive", help="score a find for novelty/rarity/marketability")
    p.add_argument("--text", required=True)
    p.add_argument("--signals", default=None, help="JSON demand signals")
    p.add_argument("--subject", default=None)
    p.add_argument("--confidence", type=float, default=0.8)
    p.add_argument("--dead-lineage", action="store_true")
    p.set_defaults(func=cmd_incentive)

    p = sub.add_parser("ingest", help="drain the bus through the gated ingestion loop")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("relay", help="print the relay directives and emit SWARM_RELAY_SYNC")
    p.set_defaults(func=cmd_relay)

    p = sub.add_parser("breaker", help="Janus Guard: hard-lock status / manual preflight release")
    p.add_argument("action", nargs="?", choices=["status", "preflight"], default="status")
    p.set_defaults(func=cmd_breaker)

    p = sub.add_parser("discovery", help="dynamic scout registry: register, sync or gate a script")
    p.add_argument("action", choices=["register", "check", "sync"])
    p.add_argument("script", nargs="?")
    p.set_defaults(func=cmd_discovery)

    p = sub.add_parser("graft", help="anti-lobotomy pre-graft inspection and merge")
    p.add_argument("action", choices=["inspect", "execute"])
    p.add_argument("--winner", required=True)
    p.add_argument("--loser", required=True)
    p.set_defaults(func=cmd_graft)

    p = sub.add_parser("reconcile", help="JANUS checklist against whorl.py + lineage.py")
    p.add_argument("--proofs", default="PROOFS_AND_GATES.md")
    p.add_argument("--expected-bus-path", default=None)
    p.set_defaults(func=cmd_reconcile)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        ctx = build_context(args)
    except RegistryError as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return 1
    return int(args.func(ctx, args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
