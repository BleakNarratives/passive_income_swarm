"""
swarm.skills — scout-driven skills that improve themselves
==========================================================

This replaces two crutches at once.

* **SKILL.md** — prose that describes a skill but can never tell you whether it
  *worked*. Here a skill is a record with executable steps, declared I/O and a
  living score. Skills earn their place; they are not documented into existence.
* **MCP hoarding** — every tool connector re-declared per agent. Here a skill
  declares the *capability ports* it needs (``llm.generate``, ``net.nostr``,
  ``pay.lnurl``) and the registry resolves them once, for everyone.

The loop
--------
Every run reports a reward in [0, 1]. The score is an exponentially weighted
moving average, so recent form dominates without throwing away history. From
score + volume + stability the skill climbs a mastery ladder:

    NOVICE -> APPRENTICE -> ADEPT -> EXPERT -> MASTER -> CERTIFIED

``CERTIFIED`` is not granted by the ladder. It is a separate gate that must be
passed explicitly against measurable thresholds (volume, score, stability), and
the evidence is recorded. That is the difference between "we think it's good"
and "it passed the checks, here is the receipt."
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .ledger import Ledger

__all__ = ["Skill", "SkillStore", "MasteryGate", "LEVELS", "SkillError"]

#: Mastery ladder, lowest to highest.
LEVELS = ["NOVICE", "APPRENTICE", "ADEPT", "EXPERT", "MASTER", "CERTIFIED"]

#: (level, min_runs, min_score, max_stdev) — first satisfied rung wins.
LADDER = [
    ("MASTER", 40, 0.88, 0.18),
    ("EXPERT", 20, 0.80, None),
    ("ADEPT", 8, 0.70, None),
    ("APPRENTICE", 3, 0.55, None),
    ("NOVICE", 0, 0.0, None),
]


class SkillError(ValueError):
    """Raised on malformed skill records or illegal transitions."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Skill:
    id: str
    name: str
    description: str = ""
    version: int = 1
    kind: str = "native"  # native | learned | emergent
    steps: List[str] = field(default_factory=list)
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    provenance: Dict[str, Any] = field(default_factory=dict)
    runs: int = 0
    successes: int = 0
    failures: int = 0
    score: float = 0.0
    score_history: List[float] = field(default_factory=list)
    level: str = "NOVICE"
    certified: bool = False
    certified_at: Optional[str] = None
    cert_evidence: Dict[str, Any] = field(default_factory=dict)
    flagged: bool = False
    retired: bool = False

    # -- derived -----------------------------------------------------------
    @property
    def success_rate(self) -> float:
        return self.successes / self.runs if self.runs else 0.0

    def stdev(self, window: int = 20) -> float:
        hist = self.score_history[-window:]
        if len(hist) < 2:
            return 0.0
        mean = sum(hist) / len(hist)
        var = sum((x - mean) ** 2 for x in hist) / (len(hist) - 1)
        return math.sqrt(var)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Skill":
        if not raw.get("id"):
            raise SkillError("skill record missing 'id'")
        known = {k: raw[k] for k in raw if k in cls.__dataclass_fields__}
        known.setdefault("name", str(known.get("id")))
        return cls(**known)


class MasteryGate:
    """The certification gate. Explicit thresholds; no vibes."""

    def __init__(self, min_runs: int = 20, min_score: float = 0.80, max_stdev: float = 0.20):
        self.min_runs = min_runs
        self.min_score = min_score
        self.max_stdev = max_stdev

    def evaluate(self, skill: Skill) -> Dict[str, Any]:
        checks = [
            {
                "name": "volume",
                "passed": skill.runs >= self.min_runs,
                "detail": f"runs={skill.runs} >= {self.min_runs}",
            },
            {
                "name": "score",
                "passed": skill.score >= self.min_score,
                "detail": f"score={skill.score:.3f} >= {self.min_score}",
            },
            {
                "name": "stability",
                "passed": skill.stdev() <= self.max_stdev,
                "detail": f"stdev={skill.stdev():.3f} <= {self.max_stdev}",
            },
            {
                "name": "not-retired",
                "passed": not skill.retired,
                "detail": f"retired={skill.retired}",
            },
        ]
        return {"passed": all(c["passed"] for c in checks), "checks": checks}


