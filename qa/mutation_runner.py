"""Mutation runner: spawn one isolated worker subprocess per mutant."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .mutation import MUTATIONS, apply_mutation

WORKSPACE = Path(__file__).resolve().parents[1]
SRC = WORKSPACE / "src" / "evidence_invoice_reconcile"
MODULE_FILES = {
    "engine": "engine.py",
    "parser": "parser.py",
    "report": "report.py",
    "pipeline": "pipeline.py",
}


@dataclass
class MutationResult:
    mid: str
    module: str
    description: str
    status: str           # killed | survived | equivalent | not_applied
    detail: str = ""


def _materialize(mid: str, module: str, workdir: Path) -> bool:
    target_dir = workdir / "evidence_invoice_reconcile"
    shutil.copytree(SRC, target_dir,
                    ignore=shutil.ignore_patterns("__pycache__"))
    source_path = target_dir / MODULE_FILES[module]
    source = source_path.read_text()
    mutated, applied = apply_mutation(source, mid)
    if not applied:
        return False
    source_path.write_text(mutated)
    return True


def run_mutation(mutation, timeout: float = 120.0) -> MutationResult:
    if mutation.equivalent:
        return MutationResult(
            mutation.mid, mutation.module, mutation.description,
            "equivalent", mutation.justification)
    with tempfile.TemporaryDirectory(prefix="qa-mut-") as tmp:
        workdir = Path(tmp)
        if not _materialize(mutation.mid, mutation.module, workdir):
            return MutationResult(
                mutation.mid, mutation.module, mutation.description,
                "not_applied", "mutation point not found in source")
        env = {
            # no -I here on purpose: isolated mode would discard
            # PYTHONPATH. The paths are fully controlled (workspace +
            # mutated package dir injected first by the worker), so no
            # ambient site-packages influence the import.
            **__import__("os").environ,
            "PYTHONPATH": f"{workdir}:{WORKSPACE}:{WORKSPACE / 'src'}",
            "PYTHONNOUSERSITE": "1",
        }
        proc = subprocess.run(
            [sys.executable, "-S", "-m", "qa.mutation_worker",
             str(workdir), mutation.mid],
            cwd=str(WORKSPACE),
            capture_output=True, text=True, timeout=timeout,
            env=env,
        )
        line = proc.stdout.strip().splitlines()[-1] \
            if proc.stdout.strip() else ""
        try:
            payload = json.loads(line)
            status = payload.get("status", "killed")
            detail = payload.get("reason", proc.stderr[:500])
        except json.JSONDecodeError:
            status = "killed"
            detail = (line or proc.stderr[:500] or
                      f"worker exit {proc.returncode}")
        return MutationResult(
            mutation.mid, mutation.module, mutation.description,
            status, detail)


def run_all(timeout: float = 120.0) -> list[MutationResult]:
    results = []
    for mutation in MUTATIONS:
        results.append(run_mutation(mutation, timeout=timeout))
    return results
