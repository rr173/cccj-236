"""定义与快照的领域模型。

静态路径（static path）：同一版本内题/组的稳定身份，形如
``q1``、``grp_a.q2``、``grp_outer.grp_inner.q3``。

实例路径（instance path）：某份快照内具体重复实例的定位，形如
``grp_a#i_1``、``grp_outer#o_1.grp_inner#n_2``；根级题无实例段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .tristate import Tri

# ---------------------------------------------------------------------------
# 问卷定义
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    """门控表达式（AST）。

    操作符：
      eq       等值比较，{"op": "eq", "ref": "q1", "value": 1}
      answered 是否真正答过，{"op": "answered", "ref": "q2"}
      and/or   合取/析取，{"op": "and", "args": [...]}
      not      否定，{"op": "not", "arg": {...}}
      exists   前面的子组中是否存在一个实例答了指定值：
               {"op": "exists", "group": "grp_child",
                "ref": "grp_child.q", "value": "yes"}
    """

    op: str
    raw: dict[str, Any]
    ref: str | None = None
    value: Any = None
    args: tuple["Gate", ...] = ()
    group: str | None = None


@dataclass
class Node:
    static_path: str
    order: int  # 版本内前序序号，保证“只可引用较早题”
    parent_path: str | None
    # 作用域链：从根到该节点所属实例的组路径。
    # 根级题为 ()；grp_a 内题为 ("grp_a",)。
    scope_chain: tuple[str, ...]
    gate: Gate | None = None


@dataclass
class QuestionNode(Node):
    kind: str = "choice"  # choice | text | number | bool
    options: tuple[Any, ...] = ()  # 仅 choice：合法值由题与版本确定


@dataclass
class GroupNode(Node):
    max_depth_note: str = ""


@dataclass
class Definition:
    version: str
    nodes: dict[str, Node]
    order: tuple[str, ...]  # 前序静态路径
    roots: tuple[Node, ...]
    children: dict[str | None, tuple[Node, ...]]


# ---------------------------------------------------------------------------
# 提交快照
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnswerCell:
    """快照中缓存的一个答案值。"""

    value: Any
    present: bool  # 快照中是否显式带了该题的键


@dataclass
class Instance:
    """一个重复组实例。

    answers   : 本实例直接拥有的题（静态路径 -> 缓存值）
    children  : 子组路径 -> 实例清单
    closed    : 该子组实例清单是否已封闭（采集器是否保证清单完整）
    """

    group_path: str  # 该实例所属组的静态路径；根为 ""
    instance_id: str  # 稳定 ID；根实例为 ""
    answers: dict[str, AnswerCell] = field(default_factory=dict)
    children: dict[str, "InstanceList"] = field(default_factory=dict)


@dataclass
class InstanceList:
    group_path: str
    instances: list[Instance]
    closed: bool


@dataclass
class Snapshot:
    submission_id: str
    version: str
    root: Instance


# ---------------------------------------------------------------------------
# 事实输出
# ---------------------------------------------------------------------------


@dataclass
class Evidence:
    """一条实际参与裁决的证据引用。

    kind:
      ref     对某题单元格的引用（门控读取）
      exists  对子组“存在”判断的汇总
      cached  本题缓存值（是否被采用及原因）
    """

    kind: str
    detail: str
    static_path: str | None = None
    instance_path: str | None = None
    value: Any = None
    status: str | None = None  # used | missing | illegal | hidden_missing ...


@dataclass
class FactRow:
    version: str
    submission_id: str
    static_path: str
    instance_path: str  # 根级题为 ""
    visibility: Tri  # true 展示 / false 隐藏 / unknown 未定
    answer_status: str  # 见 engine.ANSWER_* 常量
    answer_value: Any
    reason: str | None
    evidence: list[Evidence]
    # 仅对重复组输出清单状态（未列出的实例不造假行）：
    list_closed: bool | None = None
    list_instance_count: int | None = None
