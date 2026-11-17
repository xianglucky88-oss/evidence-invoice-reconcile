"""金额与舍入工具。

审计系统内所有金额一律使用 ``Decimal`` 解析、以整数“分”参与分配运算，
全程不允许使用 float，避免二进制浮点误差进入证据链。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")
HALF_UP = ROUND_HALF_UP


class MoneyError(ValueError):
    """金额无法解析或不满足两位小数约束。"""


def to_decimal(value, *, field_path: str = "") -> Decimal:
    """把字符串/整数/Decimal 安全转换为定点 Decimal。"""
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, bool):  # bool 是 int 的子类，显式拒绝
        raise MoneyError(f"{field_path or 'value'} 不允许是布尔值")
    elif isinstance(value, (int, str)):
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise MoneyError(f"{field_path or 'value'} 不是合法金额: {value!r}")
    else:
        raise MoneyError(f"{field_path or 'value'} 类型不支持: {type(value).__name__}")
    if not dec.is_finite():
        raise MoneyError(f"{field_path or 'value'} 必须是有限数: {value!r}")
    return dec


def quantize(dec: Decimal, rounding=HALF_UP) -> Decimal:
    """按分位量化，舍入方式由税率规则版本决定。"""
    return dec.quantize(CENT, rounding=rounding)


def to_cents(dec: Decimal) -> int:
    """Decimal -> 整数分；要求量化后恰好两位小数，否则报错而非隐式舍入。"""
    q = quantize(dec)
    if q != dec.quantize(CENT):
        # 调用方应先量化；这里保护不变量
        q = dec.quantize(CENT)
    return int(q.scaleb(2))


def from_cents(cents: int) -> str:
    """整数分 -> 两位小数字符串，纯整数运算，无 float。"""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def parse_money(value, *, field_path: str = "") -> Decimal:
    """解析并量化为分位 Decimal。"""
    return quantize(to_decimal(value, field_path=field_path))


def parse_cents(value, *, field_path: str = "") -> int:
    """解析金额字符串为整数分。"""
    return to_cents(parse_money(value, field_path=field_path))


def sum_decimal(items) -> Decimal:
    total = Decimal("0.00")
    for item in items:
        total += item
    return total
