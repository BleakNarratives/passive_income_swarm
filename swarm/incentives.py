"""
swarm.incentives — paying for the weird, the rare and the sellable
=================================================================

A scout that only reports what every other scout already knows is worthless.
The swarm needs to reward the opposite: findings that are **novel**, **rare**
and **marketable**. This module turns a find into an incentive in ``[0, 1]``
with inspectable components, so the reward is a number you can argue with
rather than a vibe.

    total = w_n·novelty + w_r·rarity + w_m·marketability + w_c·confidence
            × (1 + outside_box_bonus)   when novelty clears the threshold
            × 0                        when lineage is not live

* **novelty** — 1 minus the max token-set Jaccard against everything the swarm
  already knows (skills, capabilities, registry occupants). High novelty means
  genuinely new territory.
* **rarity** — mean inverse document frequency of the finding's tokens against
  a fitted corpus; rare combinations score higher than common chatter.
* **marketability** — weighted roll-up of demand signals (revenue, niche,
  demand, uniqueness, exclusivity, freshness). Unknown signals are ignored.

The **outside-the-box bonus** is the point: a novel find is rewarded
super-linearly, so the swarm is explicitly incentivised to chase rare, niche,
sellable intel instead of farming the same safe obvious thing.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from .ledger import Ledger
from .skills import SkillStore

__all__ = ["Incentive", "ScoutIncentives"]

_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-]{1,}")

#: Recognised demand signals and their weights.
MARKET_WEIGHTS = {
    "revenue": 0.30,
    "niche": 0.20,
    "demand": 0.20,
    "uniqueness": 0.15,
    "exclusivity": 0.10,
    "freshness": 0.05,
}


def tokenize(text: str) -> Set[str]:
    return set(_TOKEN.findall(str(text).lower()))


@dataclass
class Incentive:
    subject: str
    novelty: float
    rarity: float
    marketability: float
    confidence: float
    outside_box: bool
    lineage_ok: bool
    total: float
    components: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ScoutIncentives:
    """Scores finds and records the reward where the swarm can act on it."""

    def __init__(
        self,
        store: Optional[SkillStore] = None,
        registry: Optional[Any] = None,
        weights: Optional[Dict[str, float]] = None,
        outside_box_threshold: float = 0.60,
        outside_box_bonus: float = 0.35,
    ):
        self.store = store
        self.registry = registry
        self.weights = weights or {"novelty": 0.35, "rarity": 0.25, "marketability": 0.30, "confidence": 0.10}
        self.outside_box_threshold = outside_box_threshold
        self.outside_box_bonus = outside_box_bonus
        self._df: Dict[str, int] = {}
        self._docs: int = 0

    # -- knowledge base ----------------------------------------------------
    def _known_docs(self) -> List[Set[str]]:
        docs: List[Set[str]] = []
        if self.store:
            for skill in self.store.all():
                blob = " ".join(
                    [skill.id, skill.name, skill.description]
                    + list(skill.capabilities)
                    + list(skill.steps)
                )
                docs.append(tokenize(blob))
        if self.registry:
            for entity in self.registry:
                docs.append(tokenize(f"{entity.id} {entity.display} {entity.role} {' '.join(entity.capabilities)}"))
            docs.append(tokenize(" ".join(self.registry.capabilities.keys())))
        return [d for d in docs if d]

    @staticmethod
    def _jaccard(a: Set[str], b: Set[str]) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    # -- components --------------------------------------------------------
    def novelty(self, text: str) -> float:
        tokens = tokenize(text)
        if not tokens:
            return 0.0
        known = self._known_docs()
        if not known:
            return 1.0
        return round(1.0 - max(self._jaccard(tokens, doc) for doc in known), 4)

    def fit(self, corpus: Iterable[str]) -> "ScoutIncentives":
        """Fit inverse-document-frequency stats for the rarity component."""
        self._df = {}
        self._docs = 0
        for doc in corpus:
            self._docs += 1
            for tok in tokenize(doc):
                self._df[tok] = self._df.get(tok, 0) + 1
        return self

    def rarity(self, text: str) -> float:
        tokens = tokenize(text)
        if not tokens:
            return 0.0
        if self._docs == 0:
            return 0.5  # neutral until fitted
        ceiling = math.log((self._docs + 1) / 1.0) or 1.0
        total = 0.0
        for tok in tokens:
            idf = math.log((self._docs + 1) / (self._df.get(tok, 0) + 1))
            total += idf
        return round(min(1.0, (total / len(tokens)) / ceiling), 4)

    def marketability(self, signals: Optional[Dict[str, float]]) -> float:
        if not signals:
            return 0.0
        num, den = 0.0, 0.0
        for key, weight in MARKET_WEIGHTS.items():
            if key in signals:
                num += weight * min(1.0, max(0.0, float(signals[key])))
                den += weight
        return round(num / den, 4) if den else 0.0

    # -- scoring -----------------------------------------------------------
    def score(
        self,
        subject: str,
        text: str,
        signals: Optional[Dict[str, float]] = None,
        confidence: float = 0.8,
        lineage_ok: bool = True,
    ) -> Incentive:
        n = self.novelty(text)
        r = self.rarity(text)
        m = self.marketability(signals)
        c = min(1.0, max(0.0, float(confidence)))
        w = self.weights
        total = w["novelty"] * n + w["rarity"] * r + w["marketability"] * m + w["confidence"] * c
        outside_box = n >= self.outside_box_threshold
        if outside_box:
            total *= 1.0 + self.outside_box_bonus
        if not lineage_ok:
            total = 0.0  # no reward for dead lineage
        total = round(min(1.0, max(0.0, total)), 4)
        return Incentive(
            subject=subject,
            novelty=n,
            rarity=r,
            marketability=m,
            confidence=round(c, 4),
            outside_box=outside_box and lineage_ok,
            lineage_ok=lineage_ok,
            total=total,
            components={
                "weights": dict(w),
                "outside_box_bonus": self.outside_box_bonus if outside_box else 0.0,
                "market_signals": dict(signals or {}),
            },
        )

    def record(
        self,
        incentive: Incentive,
        ledger: Optional[Ledger] = None,
        store: Optional[SkillStore] = None,
        actor: str = "incentives",
    ) -> Incentive:
        """Book the reward: ledger event always, skill score when there is one."""
        if ledger is not None:
            ledger.append("incentive", incentive.to_dict(), actor=actor)
        target = store or self.store
        if target is not None and target.get(incentive.subject) is not None:
            target.record_run(
                incentive.subject,
                reward=incentive.total,
                ok=incentive.total >= 0.5,
                context={"actor": actor, "components": incentive.components},
            )
        return incentive
