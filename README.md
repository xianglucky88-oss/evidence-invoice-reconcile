# Evidence Invoice Reconcile

证据级发票核验与可重放对账引擎：把 **OCR 发票、结构化开票接口、采购入账、
付款凭证** 绑定到同一条哈希证据链上，输出 `verified / manual / rejected`
三类结论，并为每个异常保留**字段路径、来源证据、规则版本、金额计算过程**。

## 审计不变量

| 不变量 | 实现位置 |
| --- | --- |
| 金额全程定点（整数分），无 float | `money.py` |
| 税率与舍入规则按版本冻结，历史结论永久绑定当时版本 | `taxrules.py` |
| 重复导出不得重复入账 | `pipeline.py` 账务提交阶段 + ledger 成员判断 |
| 红字可冲销一张或多张蓝票，但不得产生不可解释负余额 | `matching.py` 守恒 + `engine.py` HARD 拒绝 |
| 原始文件哈希变化 / 人工更正 / 跨期付款 / 部分匹配 → 新结论（supersedes 链），不覆盖旧记录 | `journal.py` 仅追加事件流 |
| 同输入哈希重跑返回同一 conclusion_id（幂等） | `journal.input_hash_index` |
| 事件哈希链防篡改，重放逐链校验 | `journal.replay()` |
| 批次中断后已确认结果不被静默改写 | 每票一个 WAL 事务 + 崩溃恢复 |

## 模块划分

- `money.py` — Decimal/整数分金额、按版本的 HALF_UP / HALF_EVEN 舍入。
- `taxrules.py` — 不可变规则版本注册表（`TAX-2018.5` 行级 HALF_UP、
  `TAX-2019.4` 发票级 HALF_EVEN、`TAX-2024.1` 服务税率调整）。
- `models.py` — frozen 领域契约：发票、明细行、证据引用、异常、分配、结论。
- `hashing.py` — 规范化 JSON 与 SHA-256 证据指纹（键序无关）。
- `parser.py` — 严格解析，错误携带 `field_path`（如 `invoices[2].lines[1].tax`）。
- `matching.py` — 候选生成 + 两套冲销分配：
  - `greedy`：输入顺序敏感，先到先占，可能抢占显式关联目标；
  - `global`：最小费用流（Bellman-Ford 增广），全局最优、顺序无关。
  - 两者共享候选与惩罚价格，失败代价可直接比较。
- `engine.py` — 纯函数核验：税额/合计守恒、同号重复、证据漂移、
  红字分配、付款部分匹配/超额/跨期、时间链，异常分级裁决。
- `journal.py` — 仅追加哈希链事件流、WAL 事务、文件锁、崩溃恢复、重放。
- `pipeline.py` — 四阶段事务边界编排（见下）。
- `report.py` / `cli.py` — 稳定证据报告与确定性 JSON 入口。

## 四阶段事务边界

1. **候选/分配（只读）**：解析 + 全局/贪心双计划 + 守恒检查，不落账，可整体重跑。
2. **结论（每票一个 WAL 事务）**：逐票生成 `CONCLUSION_ISSUED / CONCLUSION_SUPERSEDE`
   事件。单票失败隔离在本事务，不影响其他发票。
3. **账务提交（同一 WAL）**：仅 `verified` 写 `LEDGER_BOOKING`；
   重复导出、拒绝、人工挂账写 `LEDGER_REJECTED`。结论事件做快照隔离，提交后不可回写。
4. **证据报告（只读）**：从事件流重放生成，不修改任何状态。

崩溃时残留 `wal-<tx>.ndjson` 在下次打开时被隔离为 `aborted-*.ndjson` 并写入
`RUN_ABORT`；主流哈希链始终可验证。

## 失败代价模型

每分配边成本 = `(100 - 候选评分) × 分配分数`；显式关联评分为 100（零成本）。
惩罚单价（每分）：红字未分配 500、蓝字未被冲销 300，远高于边成本，
因此优化器先最大化覆盖、再优化匹配质量。贪心在容量受限时可能严格更差
（见 `tests/test_evidence.py::test_greedy_has_strictly_higher_failure_cost_*`）。

## 运行

```bash
# 一次性核验（内存账本）
PYTHONPATH=src python -m evidence_invoice_reconcile.cli

# 持久化审计账本（支持跨进程重放、重跑幂等、规则升级追溯）
PYTHONPATH=src python -m evidence_invoice_reconcile.cli fixtures/input.json \
    --state-dir ./audit-state --out report.json

# 测试
PYTHONPATH=src python -m unittest discover -s tests -v
```

## 输入契约要点

- 发票：`number/status(normal|red)/net/tax/rule_version/lines[]/red_links/
  evidence[]/export_id/source_hash/certified_at/booked_at/fiscal_period`。
- 明细行：`rate_code(goods|service|zero)/net/tax`，支持一票多行。
- 证据：`source(ocr|api|booking|payment|manual)/file_id/hash(64位十六进制)/locator`。
- 付款：`payment_id/invoice_number/amount/paid_at/fiscal_period/evidence[]`。
- 无 `lines` 时按单行简表兼容；`payments` 支持发票号字符串数组的旧格式。

## 异常分级

- `hard` → `rejected`：结构破坏、红字超蓝、不可解释负余额、付款目标缺失。
- `high` → `manual`：哈希漂移、同号冲突、税额/合计不符、红字未分配、
  部分匹配、跨期付款、证据不一致。
- `medium` → `manual`：重复导出、付款超额/无着、人工更正、时间链倒置。

所有异常的 `calc` 同时给出整数 `cents` 与两位十进制文本，计算过程可逐分复算。
