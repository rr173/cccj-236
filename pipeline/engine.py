"""事实裁决引擎。

对一份提交快照，按版本定义的静态前序、仅遍历快照中实际列出的实例，
逐题裁决：

  展示三态（visibility）：true 展示 / false 隐藏 / unknown 当时无法判定；
  作答状态（answer_status）：见下方常量，缓存值不等于作答。

关键规则（对应需求）：
  * 门控只可引用同实例或祖先实例的较早题（定义期已校验），同名题不跨实例取值；
  * eq 比较缺答值为 unknown；answered 在确定隐藏/展示未答时为 false、
    可见性未定时为 unknown；
  * exists：未封闭清单且无已知真时不得判假；父组确定隐藏时不受未封闭清单影响；
  * 非法缓存值不参与门控；题确实展示时标出无有效回答及具体原因；
  * 未列出的实例不产生任何事实行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import (
    AnswerCell,
    Definition,
    Evidence,
    FactRow,
    Gate,
    GroupNode,
    Instance,
    InstanceList,
    QuestionNode,
    Snapshot,
)
from .tristate import FALSE, TRUE, UNKNOWN, Tri, and_, not_, or_

# ---------------------------------------------------------------------------
# 作答状态码
# ---------------------------------------------------------------------------

ANSWERED = "answered"                          # 展示且有合法作答
DISPLAYED_UNANSWERED = "displayed_unanswered"  # 问了没答
ILLEGAL_CACHED = "illegal_cached_value"        # 展示但缓存值非法，无有效回答
NOT_ASKED = "not_asked"                        # 根本没问（确定隐藏，无缓存）
NOT_ASKED_CACHED = "not_asked_cached"          # 确定隐藏，缓存值被排除、不算作答
UNDETERMINED = "undetermined"                  # 是否该问当时无法判定，无缓存
UNDETERMINED_CACHED = "undetermined_cached"    # 可见性未定，缓存暂不采信


@dataclass
class _Ctx:
    """求值上下文的一层：实例 + 有效可见性 + 实例路径 + 该层组门控证据。"""

    instance: Instance
    visibility: Tri
    instance_path: str
    evidence: list[Evidence] = field(default_factory=list)


def strict_equal(left: Any, right: Any) -> bool:
    """JSON 语义的严格标量相等：bool 与 int 不互相等同。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if type(left) is not type(right):
        return False
    return left == right


