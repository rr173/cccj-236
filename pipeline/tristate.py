"""三值逻辑（Kleene 强三值逻辑）。

取值：
  TRUE    确定为真（例如题确定展示，或条件确定成立）；
  FALSE   确定为假（例如题确定隐藏，或条件确定不成立）；
  UNKNOWN 无法判定（例如未封闭清单、缺答值参与比较、父组可见性未定）。
"""

from __future__ import annotations

from enum import Enum


class Tri(Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"

    def __str__(self) -> str:  # pragma: no cover - 调试辅助
        return self.value


TRUE = Tri.TRUE
FALSE = Tri.FALSE
UNKNOWN = Tri.UNKNOWN


def not_(value: Tri) -> Tri:
    """三值否定：UNKNOWN 取反仍为 UNKNOWN。"""
    if value is TRUE:
        return FALSE
    if value is FALSE:
        return TRUE
    return UNKNOWN


def and_(left: Tri, right: Tri) -> Tri:
    """三值合取：任一确定假即为假；否则任一未知即为未知。"""
    if left is FALSE or right is FALSE:
        return FALSE
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    return TRUE


def or_(left: Tri, right: Tri) -> Tri:
    """三值析取：任一确定真即为真；否则任一未知即为未知。"""
    if left is TRUE or right is TRUE:
        return TRUE
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    return FALSE


def tri_from_json(value: object) -> Tri:
    """解析 JSON 中的 ``true`` / ``false`` / ``"unknown"`` 字面量。"""
    if value is True:
        return TRUE
    if value is False:
        return FALSE
    if isinstance(value, str) and value in ("true", "false", "unknown"):
        return Tri(value)
    raise ValueError(f"非法三值字面量: {value!r}")
