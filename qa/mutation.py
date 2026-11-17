"""AST-level mutation engine for the four production modules.

Mutations are applied to parsed source trees (never by text hacking),
one mutant per run, inside an isolated copy of the package that is
executed in a worker subprocess. The kill suite in
``qa.mutation_worker`` exercises parser/engine/report/pipeline behavior
on the shared deterministic corpus, so a surviving mutant means the QA
layer failed to distinguish the altered behavior.

Mutations deliberately target exactly the defect classes the finance
audit cares about:
  * amount types / rounding mode / quantize target
  * grouping (duplicate detection, reversal suffix, row filtering)
  * ordering (input order sensitivity, exception stability)
  * report fields (envelope keys, values, aliasing)
"""
from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class Mutation:
    mid: str
    module: str
    description: str
    equivalent: bool = False      # behavior-preserving on the contract
    justification: str = ""


MUTATIONS: list[Mutation] = [
    # ---- engine.py -------------------------------------------------------
    Mutation("E01", "engine", "ROUND_HALF_UP replaced by ROUND_HALF_DOWN"),
    Mutation("E02", "engine", "ROUND_HALF_UP replaced by ROUND_CEILING"),
    Mutation("E03", "engine", "quantize target 0.01 replaced by 0.1 (dime)"),
    Mutation("E04", "engine", "quantize target 0.01 replaced by 1 (whole)"),
    Mutation("E05", "engine", "tax product drops quantize (raw precision)"),
    Mutation("E06", "engine", "tax mismatch operator != replaced by =="),
    Mutation("E07", "engine", "gross accumulator uses net only (tax dropped)"),
    Mutation("E08", "engine", "gross accumulator uses tax only (net dropped)"),
    Mutation("E09", "engine", "reversal suffix -R replaced by -X"),
    Mutation("E10", "engine", "duplicate threshold >1 replaced by >2"),
    Mutation("E11", "engine", "final gross quantize removed"),
    Mutation("E12", "engine", "exception reason tax_mismatch renamed"),
    Mutation("E13", "engine", "exception reason duplicate_number renamed"),
    Mutation("E14", "engine", "paid_numbers default [] replaced by None"),
    # ---- parser.py -------------------------------------------------------
    Mutation("P01", "parser", "non-dict rows retained instead of filtered"),
    Mutation("P02", "parser", "row copy removed (source object aliasing)"),
    Mutation("P03", "parser", "error message text changed"),
    Mutation("P04", "parser", "rows returned in reversed input order"),
    # ---- report.py / pipeline.py envelope --------------------------------
    Mutation("R01", "pipeline", "project name string changed at build_result"),
    Mutation("R02", "report", "status value ok changed at build_result"),
    Mutation("R03", "report", "nested data dict instead of flattened"),
    Mutation("R04", "report", "PipelineResult receives swapped fields",
             equivalent=True,
             justification="PipelineResult stores both fields; swapping the "
                           "two kwargs changes only internal attribute names "
                           "which build_result never reads individually"),
]


class _RawRow(ast.NodeTransformer):
    """dict(row) -> row (removes the parser's defensive copy)."""

    def visit_Call(self, inner: ast.Call) -> ast.AST:
        self.generic_visit(inner)
        if isinstance(inner.func, ast.Name) and inner.func.id == "dict" \
                and len(inner.args) == 1:
            return inner.args[0]
        return inner


