"""Aggregation.

Two decisions here are not cosmetic. Repeats of the same task are not
independent samples, so the confidence interval resamples *tasks* and carries
their repeats along; treating 60 tasks x 3 runs as 180 draws would report an
interval roughly 1.7x too narrow and manufacture significance that is not there.
And runs that failed for reasons outside the model -- provider outage, broken
fixture, harness bug -- are reported as availability rather than folded into the
capability score, so an API incident does not read as a weak model.
"""

from __future__ import annotations

import random
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from deskmind_bench.failures import NOT_MODEL_FAULT, FailureClass


@dataclass
class Aggregate:
    system: str
    n_runs: int = 0
    n_tasks: int = 0
    n_scored: int = 0
    n_excluded: int = 0
    strict_pct: float = 0.0
    partial_pct: float = 0.0
    strict_ci95: tuple[float, float] = (0.0, 0.0)
    median_s: float = 0.0
    p95_s: float = 0.0
    dialogue_per_run: float = 0.0
    stale_refusals: int = 0
    total_cost_usd: float = 0.0
    cost_per_success: float | None = None
    availability_pct: float = 100.0
    failures: dict[str, int] = field(default_factory=dict)
    per_task_strict: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> dict:
        d = dict(self.__dict__)
        d["strict_ci95"] = [round(self.strict_ci95[0], 2), round(self.strict_ci95[1], 2)]
        for k in ("strict_pct", "partial_pct", "median_s", "p95_s",
                  "dialogue_per_run", "availability_pct", "total_cost_usd"):
            d[k] = round(d[k], 3)
        if self.cost_per_success is not None:
            d["cost_per_success"] = round(self.cost_per_success, 6)
        d["per_task_strict"] = {k: round(v, 3) for k, v in sorted(self.per_task_strict.items())}
        return d


def _percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    idx = q * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def cluster_bootstrap_ci(by_task: dict[str, list[bool]], *, iters: int = 2000,
                         seed: int = 20260906) -> tuple[float, float]:
    """95% CI for the strict rate, resampling tasks (with their repeats)."""
    tasks = list(by_task)
    if len(tasks) < 2:
        return (0.0, 100.0)
    rng = random.Random(seed)
    means = []
    for _ in range(iters):
        picked = [by_task[rng.choice(tasks)] for _ in tasks]
        flat = [v for group in picked for v in group]
        means.append(100.0 * sum(flat) / len(flat) if flat else 0.0)
    means.sort()
    return (means[int(0.025 * len(means))], means[int(0.975 * len(means)) - 1])


def aggregate(runs: list, system: str) -> Aggregate:
    """``runs`` is a list of RunResult (or their to_json dicts)."""
    rows = [r if isinstance(r, dict) else r.to_json() for r in runs]
    agg = Aggregate(system=system, n_runs=len(rows))
    if not rows:
        return agg

    scored, excluded = [], []
    for r in rows:
        cls = FailureClass(r["failure"]["class"])
        (excluded if cls in NOT_MODEL_FAULT else scored).append(r)

    agg.n_excluded = len(excluded)
    agg.n_scored = len(scored)
    agg.availability_pct = 100.0 * len(scored) / len(rows)
    agg.failures = dict(Counter(r["failure"]["class"] for r in rows
                                if r["failure"]["class"] != "none").most_common())

    by_task: dict[str, list[bool]] = defaultdict(list)
    for r in scored:
        by_task[r["task_id"]].append(bool(r["grade"]["strict"]))
    agg.n_tasks = len(by_task)
    agg.per_task_strict = {t: 100.0 * sum(v) / len(v) for t, v in by_task.items()}

    if scored:
        agg.strict_pct = 100.0 * sum(r["grade"]["strict"] for r in scored) / len(scored)
        agg.partial_pct = 100.0 * statistics.fmean(r["grade"]["partial"] for r in scored)
        agg.strict_ci95 = cluster_bootstrap_ci(by_task)
        times = [r["metrics"]["agent_s"] for r in scored]
        agg.median_s = statistics.median(times)
        agg.p95_s = _percentile(times, 0.95)
        agg.dialogue_per_run = statistics.fmean(r["metrics"]["dialogue_turns"] for r in scored)
        agg.stale_refusals = sum(r["metrics"]["stale_refusals"] for r in scored)
        agg.total_cost_usd = sum(r["metrics"]["cost_usd"] for r in rows)
        n_success = sum(r["grade"]["strict"] for r in scored)
        # Cost per success divides *all* attempts by successes, including the
        # failed ones -- that is what a task actually costs to get done.
        agg.cost_per_success = (agg.total_cost_usd / n_success) if n_success else None
    return agg


def markdown_table(aggs: list[Aggregate]) -> str:
    head = ("| system | runs | scored | strict% | 95% CI | partial% | median s | p95 s | "
            "dialogue/run | avail% | cost/success |")
    sep = "|" + "---|" * 11
    lines = [head, sep]
    for a in aggs:
        cps = "-" if a.cost_per_success is None else f"{a.cost_per_success:.4f}"
        lines.append(
            f"| {a.system} | {a.n_runs} | {a.n_scored} | {a.strict_pct:.1f} | "
            f"[{a.strict_ci95[0]:.1f}, {a.strict_ci95[1]:.1f}] | {a.partial_pct:.1f} | "
            f"{a.median_s:.1f} | {a.p95_s:.1f} | {a.dialogue_per_run:.2f} | "
            f"{a.availability_pct:.1f} | {cps} |")
    return "\n".join(lines)
