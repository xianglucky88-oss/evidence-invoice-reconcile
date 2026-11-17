"""税率与舍入规则的版本注册表。

关键审计原则：规则按版本冻结（frozen），某张发票一经核验就永久绑定当时的
``rule_version``；后续规则升级不会改写历史结论，只会在重跑时产生带
``supersedes`` 链接的新结论。每个版本对象本身不可变。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, ROUND_HALF_EVEN
from typing import Dict, Tuple

from .money import Decimal, quantize


@dataclass(frozen=True)
class TaxRuleVersion:
    """一个不可变的税务规则版本。"""

    version: str
    description: str
    # 税率代码（货物/服务分类）-> 税率
    rates: Tuple[Tuple[str, Decimal], ...]
    # 税额舍入层级：line=按明细行各自舍入；invoice=先汇总净额再统一舍入
    rounding_level: str
    rounding: str  # decimal 模块的舍入常量名

    def rate_of(self, rate_code: str) -> Decimal:
        for code, rate in self.rates:
            if code == rate_code:
                return rate
        raise UnknownRateError(self.version, rate_code)

    def rounding_constant(self):
        return {"HALF_UP": ROUND_HALF_UP, "HALF_EVEN": ROUND_HALF_EVEN}[self.rounding]


class UnknownRateError(LookupError):
    def __init__(self, version: str, rate_code: str):
        super().__init__(f"规则版本 {version} 中不存在税率代码 {rate_code!r}")
        self.version = version
        self.rate_code = rate_code


class RuleVersionMissing(LookupError):
    pass


# 注册表内容在进程启动后即为常量，不允许业务代码修改。
_REGISTRY: Dict[str, TaxRuleVersion] = {}


def _register(rule: TaxRuleVersion) -> None:
    if rule.version in _REGISTRY:
        raise RuntimeError(f"规则版本重复注册: {rule.version}")
    _REGISTRY[rule.version] = rule


_register(
    TaxRuleVersion(
        version="TAX-2018.5",
        description="初版规则：按明细行 HALF_UP 舍入，13% 货物 / 6% 现代服务",
        rates=(("goods", Decimal("0.13")), ("service", Decimal("0.06")),
               ("zero", Decimal("0.00"))),
        rounding_level="line",
        rounding="HALF_UP",
    )
)
_register(
    TaxRuleVersion(
        version="TAX-2019.4",
        description="规则升级：发票级 HALF_EVEN（银行家舍入），税率代码不变",
        rates=(("goods", Decimal("0.13")), ("service", Decimal("0.06")),
               ("zero", Decimal("0.00"))),
        rounding_level="invoice",
        rounding="HALF_EVEN",
    )
)
_register(
    TaxRuleVersion(
        version="TAX-2024.1",
        description="服务税率调整为 5%，维持发票级 HALF_EVEN",
        rates=(("goods", Decimal("0.13")), ("service", Decimal("0.05")),
               ("zero", Decimal("0.00"))),
        rounding_level="invoice",
        rounding="HALF_EVEN",
    )
)

DEFAULT_RULE_VERSION = "TAX-2018.5"


def get_rule(version: str) -> TaxRuleVersion:
    try:
        return _REGISTRY[version]
    except KeyError:
        raise RuleVersionMissing(f"未知规则版本: {version!r}")


def available_versions() -> Tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def expected_line_tax(rule: TaxRuleVersion, net: Decimal, rate_code: str) -> Decimal:
    """按版本计算单行的预期税额（已按该版本的舍入常量量化）。"""
    rate = rule.rate_of(rate_code)
    return quantize(net * rate, rounding=rule.rounding_constant())


def expected_invoice_tax(rule: TaxRuleVersion, lines) -> Decimal:
    """按版本计算整张发票的预期税额。

    ``lines`` 中每项需含 net(Decimal) 与 rate_code。
    - line 级版本：逐行算税再求和（各行已经过分位舍入）；
    - invoice 级版本：先按税率代码汇总净额，再分别算税求和，
      与逐行舍入在半分边界上会产生可解释的 1 分钱差异。
    """
    rounding = rule.rounding_constant()
    if rule.rounding_level == "line":
        total = Decimal("0.00")
        for line in lines:
            total += expected_line_tax(rule, line["net"], line["rate_code"])
        return quantize(total, rounding=rounding)

    # invoice 级：先汇总同税率净额，再计算
    grouped: Dict[str, Decimal] = {}
    for line in lines:
        grouped[line["rate_code"]] = grouped.get(line["rate_code"], Decimal("0.00")) + line["net"]
    total = Decimal("0.00")
    for rate_code, net in grouped.items():
        total += quantize(net * rule.rate_of(rate_code), rounding=rounding)
    return quantize(total, rounding=rounding)