class _Mutator(ast.NodeTransformer):
    def __init__(self, mid: str):
        self.mid = mid
        self.applied = False

    # -- helpers -----------------------------------------------------------
    def _is_half_up(self, node: ast.AST) -> bool:
        return (isinstance(node, ast.Attribute)
                and node.attr == "ROUND_HALF_UP")

    def _rounding(self, name: str) -> ast.Attribute:
        self.applied = True
        return ast.Attribute(
            value=ast.Name(id="ROUND_HALF_UP", ctx=ast.Load()),
            attr=name, ctx=ast.Load(),
        )

    def _is_cent(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Constant) and node.value == "0.01"

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        if self.mid == "E09" and isinstance(node, ast.Constant):
            pass
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        # ROUND_HALF_UP is imported directly, so it appears as a Name
        if node.id == "ROUND_HALF_UP" and self.mid in ("E01", "E02"):
            self.applied = True
            return ast.copy_location(
                ast.Name(id=("ROUND_HALF_DOWN" if self.mid == "E01"
                             else "ROUND_CEILING"), ctx=node.ctx), node)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if self.mid == "E03" and node.value == "0.01":
            self.applied = True
            return ast.copy_location(ast.Constant("0.1"), node)
        if self.mid == "E04" and node.value == "0.01":
            self.applied = True
            return ast.copy_location(ast.Constant("1"), node)
        if self.mid == "E09" and node.value == "-R":
            self.applied = True
            return ast.copy_location(ast.Constant("-X"), node)
        if self.mid in ("E12",) and node.value == "tax_mismatch":
            self.applied = True
            return ast.copy_location(ast.Constant("tax_mismatch_X"), node)
        if self.mid in ("E13",) and node.value == "duplicate_number":
            self.applied = True
            return ast.copy_location(
                ast.Constant("duplicate_number_X"), node)
        if self.mid == "P03" and node.value == "invoices must be a list":
            self.applied = True
            return ast.copy_location(
                ast.Constant("invoices list required"), node)
        if self.mid == "R01" and node.value == "evidence-invoice-reconcile":
            self.applied = True
            return ast.copy_location(
                ast.Constant("evidence-invoice-reconcile-X"), node)
        if self.mid == "R02" and node.value == "ok":
            self.applied = True
            return ast.copy_location(ast.Constant("error"), node)
        return node

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if self.mid == "E06" and len(node.ops) == 1 \
                and isinstance(node.ops[0], ast.NotEq):
            self.applied = True
            node.ops = [ast.Eq()]
        if self.mid == "E10" and len(node.ops) == 1 \
                and isinstance(node.ops[0], ast.Gt) \
                and isinstance(node.comparators[0], ast.Constant) \
                and node.comparators[0].value == 1:
            self.applied = True
            node.comparators = [ast.Constant(2)]
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        # E14: payload.get("payments", []) -> payload.get("payments", None)
        # (lives inside the returned dict, not inside a subscript)
        if self.mid == "E14" and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and len(node.args) >= 2:
            if isinstance(node.args[0], ast.Constant) \
                    and node.args[0].value == "payments" \
                    and isinstance(node.args[1], ast.List) \
                    and not node.args[1].elts:
                self.applied = True
                node.args[1] = ast.Constant(None)
        # E05 / E11: remove quantize calls on the arithmetic path
        if self.mid in ("E05", "E11") \
                and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "quantize":
            if self.mid == "E05":
                # only the tax-line quantize has a rounding kwarg; the
                # gross-total quantize has just the decimal target
                if any(kw.arg == "rounding" for kw in node.keywords):
                    self.applied = True
                    return node.func.value
            else:
                if not any(kw.arg == "rounding" for kw in node.keywords):
                    self.applied = True
                    return node.func.value
        return node

    def _is_decimal_row_field(self, node: ast.AST, field: str) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Decimal"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Subscript)
            and isinstance(node.args[0].slice, ast.Constant)
            and node.args[0].slice.value == field
        )

    def _accumulator_match(self, node: ast.BinOp) -> bool:
        return (
            isinstance(node.op, ast.Add)
            and self._is_decimal_row_field(node.left, "net")
            and self._is_decimal_row_field(node.right, "tax")
        )

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        # E07/E08 target the net += Decimal(net) + Decimal(tax) addition
        if self.mid in ("E07", "E08") and self._accumulator_match(node):
            self.applied = True
            return node.left if self.mid == "E07" else node.right
        return node

    def visit_AugAssign(self, node: ast.AugAssign) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Add) and self.mid in ("E07", "E08") \
                and isinstance(node.value, ast.BinOp) \
                and self._accumulator_match(node.value):
            self.applied = True
            node.value = (node.value.left if self.mid == "E07"
                          else node.value.right)
        return node

    def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
        self.generic_visit(node)
        return node

    def visit_ListComp(self, node: ast.ListComp) -> ast.AST:
        self.generic_visit(node)
        if self.mid == "P01":
            # drop the `if isinstance(row, dict)` filter
            for gen in node.generators:
                before = len(gen.ifs)
                gen.ifs = [
                    cond for cond in gen.ifs
                    if not (isinstance(cond, ast.Call)
                            and isinstance(cond.func, ast.Name)
                            and cond.func.id == "isinstance")
                ]
                if len(gen.ifs) != before:
                    self.applied = True
        return node

    def visit_Return(self, node: ast.Return) -> ast.AST:
        self.generic_visit(node)
        if self.mid == "P02" and isinstance(node.value, ast.ListComp):
            # replace defensive dict(row) copies with raw row references
            original_dump = ast.dump(node.value)
            node.value.elt = _RawRow().visit(node.value.elt)
            if ast.dump(node.value) != original_dump:
                self.applied = True
        if self.mid == "P04" and isinstance(node.value, ast.ListComp):
            self.applied = True
            node.value = ast.Call(
                func=ast.Name(id="list", ctx=ast.Load()),
                args=[ast.Call(
                    func=ast.Name(id="reversed", ctx=ast.Load()),
                    args=[node.value], keywords=[])],
                keywords=[])
        return node

    def visit_Dict(self, node: ast.Dict) -> ast.AST:
        self.generic_visit(node)
        # R03: the **result.data spread appears as a dict entry whose key
        # is None; replace it with an explicit nested "data" key
        if self.mid == "R03":
            for i, key in enumerate(node.keys):
                if key is None:
                    node.keys[i] = ast.Constant("data")
                    self.applied = True
        return node

    def visit_keyword(self, node: ast.keyword) -> ast.keyword:
        self.generic_visit(node)
        # R03: flatten spread **result.data replaced by nested "data" key
        if self.mid == "R03" and node.arg is None:
            self.applied = True
            return ast.copy_location(ast.keyword(
                arg="data",
                value=ast.Attribute(
                    value=ast.Name(id="result", ctx=ast.Load()),
                    attr="data", ctx=ast.Load())), node)
        return node

    def visit_Call_constructor(self, node: ast.Call) -> ast.Call:  # R04
        return node


def apply_mutation(source: str, mid: str) -> tuple[str, bool]:
    """Return (mutated_source, applied). Raises if the mutation point is
    absent from this module."""
    tree = ast.parse(source)
    mutator = _Mutator(mid)
    mutated = mutator.visit(tree)
    ast.fix_missing_locations(mutated)
    if not mutator.applied:
        return source, False
    if mid in ("E01", "E02"):
        # the renamed rounding constant must be imported as well,
        # otherwise the mutant kills itself with NameError
        alias = "ROUND_HALF_DOWN" if mid == "E01" else "ROUND_CEILING"
        mutated.body.insert(0, ast.ImportFrom(
            module="decimal", names=[ast.alias(name=alias, asname=None)],
            level=0))
        ast.fix_missing_locations(mutated)
    return ast.unparse(mutated), True
