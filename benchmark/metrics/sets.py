"""Set-comparison primitives, with the invariants asserted rather than assumed."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Score:
    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def engine_total(self) -> int:
        return self.true_positives + self.false_positives

    @property
    def reference_total(self) -> int:
        return self.true_positives + self.false_negatives

    @property
    def precision(self) -> float:
        return self.true_positives / self.engine_total if self.engine_total else 0.0

    @property
    def recall(self) -> float:
        return self.true_positives / self.reference_total if self.reference_total else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def wilson(self, value: float, total: int, z: float = 1.96) -> tuple[float, float]:
        """Wilson 95% interval.

        A 400-line corpus gives small denominators, so a bare point estimate would
        imply a precision the sample size does not support.
        """
        if total == 0:
            return (0.0, 1.0)
        centre = (value + z * z / (2 * total)) / (1 + z * z / total)
        margin = (z / (1 + z * z / total)) * math.sqrt(
            value * (1 - value) / total + z * z / (4 * total * total)
        )
        return (max(0.0, centre - margin), min(1.0, centre + margin))

    @property
    def precision_ci(self) -> tuple[float, float]:
        return self.wilson(self.precision, self.engine_total)

    @property
    def recall_ci(self) -> tuple[float, float]:
        return self.wilson(self.recall, self.reference_total)

    def line(self, label: str) -> str:
        pl, ph = self.precision_ci
        rl, rh = self.recall_ci
        return (
            f"{label:<26} P {self.precision:5.1%} [{pl:.0%}-{ph:.0%}]  "
            f"R {self.recall:5.1%} [{rl:.0%}-{rh:.0%}]  F1 {self.f1:5.1%}  "
            f"(tp {self.true_positives}, fp {self.false_positives}, fn {self.false_negatives})"
        )


def score(engine: set, reference: set) -> Score:
    true_positives = engine & reference
    result = Score(
        true_positives=len(true_positives),
        false_positives=len(engine - reference),
        false_negatives=len(reference - engine),
    )
    assert result.engine_total == len(engine), "TP + FP must equal the engine's set"
    assert result.reference_total == len(reference), "TP + FN must equal the reference set"
    return result
