"""Minimal property-based testing framework (stdlib only).

Provides:
  * deterministic value strategies with shrinking
  * ``for_all`` runner that records minimal counter-examples
  * a tiny assertion/statistics API used by the QA layer

The framework is intentionally independent of the system under test:
it knows nothing about invoices. Domain generators live in
``qa.generators``.
"""
from __future__ import annotations

import decimal
import random
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, List, Sequence, Tuple


class PropertyFailure(AssertionError):
    """Raised when a property predicate returns False or raises."""


@dataclass
class CounterExample:
    label: str
    value: Any
    reason: str
    traceback_text: str | None = None
    original: Any = None
    shrink_steps: int = 0


@dataclass
class PropertyResult:
    label: str
    ok: bool
    examples_run: int
    counterexample: CounterExample | None = None
    # labels collected from predicates (e.g. coverage buckets)
    collected: dict = field(default_factory=dict)


class Strategy:
    """Base strategy. Sub-classes implement generate/shrink."""

    def generate(self, rnd: random.Random) -> Any:
        raise NotImplementedError

    def shrink(self, value: Any):
        """Yield progressively smaller values (empty by default)."""
        return iter(())


class Just(Strategy):
    def __init__(self, value: Any):
        self.value = value

    def generate(self, rnd: random.Random) -> Any:
        return self.value


class SampledFrom(Strategy):
    def __init__(self, choices: Sequence[Any]):
        if not choices:
            raise ValueError("SampledFrom requires at least one choice")
        self.choices = list(choices)

    def generate(self, rnd: random.Random) -> Any:
        return rnd.choice(self.choices)

    def shrink(self, value: Any):
        # earlier entries are assumed simpler
        try:
            idx = self.choices.index(value)
        except ValueError:
            return
        for j in range(idx):
            yield self.choices[j]


class Integers(Strategy):
    def __init__(self, lo: int, hi: int):
        self.lo, self.hi = lo, hi

    def generate(self, rnd: random.Random) -> int:
        return rnd.randint(self.lo, self.hi)

    def shrink(self, value: int):
        seen = {value}
        candidates = [0, 1, -1, 2, -2]
        if value:
            candidates.append(value // 2)
            candidates.append(int(value * 0.9))
            # exact boundary walk so predicates like v < 100 shrink to 100
            candidates.append(abs(value) - 1 if value > 0 else
                              -abs(value) + 1)
        for cand in candidates:
            if self.lo <= cand <= self.hi and cand not in seen:
                seen.add(cand)
                yield cand


class Booleans(Strategy):
    def generate(self, rnd: random.Random) -> bool:
        return rnd.random() < 0.5

    def shrink(self, value: bool):
        if value:
            yield False


class Lists(Strategy):
    def __init__(self, element: Strategy, min_size: int = 0, max_size: int = 12):
        self.element, self.min_size, self.max_size = element, min_size, max_size

    def generate(self, rnd: random.Random) -> list:
        size = rnd.randint(self.min_size, self.max_size)
        return [self.element.generate(rnd) for _ in range(size)]

    def shrink(self, value: list):
        # remove elements first (most diagnostic), then shrink elements
        if len(value) > self.min_size:
            # try dropping each index, then halves
            for i in range(len(value)):
                smaller = value[:i] + value[i + 1:]
                if len(smaller) >= self.min_size:
                    yield smaller
            mid = len(value) // 2
            if mid >= self.min_size and mid < len(value):
                yield value[:mid]
                yield value[mid:]
        for i, item in enumerate(value):
            for shrunk in self.element.shrink(item):
                yield value[:i] + [shrunk] + value[i + 1:]


class FixedDict(Strategy):
    """Dict with a fixed key set and per-key strategies."""

    def __init__(self, fields: dict):
        self.fields = fields

    def generate(self, rnd: random.Random) -> dict:
        return {k: s.generate(rnd) for k, s in self.fields.items()}

    def shrink(self, value: dict):
        for k, strategy in self.fields.items():
            if k not in value:
                continue
            for shrunk in strategy.shrink(value[k]):
                candidate = dict(value)
                candidate[k] = shrunk
                yield candidate


class OneOf(Strategy):
    def __init__(self, branches: Sequence[Strategy]):
        self.branches = list(branches)

    def generate(self, rnd: random.Random) -> Any:
        return rnd.choice(self.branches).generate(rnd)

    def shrink(self, value: Any):
        for branch in self.branches:
            for shrunk in branch.shrink(value):
                yield shrunk


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def _failure_reason(exc: BaseException) -> Tuple[str, str | None]:
    if isinstance(exc, AssertionError):
        text = str(exc) or "assertion failed"
        return text, None
    return f"{type(exc).__name__}: {exc}", "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )


def for_all(
    label: str,
    strategy: Strategy,
    predicate: Callable[[Any], None],
    *,
    examples: int = 100,
    seed: int = 20261117,
    shrink_budget: int = 400,
    collector: Callable[[Any], str] | None = None,
) -> PropertyResult:
    """Generate ``examples`` values and assert ``predicate(value)``.

    On failure the value is greedily shrunk against the strategy's
    ``shrink`` lattice until no smaller failing value is found or the
    budget is exhausted.
    """
    rnd = random.Random(seed)
    collected: dict = {}
    counter: CounterExample | None = None
    run = 0
    for run in range(1, examples + 1):
        value = strategy.generate(rnd)
        try:
            predicate(value)
        except BaseException as exc:  # noqa: BLE001 - property boundary
            reason, tb = _failure_reason(exc)
            counter = CounterExample(
                label=label, value=value, reason=reason,
                traceback_text=tb, original=value, shrink_steps=0,
            )
            break
        if collector is not None:
            bucket = collector(value)
            collected[bucket] = collected.get(bucket, 0) + 1
    if counter is None:
        return PropertyResult(label=label, ok=True, examples_run=run,
                              collected=collected)

    # greedy counter-example reduction
    current = counter.value
    steps = 0
    improved = True
    while improved and steps < shrink_budget:
        improved = False
        for candidate in strategy.shrink(current):
            steps += 1
            try:
                predicate(candidate)
            except BaseException as exc:  # still failing -> smaller case
                reason, tb = _failure_reason(exc)
                current = candidate
                counter.value = candidate
                counter.reason = reason
                counter.traceback_text = tb
                counter.shrink_steps = steps
                improved = True
                break
            if steps >= shrink_budget:
                break
    return PropertyResult(label=label, ok=False, examples_run=run,
                          counterexample=counter, collected=collected)


def check(condition: bool, message: str, **locals_) -> None:
    """Assertion helper that attaches diagnostic local values."""
    if not condition:
        detail = ", ".join(f"{k}={v!r}" for k, v in locals_.items())
        raise PropertyFailure(f"{message} [{detail}]" if detail else message)


def is_finite_decimal(value: decimal.Decimal) -> bool:
    return value.is_finite()
