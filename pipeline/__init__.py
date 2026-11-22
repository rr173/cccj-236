"""离线问卷事实裁决管线。

输入：
  * 问卷定义（版本化、静态题路径、最多两层重复组、门控表达式）；
  * 提交快照（固定引用一个版本，带重复组实例及稳定 ID）。

输出：按提交查询的问题事实明细，区分展示三态与作答状态，并给出实际
参与裁决的证据引用。

仅使用 Python 标准库，且设计为以 ``python -I`` 运行。
"""

from .tristate import TRUE, FALSE, UNKNOWN, Tri, and_, or_, not_, tri_from_json
from .models import (
    Definition,
    QuestionNode,
    GroupNode,
    Node,
    Gate,
    Snapshot,
    Instance,
    AnswerCell,
)
from .errors import (
    DefinitionError,
    SnapshotError,
    DefinitionRejected,
    SnapshotRejected,
)
from .validate import load_definition, load_snapshot, validate_definition
from .engine import evaluate_snapshot, FactRow, Evidence

__all__ = [
    "TRUE",
    "FALSE",
    "UNKNOWN",
    "Tri",
    "and_",
    "or_",
    "not_",
    "tri_from_json",
    "Definition",
    "QuestionNode",
    "GroupNode",
    "Node",
    "Gate",
    "Snapshot",
    "Instance",
    "AnswerCell",
    "DefinitionError",
    "SnapshotError",
    "DefinitionRejected",
    "SnapshotRejected",
    "load_definition",
    "load_snapshot",
    "validate_definition",
    "evaluate_snapshot",
    "FactRow",
    "Evidence",
]