class SkillStore:
    """Persistent, self-improving skill registry backed by the ledger."""

    def __init__(
        self,
        path: str | Path,
        ledger: Optional[Ledger] = None,
        alpha: float = 0.3,
        gate: Optional[MasteryGate] = None,
        retire_below: float = 0.35,
        retire_after: int = 12,
        auto_certify: bool = True,
    ):
        self.path = Path(path)
        self.ledger = ledger
        self.alpha = alpha
        self.gate = gate or MasteryGate()
        self.retire_below = retire_below
        self.retire_after = retire_after
        self.auto_certify = auto_certify
        self._skills: Dict[str, Skill] = {}
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in raw.get("skills", []):
            try:
                skill = Skill.from_dict(item)
            except SkillError:
                continue
            self._skills[skill.id] = skill

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 1,
            "updated": _now(),
            "skills": [s.to_dict() for s in self._skills.values()],
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    # -- registry ops ------------------------------------------------------
    def register(self, skill: Skill, replace: bool = False) -> Skill:
        if skill.id in self._skills and not replace:
            raise SkillError(f"skill '{skill.id}' already registered")
        if skill.level not in LEVELS:
            raise SkillError(f"skill '{skill.id}' has unknown level '{skill.level}'")
        self._skills[skill.id] = skill
        self.save()
        if self.ledger:
            self.ledger.append(
                "skill_register",
                {"skill": skill.id, "kind": skill.kind, "capabilities": skill.capabilities},
                actor="skills",
            )
        return skill

    def get(self, skill_id: str) -> Optional[Skill]:
        return self._skills.get(skill_id)

    def all(self) -> List[Skill]:
        return list(self._skills.values())

    def leaderboard(self, n: int = 10) -> List[Skill]:
        return sorted(self._skills.values(), key=lambda s: (s.score, s.runs), reverse=True)[:n]

    def by_capability(self, capability: str) -> List[Skill]:
        return [s for s in self._skills.values() if capability in s.capabilities]

    # -- the self-improvement loop ----------------------------------------
    def record_run(
        self,
        skill_id: str,
        reward: float,
        ok: bool = True,
        context: Optional[Dict[str, Any]] = None,
    ) -> Skill:
        """Fold one outcome into the skill's score and re-evaluate its level."""
        skill = self._skills.get(skill_id)
        if skill is None:
            raise SkillError(f"unknown skill '{skill_id}'")
        if not 0.0 <= reward <= 1.0:
            raise SkillError(f"reward must be in [0,1], got {reward}")

        # An explicit failure can never be rewarded as a success.
        if not ok:
            reward = min(reward, 0.25)

        skill.runs += 1
        if ok:
            skill.successes += 1
        else:
            skill.failures += 1
        skill.score_history.append(reward)
        # EWMA seeded by the first observation.
        if skill.runs == 1:
            skill.score = reward
        else:
            skill.score = (1 - self.alpha) * skill.score + self.alpha * reward

        self._recompute(skill)
        self.save()
        if self.ledger:
            self.ledger.append(
                "skill_run",
                {
                    "skill": skill.id,
                    "ok": ok,
                    "reward": reward,
                    "score": round(skill.score, 4),
                    "level": skill.level,
                    "context": context or {},
                },
                actor=(context or {}).get("actor", "skills"),
            )
        return skill

    def _recompute(self, skill: Skill) -> None:
        """Re-derive level from score/volume/stability, flag or certify as due."""
        prev = skill.level
        new_level = "NOVICE"
        for level, min_runs, min_score, max_stdev in LADDER:
            if skill.runs >= min_runs and skill.score >= min_score:
                if max_stdev is not None and skill.stdev() > max_stdev:
                    continue
                new_level = level
                break

        # A previously certified skill never silently loses the badge; it just
        # shows up as EXPERT again if form slips.
        if skill.certified:
            skill.level = "CERTIFIED"
        else:
            skill.level = new_level

        if (
            not skill.retired
            and skill.runs >= self.retire_after
            and skill.score < self.retire_below
        ):
            skill.flagged = True
        else:
            skill.flagged = False

        if (
            self.auto_certify
            and not skill.certified
            and skill.level == "MASTER"
        ):
            self.certify(skill.id, record=False)

        if self.ledger and prev != skill.level:
            self.ledger.append(
                "skill_level",
                {"skill": skill.id, "from": prev, "to": skill.level},
                actor="skills",
            )

    # -- certification -----------------------------------------------------
    def certify(self, skill_id: str, record: bool = True) -> Dict[str, Any]:
        """Run the gate. Returns the evidence either way; only passing certifies."""
        skill = self._skills.get(skill_id)
        if skill is None:
            raise SkillError(f"unknown skill '{skill_id}'")
        evidence = self.gate.evaluate(skill)
        evidence["at"] = _now()
        skill.cert_evidence = evidence
        if evidence["passed"]:
            skill.certified = True
            skill.certified_at = evidence["at"]
            skill.level = "CERTIFIED"
            skill.flagged = False
        if record:
            self.save()
            if self.ledger:
                self.ledger.append(
                    "skill_certify",
                    {"skill": skill.id, "passed": evidence["passed"], "checks": evidence["checks"]},
                    actor="skills",
                )
        return evidence

    def retire(self, skill_id: str) -> Skill:
        skill = self._skills.get(skill_id)
        if skill is None:
            raise SkillError(f"unknown skill '{skill_id}'")
        skill.retired = True
        self.save()
        if self.ledger:
            self.ledger.append("skill_retire", {"skill": skill.id}, actor="skills")
        return skill

    # -- scout intake ------------------------------------------------------
    def propose(
        self,
        skill_id: str,
        name: str,
        capabilities: List[str],
        provenance: Dict[str, Any],
        description: str = "",
        steps: Optional[List[str]] = None,
    ) -> Skill:
        """Register an emergent skill discovered by a scout (never certified)."""
        if skill_id in self._skills:
            existing = self._skills[skill_id]
            existing.version += 1
            existing.provenance = provenance
            self.save()
            return existing
        skill = Skill(
            id=skill_id,
            name=name,
            description=description,
            kind="emergent",
            steps=steps or [],
            capabilities=capabilities,
            provenance=provenance,
        )
        return self.register(skill)

    def summary(self) -> Dict[str, Any]:
        by_level: Dict[str, int] = {lvl: 0 for lvl in LEVELS}
        for skill in self._skills.values():
            by_level[skill.level] = by_level.get(skill.level, 0) + 1
        return {
            "total": len(self._skills),
            "certified": sum(1 for s in self._skills.values() if s.certified),
            "flagged": sum(1 for s in self._skills.values() if s.flagged),
            "by_level": by_level,
        }
