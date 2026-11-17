"""确定性 JSON 命令行入口。

用法::

    python -m evidence_invoice_reconcile.cli <input.json> [--state-dir DIR]
                                              [--out report.json]

不指定 --state-dir 时使用内存账本（进程结束即消失，适合一次性核验）；
指定后事件流持久化到目录，支持跨进程重放、重跑幂等与规则升级追溯。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import run

DEFAULT_FIXTURE = Path(__file__).parents[2] / "fixtures" / "input.json"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="证据级发票核验与可重放对账（OCR/接口/入账/付款证据链）")
    parser.add_argument("input", nargs="?", default=str(DEFAULT_FIXTURE),
                        help="输入 JSON（默认 fixtures/input.json）")
    parser.add_argument("--state-dir", default=None,
                        help="审计账本目录；不指定则使用内存账本")
    parser.add_argument("--out", default=None, help="报告输出文件；缺省打印到 stdout")
    args = parser.parse_args()

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    report = run(payload, state_dir=args.state_dir)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
