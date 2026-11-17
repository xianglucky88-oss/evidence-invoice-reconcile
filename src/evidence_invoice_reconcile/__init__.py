"""证据级发票核验与可重放对账。

模块划分：
- money/taxrules: 金额定点运算与税率规则版本冻结
- models: 不可变领域契约
- hashing/parser: 证据哈希与输入解析（字段路径）
- matching: 候选生成、贪心 vs 全局最小费用流冲销分配
- engine: 异常生成、决策分级、付款/时间链核验
- journal: 仅追加哈希链账本、WAL 事务与重放
- pipeline: 四阶段事务边界编排
- report/cli: 证据报告与确定性 JSON 入口
"""
from .models import Decision, AnomalyLevel

__all__ = ["Decision", "AnomalyLevel"]
__version__ = "0.2.0"