class Engine:
    def __init__(self, definition: Definition, snapshot: Snapshot):
        self.definition = definition
        self.snapshot = snapshot
        self.rows: list[FactRow] = []
        # (静态路径, 实例路径) -> 可见性；只在同一实例内记忆，绝不跨实例。
        self._vis_cache: dict[tuple[str, str], Tri] = {}

    # ------------------------------------------------------------------
    # 实例路径与单元格解析
    # ------------------------------------------------------------------

    @staticmethod
    def _instance_path(parent_path: str, group_path: str, instance_id: str) -> str:
        segment = f"{group_path}#{instance_id}"
        return f"{parent_path}.{segment}" if parent_path else segment

    def _owner_context(self, target: QuestionNode, ctx: list[_Ctx]) -> _Ctx:
        """按题的作用域链取拥有该题的上下文层（同实例或祖先实例）。"""
        return ctx[len(target.scope_chain)]

    def _owner_instance(
        self, target: QuestionNode, ctx: list[_Ctx]
    ) -> Instance:
        return self._owner_context(target, ctx).instance

    def _raw_cell(self, target: QuestionNode, owner: Instance) -> AnswerCell | None:
        return owner.answers.get(target.static_path)

    def _is_legal(self, node: QuestionNode, value: Any) -> bool:
        if value is None:
            return False
        if node.kind == "choice":
            return any(strict_equal(value, opt) for opt in node.options)
        return isinstance(value, (str, int, float, bool))

    def _question_visibility(
        self,
        node: QuestionNode,
        ctx: list[_Ctx],
        evidence: list[Evidence],
    ) -> Tri:
        owner = self._owner_context(node, ctx)
        key = (node.static_path, owner.instance_path)
        cached = self._vis_cache.get(key)
        if cached is not None:
            return cached
        if node.gate is None:
            vis = owner.visibility
        else:
            gate_vis = self._eval_gate(
                node.gate, ctx[: len(node.scope_chain) + 1], evidence
            )
            vis = and_(owner.visibility, gate_vis)
        self._vis_cache[key] = vis
        return vis

    def _answered(
        self,
        node: QuestionNode,
        ctx: list[_Ctx],
        evidence: list[Evidence],
    ) -> tuple[Tri, AnswerCell | None, bool]:
        """返回（是否真正答过三态, 原始缓存, 缓存是否合法）。"""
        owner_layer = self._owner_context(node, ctx)
        vis = self._question_visibility(node, ctx, evidence)
        cell = self._raw_cell(node, owner_layer.instance)
        present = cell is not None and cell.present and cell.value is not None
        legal = present and self._is_legal(node, cell.value)  # type: ignore[union-attr]

        if vis is FALSE:
            # 确定不展示：即使带缓存也不是“真正答过”。
            return FALSE, cell, bool(legal)
        if vis is UNKNOWN:
            return UNKNOWN, cell, bool(legal)
        # 展示中：非法缓存或缺答都不是有效回答。
        return (TRUE if legal else FALSE), cell, bool(legal)

    # ------------------------------------------------------------------
    # 门控求值
    # ------------------------------------------------------------------

    def _eval_gate(
        self, gate: Gate, ctx: list[_Ctx], evidence: list[Evidence]
    ) -> Tri:
        if gate.op == "and":
            result = TRUE
            for sub in gate.args:
                result = and_(result, self._eval_gate(sub, ctx, evidence))
            return result
        if gate.op == "or":
            result = FALSE
            for sub in gate.args:
                result = or_(result, self._eval_gate(sub, ctx, evidence))
            return result
        if gate.op == "not":
            return not_(self._eval_gate(gate.args[0], ctx, evidence))
        if gate.op == "answered":
            return self._eval_answered(gate, ctx, evidence)
        if gate.op == "eq":
            return self._eval_eq(gate, ctx, evidence)
        if gate.op == "exists":
            return self._eval_exists(gate, ctx, evidence)
        raise ValueError(f"未知门控操作符 {gate.op!r}")  # 定义期已拦截

    def _target_node(self, ref: str | None) -> QuestionNode:
        node = self.definition.nodes[ref]  # 定义期已校验存在且为题
        assert isinstance(node, QuestionNode)
        return node

    def _eval_answered(
        self, gate: Gate, ctx: list[_Ctx], evidence: list[Evidence]
    ) -> Tri:
        node = self._target_node(gate.ref)
        answered, cell, legal = self._answered(node, ctx, evidence)
        owner = self._owner_context(node, ctx)
        self._evidence_ref(
            node, owner, cell, legal, used_op="answered",
            result=answered, evidence=evidence,
        )
        return answered

    def _eval_eq(
        self, gate: Gate, ctx: list[_Ctx], evidence: list[Evidence]
    ) -> Tri:
        node = self._target_node(gate.ref)
        answered, cell, legal = self._answered(node, ctx, evidence)
        owner = self._owner_context(node, ctx)
        if answered is TRUE:
            result = TRUE if strict_equal(cell.value, gate.value) else FALSE  # type: ignore[union-attr]
            self._evidence_ref(
                node, owner, cell, legal, used_op="eq", result=result,
                compared=gate.value, evidence=evidence,
            )
            return result
        # 缺答（含隐藏、未定、非法缓存不参与）→ 未知。
        self._evidence_ref(
            node, owner, cell, legal, used_op="eq", result=UNKNOWN,
            compared=gate.value, evidence=evidence,
        )
        return UNKNOWN

    def _eval_exists(
        self, gate: Gate, ctx: list[_Ctx], evidence: list[Evidence]
    ) -> Tri:
        owner_layer = ctx[-1]
        group = self.definition.nodes[gate.group]
        assert isinstance(group, GroupNode)
        target = self._target_node(gate.ref)

        group_gate_vis = (
            self._eval_gate(group.gate, ctx, evidence)
            if group.gate is not None
            else TRUE
        )
        group_vis = and_(owner_layer.visibility, group_gate_vis)

        inst_list = owner_layer.instance.children.get(group.static_path)
        if inst_list is None:
            # 快照完全未给清单：视为已封闭的空清单（不造假实例）。
            inst_list = InstanceList(
                group_path=group.static_path, instances=[], closed=True
            )

        summary = Evidence(
            kind="exists",
            detail=(
                f"exists group={group.static_path} ref={gate.ref!r} "
                f"value={gate.value!r} closed={inst_list.closed} "
                f"listed={len(inst_list.instances)} group_visibility={group_vis.value}"
            ),
            static_path=gate.ref,
            status="pending",
        )
        evidence.append(summary)

        # 父组确定隐藏：未封闭清单不得把结果变成未知。
        if group_vis is FALSE:
            summary.status = "group_hidden_false"
            return FALSE

        known_true = False
        for child in inst_list.instances:
            child_path = self._instance_path(
                owner_layer.instance_path, group.static_path, child.instance_id
            )
            child_ctx = ctx + [_Ctx(child, group_vis, child_path)]
            answered, cell, legal = self._answered(target, child_ctx, evidence)
            if answered is TRUE and strict_equal(cell.value, gate.value):  # type: ignore[union-attr]
                known_true = True
                evidence.append(
                    Evidence(
                        kind="exists_match",
                        detail=(
                            f"实例 {child_path} 中 {gate.ref}={gate.value!r} 命中"
                        ),
                        static_path=target.static_path,
                        instance_path=child_path,
                        value=cell.value,  # type: ignore[union-attr]
                        status="true",
                    )
                )

        if known_true:
            summary.status = "true"
            return TRUE
        if inst_list.closed:
            summary.status = "closed_no_match_false"
            return FALSE
        summary.status = "open_unknown"
        return UNKNOWN

    def _evidence_ref(
        self,
        node: QuestionNode,
        owner: _Ctx,
        cell: AnswerCell | None,
        legal: bool,
        used_op: str,
        result: Tri,
        evidence: list[Evidence],
        compared: Any = None,
    ) -> None:
        if cell is None or not cell.present:
            status, detail = "missing", "快照无缓存值，按缺答处理"
            value = None
        elif cell.value is None:
            status, detail = "explicit_null", "显式 null，视为缺答"
            value = None
        elif not legal:
            status = "illegal"
            if node.kind == "choice":
                detail = (
                    f"缓存值 {cell.value!r} 不在版本合法值 "
                    f"{list(node.options)!r} 内，不参与门控"
                )
            else:
                detail = f"缓存值 {cell.value!r} 类型非法，不参与门控"
            value = cell.value
        else:
            value = cell.value
            # 可见性在 _answered 内已计算并按（静态路径, 实例路径）缓存。
            target_vis = self._vis_cache.get(
                (node.static_path, owner.instance_path), UNKNOWN
            )
            if target_vis is FALSE:
                status = "hidden_unused"
                detail = "缓存值合法但题确定未展示，按缺答处理，不参与门控"
            elif target_vis is UNKNOWN:
                status = "undetermined_unused"
                detail = "缓存值合法但题可见性未定，按缺答处理，不参与门控"
            else:
                status, detail = "used", "合法缓存值参与裁决"
        evidence.append(
            Evidence(
                kind="ref",
                detail=(
                    f"{used_op} 引用 {node.static_path}@"
                    f"{owner.instance_path or '<root>'} -> {result.value}；{detail}"
                    + (f"，比较值 {compared!r}" if compared is not None else "")
                ),
                static_path=node.static_path,
                instance_path=owner.instance_path,
                value=value,
                status=status,
            )
        )

    # ------------------------------------------------------------------
    # 遍历与事实行
    # ------------------------------------------------------------------

    def run(self) -> list[FactRow]:
        root_ctx = [_Ctx(self.snapshot.root, TRUE, "")]
        self._walk(self.snapshot.root, root_ctx, enclosing_list=None)
        return self.rows

    def _walk(
        self,
        instance: Instance,
        ctx: list[_Ctx],
        enclosing_list: InstanceList | None,
    ) -> None:
        # 根实例 group_path 为 ""，定义 children 以 None 键存放根节点。
        siblings = self.definition.children.get(instance.group_path or None, ())
        for node in siblings:
            if isinstance(node, QuestionNode):
                self._build_question_row(node, ctx, enclosing_list)
            else:
                self._walk_group(node, ctx, enclosing_list)

    def _walk_group(
        self,
        group: GroupNode,
        ctx: list[_Ctx],
        enclosing_list: InstanceList | None,
    ) -> None:
        owner_layer = ctx[-1]
        inst_list = owner_layer.instance.children.get(group.static_path)
        if inst_list is None:
            # 未提供清单 = 已封闭空清单；不产生任何实例行。
            return
        for child in inst_list.instances:  # 只遍历实际列出的实例
            child_path = self._instance_path(
                owner_layer.instance_path, group.static_path, child.instance_id
            )
            layer_evidence: list[Evidence] = []
            gate_vis = (
                self._eval_gate(group.gate, ctx, layer_evidence)
                if group.gate is not None
                else TRUE
            )
            inst_vis = and_(owner_layer.visibility, gate_vis)
            child_ctx = ctx + [_Ctx(child, inst_vis, child_path, layer_evidence)]
            self._walk(child, child_ctx, inst_list)

    def _build_question_row(
        self,
        node: QuestionNode,
        ctx: list[_Ctx],
        enclosing_list: InstanceList | None,
    ) -> None:
        owner = self._owner_context(node, ctx)
        # 祖先/本层组门控确实参与了可见性相与，并入本行证据。
        evidence: list[Evidence] = []
        for layer in ctx[: len(node.scope_chain) + 1]:
            evidence.extend(layer.evidence)
        own_gate_evidence: list[Evidence] = []
        vis = self._question_visibility(node, ctx, own_gate_evidence)
        evidence.extend(own_gate_evidence)

        cell = self._raw_cell(node, owner.instance)
        cached_value = cell.value if cell is not None and cell.present else None
        has_cache = cell is not None and cell.present and cell.value is not None
        legal = has_cache and self._is_legal(node, cached_value)

        if vis is TRUE:
            if legal:
                status, reason = ANSWERED, None
            elif has_cache and not legal:
                if node.kind == "choice":
                    status = ILLEGAL_CACHED
                    reason = (
                        f"缓存值 {cached_value!r} 不是该版本合法值，"
                        f"合法值为 {list(node.options)!r}；本题已展示但无有效回答"
                    )
                else:
                    status = ILLEGAL_CACHED
                    reason = f"缓存值 {cached_value!r} 类型非法；本题已展示但无有效回答"
            else:
                status, reason = DISPLAYED_UNANSWERED, "题已展示但没有有效回答"
        elif vis is FALSE:
            if has_cache:
                status = NOT_ASKED_CACHED
                reason = "题确定未展示（根本没问），缓存值保留但不计入作答"
                if not legal:
                    reason += "；且该缓存值对本版本非法"
            else:
                status, reason = NOT_ASKED, "题确定未展示（根本没问）"
        else:
            status = UNDETERMINED_CACHED if has_cache else UNDETERMINED
            reason = (
                "展示与否当时无法判定，缓存值暂不采信"
                if has_cache
                else "展示与否当时无法判定，且无缓存值"
            )

        if has_cache:
            evidence.append(
                Evidence(
                    kind="cached",
                    detail=(
                        f"本题缓存 {cached_value!r}，"
                        f"{'采信为作答' if status == ANSWERED else '未采信为作答'}"
                    ),
                    static_path=node.static_path,
                    instance_path=owner.instance_path,
                    value=cached_value,
                    status="used" if status == ANSWERED else "excluded",
                )
            )

        self.rows.append(
            FactRow(
                version=self.snapshot.version,
                submission_id=self.snapshot.submission_id,
                static_path=node.static_path,
                instance_path=owner.instance_path,
                visibility=vis,
                answer_status=status,
                answer_value=cached_value,
                reason=reason,
                evidence=evidence,
                list_closed=enclosing_list.closed if enclosing_list else None,
                list_instance_count=(
                    len(enclosing_list.instances) if enclosing_list else None
                ),
            )
        )


def evaluate_snapshot(
    definition: Definition, snapshot: Snapshot
) -> list[FactRow]:
    """对单份快照产出问题事实明细（未列出的实例不产生行）。"""
    return Engine(definition, snapshot).run()
