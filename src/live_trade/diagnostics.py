from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Mapping

from .scanner import ScanRun


@dataclass(frozen=True)
class ScannerRejectionReport:
    discovered_count: int
    evaluated_count: int
    eligible_count: int
    activated_count: int
    rejected_instrument_count: int
    rejection_event_count: int
    rejection_counts: Mapping[str, int]
    filter_pass_percentages: Mapping[str, float]
    examples_by_reason: Mapping[str, tuple[str, ...]]

    def to_dict(self) -> dict[str, object]:
        return {
            "discovered_count": self.discovered_count,
            "evaluated_count": self.evaluated_count,
            "eligible_count": self.eligible_count,
            "activated_count": self.activated_count,
            "rejected_instrument_count": self.rejected_instrument_count,
            "rejection_event_count": self.rejection_event_count,
            "rejection_counts": dict(self.rejection_counts),
            "filter_pass_percentages": dict(self.filter_pass_percentages),
            "examples_by_reason": {key: list(value) for key, value in self.examples_by_reason.items()},
        }


def build_rejection_report(scan: ScanRun) -> ScannerRejectionReport:
    evaluated = len(scan.results)
    reason_counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for result in scan.results:
        for reason in result.reasons:
            reason_counts[reason] += 1
            if len(examples[reason]) < 20:
                examples[reason].append(result.symbol)
    rejected = sum(not result.eligible for result in scan.results)
    percentages = {
        reason: round(((evaluated - count) / evaluated) * 100, 2) if evaluated else 0.0
        for reason, count in reason_counts.items()
    }
    return ScannerRejectionReport(
        discovered_count=scan.discovered_count,
        evaluated_count=evaluated,
        eligible_count=sum(result.eligible for result in scan.results),
        activated_count=sum(result.activated for result in scan.results),
        rejected_instrument_count=rejected,
        rejection_event_count=sum(reason_counts.values()),
        rejection_counts=dict(sorted(reason_counts.items())),
        filter_pass_percentages=dict(sorted(percentages.items())),
        examples_by_reason={key: tuple(value) for key, value in sorted(examples.items())},
    )
