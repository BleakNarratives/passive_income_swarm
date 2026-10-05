"""
Norexa Swarm Control Plane
==========================

The orchestration core for the passive_income_swarm ecosystem: a single,
crash-safe registry of every daemon, scout, agent, sub-agent and persona,
addressed through an analog "cabinet" of slots, driven by a shared event
ledger, a self-improving skill engine and a predictive analytics core.

Everything here is standard-library only so it runs on the bare Python 3.10
interpreter the swarm actually deploys on (no fastapi / pydantic / yaml).

Public surface:
    Ledger              append-only event substrate
    Entity              one thing in the ecosystem
    Registry            validated cabinet manifest + slot addressing
    VendingMachine      the control plane: pull a slot, dispense a run
    SkillStore          self-improving skills, mastery + certification
    ScoutEngine         discovers / evaluates / promotes skills from the ledger
    PredictiveEngine    forecasting, backtesting and model registry
    Supervisor          daemon health, restart backoff, circuit breakers
"""

__version__ = "0.1.0"
__all__ = [
    "Ledger",
    "Event",
    "Entity",
    "Registry",
    "VendingMachine",
    "SkillStore",
    "Skill",
    "ScoutEngine",
    "PredictiveEngine",
    "Supervisor",
]
