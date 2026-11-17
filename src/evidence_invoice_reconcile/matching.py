"""匹配候选、冲销分配与失败代价比较。

两种分配策略，共享同一套候选与惩罚价格，因此失败代价可以直接比较：

- ``greedy``：按输入顺序逐张红字处理，显式关联优先、候选评分降序，
  吃满一张蓝票再看下一张。局部最优，顺序敏感，可能把蓝票额度提前占满，
  导致后续红字无法分配（失败代价更高）。
- ``global``：最小费用流（source→红字→蓝字→sink），整数“分”为容量，
  Bellman-Ford 逐次增广，全局最优且与输入顺序无关。

惩罚价格（每分）：红字未分配 500、蓝字未被冲销 300，
均远高于边上的匹配成本（0..95），所以“先最大化覆盖、再优化匹配质量”。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .models import Allocation, AllocationPlan, Invoice, MatchCandidate
from .money import from_cents

UNALLOCATED_RED_PENALTY = 500      # 每分
UNMATCHED_BLUE_PENALTY = 300       # 每分
EXPLICIT_SCORE = 100
INF = 10**18


# ---------------------------------------------------------------- 候选生成

def _tax_mix(invoice: Invoice) -> Tuple[str, ...]:
    return tuple(sorted(line.rate_code for line in invoice.lines))


def generate_candidates(reds: List[Invoice], blues: List[Invoice]) -> List[MatchCandidate]:
    """为每张红字生成候选蓝字；显式关联永远入选且评分 100。"""
    blue_by_number = {b.number: b for b in blues}
    candidates: List[MatchCandidate] = []
    for red in reds:
        red_amt = -red.gross_cents  # 红字为负，取绝对值
        seen_numbers = set()
        # 1) 显式声明的关联
        for declared in red.red_links:
            seen_numbers.add(declared)
            blue = blue_by_number.get(declared)
            if blue is None:
                # 目标不存在仍生成候选，交由 pipeline 判 HARD（证据缺失）
                candidates.append(MatchCandidate(
                    red_number=red.number, blue_number=declared,
                    score=EXPLICIT_SCORE, reasons=("explicit_link", "target_missing"),
                    link_declared=True,
                ))
                continue
            reasons = ["explicit_link"]
            score = EXPLICIT_SCORE
            if blue.gross_cents == red_amt:
                reasons.append("amount_exact")
            candidates.append(MatchCandidate(
                red_number=red.number, blue_number=blue.number,
                score=score, reasons=tuple(reasons), link_declared=True,
            ))
        # 2) 推断候选（未声明或需要额外组合冲销时）
        for blue in blues:
            if blue.number in seen_numbers:
                continue
            score = 0
            reasons: List[str] = []
            if red.counterparty and blue.counterparty and red.counterparty == blue.counterparty:
                score += 30
                reasons.append("same_counterparty")
            if blue.gross_cents == red_amt:
                score += 40
                reasons.append("amount_exact")
            if _tax_mix(red) == _tax_mix(blue):
                score += 15
                reasons.append("same_tax_mix")
            if len(red.lines) == len(blue.lines):
                score += 10
                reasons.append("same_line_count")
            if blue.gross_cents <= red_amt:
                score += 5
                reasons.append("fits_within_red")
            if score > 0:
                candidates.append(MatchCandidate(
                    red_number=red.number, blue_number=blue.number,
                    score=min(score, 95), reasons=tuple(reasons), link_declared=False,
                ))
    # 确定性排序：红字号、显式优先、评分降序、蓝字号
    candidates.sort(key=lambda c: (c.red_number, not c.link_declared, -c.score, c.blue_number))
    return candidates


# ---------------------------------------------------------------- 最小费用流

class _Edge:
    __slots__ = ("u", "v", "cap", "cost", "rev", "tag")

    def __init__(self, u, v, cap, cost, tag):
        self.u, self.v, self.cap, self.cost, self.rev, self.tag = u, v, cap, cost, None, tag


class MinCostFlow:
    def __init__(self, n: int):
        self.n = n
        self.g: List[List[_Edge]] = [[] for _ in range(n)]

    def add_edge(self, u, v, cap, cost, tag=None):
        fwd = _Edge(u, v, cap, cost, tag)
        rev = _Edge(v, u, 0, -cost, None)
        fwd.rev = rev
        rev.rev = fwd
        self.g[u].append(fwd)
        self.g[v].append(rev)
        return fwd

    def flow(self, s, t, maxf):
        """Bellman-Ford 逐次增广；返回 (实际流量, 总费用, 边引用列表)。"""
        total_cost = 0
        flowed = 0
        path_edges: List[_Edge] = []
        while flowed < maxf:
            dist = [INF] * self.n
            prev_edge: List[Optional[_Edge]] = [None] * self.n
            dist[s] = 0
            updated = True
            while updated:  # Bellman-Ford，含负费用反向边
                updated = False
                for u in range(self.n):
                    if dist[u] == INF:
                        continue
                    for e in self.g[u]:
                        if e.cap > 0 and dist[e.v] > dist[u] + e.cost:
                            dist[e.v] = dist[u] + e.cost
                            prev_edge[e.v] = e
                            updated = True
            if dist[t] == INF:
                break
            # 找瓶颈容量
            aug = maxf - flowed
            v = t
            while v != s:
                e = prev_edge[v]
                aug = min(aug, e.cap)
                v = e.u
            v = t
            while v != s:
                e = prev_edge[v]
                e.cap -= aug
                e.rev.cap += aug
                if e.tag is not None:
                    path_edges.append(e)
                v = e.u
            total_cost += aug * dist[t]
            flowed += aug
        return flowed, total_cost, path_edges


# ---------------------------------------------------------------- 分配策略

def _edge_unit_cost(candidate: MatchCandidate) -> int:
    # 显式关联 0/分；推断候选 5..95/分
    return max(0, EXPLICIT_SCORE - candidate.score)


def global_allocate(reds: List[Invoice], blues: List[Invoice],
                    candidates: List[MatchCandidate], rule_version: str) -> AllocationPlan:
    blue_by_number = {b.number: b for b in blues}
    red_index = {r.number: i for i, r in enumerate(reds)}
    blue_index = {b.number: i for i, b in enumerate(blues)}
    n_red, n_blue = len(reds), len(blues)
    source = 0
    red_base = 1
    blue_base = red_base + n_red
    sink = blue_base + n_blue
    mcf = MinCostFlow(sink + 1)

    red_edges: Dict[str, _Edge] = {}
    blue_edges: Dict[str, _Edge] = {}
    match_edges: Dict[Tuple[str, str], _Edge] = {}

    total_red = 0
    for red in reds:
        amt = -red.gross_cents
        total_red += amt
        e = mcf.add_edge(source, red_base + red_index[red.number], amt, 0, tag=("red", red.number))
        red_edges[red.number] = e
        # 红字未分配出口：高惩罚
        mcf.add_edge(red_base + red_index[red.number], sink, INF, UNALLOCATED_RED_PENALTY,
                     tag=("red_dump", red.number))
    total_blue = 0
    for blue in blues:
        total_blue += blue.gross_cents
        e = mcf.add_edge(blue_base + blue_index[blue.number], sink, blue.gross_cents, 0,
                         tag=("blue", blue.number))
        blue_edges[blue.number] = e

    candidate_by_pair: Dict[Tuple[str, str], MatchCandidate] = {}
    for cand in candidates:
        if cand.blue_number not in blue_index:
            continue  # 目标缺失的候选不进入流网络
        key = (cand.red_number, cand.blue_number)
        candidate_by_pair[key] = cand
        mcf.add_edge(
            red_base + red_index[cand.red_number],
            blue_base + blue_index[cand.blue_number],
            min(-reds[red_index[cand.red_number]].gross_cents,
                blues[blue_index[cand.blue_number]].gross_cents),
            _edge_unit_cost(cand),
            tag=("match", cand.red_number, cand.blue_number),
        )

    _, network_cost, _ = mcf.flow(source, sink, total_red)

    allocations: List[Allocation] = []
    red_used: Dict[str, int] = {r.number: 0 for r in reds}
    blue_used: Dict[str, int] = {b.number: 0 for b in blues}
    for (rnum, bnum), cand in sorted(candidate_by_pair.items()):
        e = None
        for edge in mcf.g[red_base + red_index[rnum]]:
            if edge.tag == ("match", rnum, bnum):
                e = edge
                break
        allocated = min(-reds[red_index[rnum]].gross_cents,
                        blues[blue_index[bnum]].gross_cents) - e.cap
        if allocated > 0:
            allocations.append(Allocation(
                red_number=rnum, blue_number=bnum, amount_cents=allocated,
                rule_version=rule_version, method="global",
                calc={
                    "unit_cost_per_cent": _edge_unit_cost(cand),
                    "score": cand.score,
                    "reasons": list(cand.reasons),
                    "edge_capacity_cents": min(-reds[red_index[rnum]].gross_cents,
                                               blues[blue_index[bnum]].gross_cents),
                },
            ))
            red_used[rnum] += allocated
            blue_used[bnum] += allocated

    unallocated_red = total_red - sum(red_used.values())
    unmatched_blue = total_blue - sum(blue_used.values())
    cost = _cost_breakdown(allocations, red_used, blue_used, reds, blues, network_cost)
    conservation = _conservation(allocations, reds, blues)
    return AllocationPlan(
        candidates=tuple(candidates),
        allocations=tuple(_sorted_allocations(allocations)),
        method="global",
        cost=cost,
        conservation=conservation,
        unallocated_red_cents=unallocated_red,
        unmatched_blue_cents=unmatched_blue,
    )


def greedy_allocate(reds: List[Invoice], blues: List[Invoice],
                    candidates: List[MatchCandidate], rule_version: str) -> AllocationPlan:
    """顺序敏感的贪心分配，用于与全局分配比较失败代价。"""
    blue_by_number = {b.number: b for b in blues}
    blue_remaining = {b.number: b.gross_cents for b in blues}
    red_remaining = {r.number: -r.gross_cents for r in reds}
    by_red: Dict[str, List[MatchCandidate]] = {}
    for cand in candidates:
        by_red.setdefault(cand.red_number, []).append(cand)

    allocations: List[Allocation] = []
    for red in reds:  # 严格按输入顺序
        ordered = sorted(
            by_red.get(red.number, []),
            key=lambda c: (not c.link_declared, -c.score, c.blue_number),
        )
        for cand in ordered:
            if red_remaining[red.number] <= 0:
                break
            blue = blue_by_number.get(cand.blue_number)
            if blue is None or blue_remaining[cand.blue_number] <= 0:
                continue
            amount = min(red_remaining[red.number], blue_remaining[cand.blue_number])
            allocations.append(Allocation(
                red_number=red.number, blue_number=cand.blue_number,
                amount_cents=amount, rule_version=rule_version, method="greedy",
                calc={
                    "unit_cost_per_cent": _edge_unit_cost(cand),
                    "score": cand.score,
                    "reasons": list(cand.reasons),
                    "pick_order": "input_order_then_score",
                },
            ))
            red_remaining[red.number] -= amount
            blue_remaining[cand.blue_number] -= amount

    red_used = {r.number: -r.gross_cents - red_remaining[r.number] for r in reds}
    blue_used = {b.number: b.gross_cents - blue_remaining[b.number] for b in blues}
    total_red = sum(-r.gross_cents for r in reds)
    total_blue = sum(b.gross_cents for b in blues)
    unallocated_red = total_red - sum(red_used.values())
    unmatched_blue = total_blue - sum(blue_used.values())
    network_cost = 0  # 贪心没有网络目标值，代价全部由同一惩罚公式重算
    cost = _cost_breakdown(allocations, red_used, blue_used, reds, blues, network_cost)
    conservation = _conservation(allocations, reds, blues)
    return AllocationPlan(
        candidates=tuple(candidates),
        allocations=tuple(_sorted_allocations(allocations)),
        method="greedy",
        cost=cost,
        conservation=conservation,
        unallocated_red_cents=unallocated_red,
        unmatched_blue_cents=unmatched_blue,
    )


# ---------------------------------------------------------------- 守恒与代价

def _cost_breakdown(allocations, red_used, blue_used, reds, blues, network_cost) -> Dict[str, int]:
    edge_cost = 0
    for a in allocations:
        edge_cost += a.amount_cents * a.calc["unit_cost_per_cent"]
    total_red = sum(-r.gross_cents for r in reds)
    total_blue = sum(b.gross_cents for b in blues)
    unallocated_red = total_red - sum(red_used.values())
    unmatched_blue = total_blue - sum(blue_used.values())
    red_penalty = unallocated_red * UNALLOCATED_RED_PENALTY
    blue_penalty = unmatched_blue * UNMATCHED_BLUE_PENALTY
    return {
        "edge_cost": edge_cost,
        "unallocated_red_cents": unallocated_red,
        "unmatched_blue_cents": unmatched_blue,
        "unallocated_red_penalty": red_penalty,
        "unmatched_blue_penalty": blue_penalty,
        "network_reference_cost": network_cost,
        "total_failure_cost": edge_cost + red_penalty + blue_penalty,
    }


def _conservation(allocations: List[Allocation], reds: List[Invoice],
                  blues: List[Invoice]) -> Dict[str, Any]:
    """守恒检查：任一红字分配不超过其票面、任一蓝字被冲不超过其票面。"""
    per_red: Dict[str, int] = {r.number: 0 for r in reds}
    per_blue: Dict[str, int] = {b.number: 0 for b in blues}
    for a in allocations:
        per_red[a.red_number] = per_red.get(a.red_number, 0) + a.amount_cents
        per_blue[a.blue_number] = per_blue.get(a.blue_number, 0) + a.amount_cents
    red_cap = {r.number: -r.gross_cents for r in reds}
    blue_cap = {b.number: b.gross_cents for b in blues}
    violations = []
    for number, used in per_red.items():
        if used > red_cap.get(number, 0):
            violations.append({"invoice": number, "used_cents": used,
                               "cap_cents": red_cap.get(number, 0), "side": "red"})
    for number, used in per_blue.items():
        if used > blue_cap.get(number, 0):
            violations.append({"invoice": number, "used_cents": used,
                               "cap_cents": blue_cap.get(number, 0), "side": "blue"})
    return {
        "holds": not violations,
        "violations": violations,
        "red_used_cents": {k: from_cents(v) for k, v in sorted(per_red.items())},
        "blue_used_cents": {k: from_cents(v) for k, v in sorted(per_blue.items())},
    }


def _sorted_allocations(allocations: List[Allocation]) -> List[Allocation]:
    return sorted(allocations, key=lambda a: (a.red_number, a.blue_number))


def build_plans(reds: List[Invoice], blues: List[Invoice],
                rule_version: str) -> Tuple[AllocationPlan, AllocationPlan]:
    """同一组候选上同时跑两种策略，返回 (global, greedy)。"""
    candidates = generate_candidates(reds, blues)
    return (
        global_allocate(reds, blues, candidates, rule_version),
        greedy_allocate(reds, blues, candidates, rule_version),
    )
