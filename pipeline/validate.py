"""定义与快照的解析及静态校验。

定义级校验是“全有或全无”：收集全部问题后抛出 ``DefinitionRejected``，
绝不基于半份定义产出部分事实。快照级错误只拒绝该快照（``SnapshotRejected``），
不影响同批的其他独立答卷。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import (
    DefinitionError,
    DefinitionRejected,
    SnapshotError,
    SnapshotRejected,
)
from .models import (
    AnswerCell,
    Definition,
    Gate,
    GroupNode,
    Instance,
    InstanceList,
    Node,
    QuestionNode,
    Snapshot,
)

GATE_OPS = {"eq", "answered", "and", "or", "not", "exists"}
MAX_GROUP_DEPTH = 2
QUESTION_TYPES = {"choice", "text", "number", "bool"}


# ---------------------------------------------------------------------------
# 定义解析
# ---------------------------------------------------------------------------


def _parse_gate(
    raw: Any,
    errors: list[DefinitionError],
    owner_path: str,
    field_name: str,
) -> Gate | None:
    loc = f"{owner_path}.{field_name}"
    if not isinstance(raw, dict):
        errors.append(DefinitionError("GATE_SHAPE", "门控必须是对象", loc))
        return None
    op = raw.get("op")
    if op not in GATE_OPS:
        errors.append(
            DefinitionError(
                "GATE_OP", f"未知门控操作符 {op!r}，合法集合 {sorted(GATE_OPS)}", loc
            )
        )
        return None

    if op in ("eq", "answered"):
        ref = raw.get("ref")
        if not isinstance(ref, str) or not ref:
            errors.append(DefinitionError("GATE_REF", "ref 必须为非空字符串", loc))
            ref = None
        value = None
        if op == "eq":
            if "value" not in raw:
                errors.append(DefinitionError("GATE_VALUE", "eq 缺少 value", loc))
            else:
                value = raw["value"]
                if not _is_scalar(value) or value is None:
                    errors.append(
                        DefinitionError(
                            "GATE_VALUE", "eq 的 value 必须是非空标量", loc
                        )
                    )
        return Gate(op=op, raw=raw, ref=ref, value=value)

    if op == "not":
        sub = _parse_gate(raw.get("arg"), errors, owner_path, field_name + ".arg")
        return Gate(op=op, raw=raw, args=(sub,) if sub is not None else ())

    if op in ("and", "or"):
        raw_args = raw.get("args")
        if not isinstance(raw_args, list) or not raw_args:
            errors.append(
                DefinitionError("GATE_SHAPE", f"{op} 需要非空 args 列表", loc)
            )
            return Gate(op=op, raw=raw)
        subs = tuple(
            g
            for g in (
                _parse_gate(a, errors, owner_path, f"{field_name}.args[{i}]")
                for i, a in enumerate(raw_args)
            )
            if g is not None
        )
        return Gate(op=op, raw=raw, args=subs)

    # exists
    group = raw.get("group")
    ref = raw.get("ref")
    if not isinstance(group, str) or not group:
        errors.append(DefinitionError("GATE_GROUP", "exists 缺少合法 group", loc))
        group = None
    if not isinstance(ref, str) or not ref:
        errors.append(DefinitionError("GATE_REF", "exists 缺少合法 ref", loc))
        ref = None
    if "value" not in raw:
        errors.append(DefinitionError("GATE_VALUE", "exists 缺少 value", loc))
        value = None
    else:
        value = raw["value"]
        if not _is_scalar(value) or value is None:
            errors.append(
                DefinitionError("GATE_VALUE", "exists 的 value 必须是非空标量", loc)
            )
    return Gate(op=op, raw=raw, group=group, ref=ref, value=value)


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def _build_nodes(
    raw_nodes: Any,
    errors: list[DefinitionError],
    parent_path: str | None,
    scope_chain: tuple[str, ...],
    depth: int,
    order_counter: list[int],
    nodes: dict[str, Node],
    order: list[str],
    seen_ids: set[str],
) -> tuple[list[Node], dict[str | None, list[Node]]]:
    """前序遍历定义树，赋静态路径与全局序号。"""
    local_children: list[Node] = []
    if not isinstance(raw_nodes, list):
        errors.append(DefinitionError("NODE_SHAPE", "nodes/children 必须是数组"))
        return local_children, {}

    child_groups: dict[str | None, list[Node]] = {}
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict) or not isinstance(raw_node.get("id"), str) \
                or not raw_node["id"]:
            errors.append(DefinitionError("NODE_SHAPE", "节点缺少非空 id"))
            continue
        node_id = raw_node["id"]
        if "." in node_id:
            errors.append(
                DefinitionError("NODE_ID", "id 不能包含 '.'（路径分隔符）", node_id)
            )
            continue
        static_path = f"{parent_path}.{node_id}" if parent_path else node_id
        if node_id in seen_ids:
            errors.append(
                DefinitionError("DUP_ID", f"id {node_id!r} 在版本内重复", static_path)
            )
            continue
        seen_ids.add(node_id)

        node_type = raw_node.get("type", "choice")
        gate_raw = raw_node.get("gate")
        gate = (
            _parse_gate(gate_raw, errors, static_path, "gate")
            if gate_raw is not None
            else None
        )
        idx = order_counter[0]
        order_counter[0] += 1
        order.append(static_path)

        if node_type == "group":
            if depth >= MAX_GROUP_DEPTH:
                errors.append(
                    DefinitionError(
                        "NESTING",
                        f"重复组嵌套超过 {MAX_GROUP_DEPTH} 层",
                        static_path,
                    )
                )
            node = GroupNode(
                static_path=static_path,
                order=idx,
                parent_path=parent_path,
                scope_chain=scope_chain,
                gate=gate,
            )
            nodes[static_path] = node
            local_children.append(node)
            sub_children, sub_map = _build_nodes(
                raw_node.get("children", []),
                errors,
                static_path,
                scope_chain + (static_path,),
                depth + 1,
                order_counter,
                nodes,
                order,
                seen_ids,
            )
            child_groups[static_path] = sub_children
            child_groups.update(sub_map)
        elif node_type in QUESTION_TYPES:
            options: tuple[Any, ...] = ()
            if node_type == "choice":
                raw_options = raw_node.get("options")
                if not isinstance(raw_options, list) or not raw_options:
                    errors.append(
                        DefinitionError(
                            "OPTIONS", "选择题需要非空 options 数组", static_path
                        )
                    )
                else:
                    if not all(_is_scalar(o) and o is not None for o in raw_options):
                        errors.append(
                            DefinitionError(
                                "OPTIONS", "options 必须全部为非空标量", static_path
                            )
                        )
                    if len(set(raw_options)) != len(raw_options):
                        errors.append(
                            DefinitionError("OPTIONS", "options 存在重复值", static_path)
                        )
                    options = tuple(raw_options)
            node = QuestionNode(
                static_path=static_path,
                order=idx,
                parent_path=parent_path,
                scope_chain=scope_chain,
                gate=gate,
                kind=node_type,
                options=options,
            )
            nodes[static_path] = node
            local_children.append(node)
            if "children" in raw_node:
                errors.append(
                    DefinitionError("NODE_SHAPE", "题目节点不能有 children", static_path)
                )
        else:
            errors.append(
                DefinitionError(
                    "NODE_TYPE",
                    f"未知节点类型 {node_type!r}",
                    static_path,
                )
            )

    return local_children, child_groups


def _iter_gates(gate: Gate | None):
    if gate is None:
        return
    stack = [gate]
    while stack:
        g = stack.pop()
        yield g
        stack.extend(g.args)


def _check_gate_references(definition: Definition) -> list[DefinitionError]:
    """引用合法性：目标存在、作用域（同实例/祖先实例）、较早题；exists 的子组规则。"""
    errors: list[DefinitionError] = []
    nodes = definition.nodes

    for path in definition.order:
        node = nodes[path]
        if node.gate is None:
            continue
        for g in _iter_gates(node.gate):
            if g.op in ("eq", "answered") and g.ref:
                target = nodes.get(g.ref)
                if target is None:
                    errors.append(
                        DefinitionError(
                            "BAD_REF", f"引用了不存在的题 {g.ref!r}", path
                        )
                    )
                    continue
                if not isinstance(target, QuestionNode):
                    errors.append(
                        DefinitionError(
                            "BAD_REF", f"{g.ref!r} 是重复组而非题，不可用于 {g.op}", path
                        )
                    )
                    continue
                # 作用域：目标的实例链必须是当前节点实例链的前缀（同实例或祖先实例）。
                if node.scope_chain[: len(target.scope_chain)] != target.scope_chain:
                    errors.append(
                        DefinitionError(
                            "SCOPE",
                            f"门控只能引用同实例或祖先实例的题，{g.ref!r} 越界",
                            path,
                        )
                    )
                    continue
                if target.order >= node.order:
                    errors.append(
                        DefinitionError(
                            "ORDER",
                            f"只能引用较早的题，{g.ref!r} 不早于 {path!r}",
                            path,
                        )
                    )
            elif g.op == "exists":
                group = nodes.get(g.group or "")
                if group is None:
                    errors.append(
                        DefinitionError(
                            "BAD_REF", f"exists 引用不存在的子组 {g.group!r}", path
                        )
                    )
                    continue
                if not isinstance(group, GroupNode):
                    errors.append(
                        DefinitionError(
                            "BAD_GROUP", f"exists 的 group {g.group!r} 不是重复组", path
                        )
                    )
                    continue
                expected_chain = node.scope_chain + (group.static_path,)
                if group.scope_chain != node.scope_chain:
                    errors.append(
                        DefinitionError(
                            "SCOPE",
                            f"exists 的子组 {g.group!r} 必须是当前实例的直接子组",
                            path,
                        )
                    )
                    continue
                if group.order >= node.order:
                    errors.append(
                        DefinitionError(
                            "ORDER", f"只能询问前面的子组，{g.group!r} 不早于 {path!r}", path
                        )
                    )
                    continue
                target = nodes.get(g.ref or "")
                if not isinstance(target, QuestionNode):
                    errors.append(
                        DefinitionError(
                            "BAD_REF", f"exists 的 ref {g.ref!r} 不是题", path
                        )
                    )
                    continue
                if target.scope_chain != expected_chain:
                    errors.append(
                        DefinitionError(
                            "SCOPE",
                            f"exists 的 ref {g.ref!r} 不属于子组 {g.group!r}",
                            path,
                        )
                    )
                    continue
                if target.order >= node.order:
                    errors.append(
                        DefinitionError(
                            "ORDER",
                            f"exists 的 ref {g.ref!r} 不早于 {path!r}",
                            path,
                        )
                    )
    return errors


def _check_cycles(definition: Definition) -> list[DefinitionError]:
    """门控引用图的环检测（着色 DFS）。

    顺序约束在结构上已排除环，这里显式兜底并给出 CYCLE 错误码。
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {path: WHITE for path in definition.order}
    errors: list[DefinitionError] = []

    def edges(path: str) -> list[str]:
        node = definition.nodes[path]
        out: list[str] = []
        for g in _iter_gates(node.gate):
            if g.op in ("eq", "answered") and g.ref in definition.nodes:
                out.append(g.ref)
            if g.op == "exists" and g.ref in definition.nodes:
                out.append(g.ref)
        return out

    def visit(path: str, stack: list[str]) -> None:
        color[path] = GRAY
        for nxt in edges(path):
            if color[nxt] == GRAY:
                cycle = " -> ".join(stack[stack.index(nxt):] + [nxt])
                errors.append(
                    DefinitionError("CYCLE", f"门控引用存在环: {cycle}", path)
                )
            elif color[nxt] == WHITE:
                visit(nxt, stack + [nxt])
        color[path] = BLACK

    for path in definition.order:
        if color[path] == WHITE:
            visit(path, [path])
    return errors


def validate_definition(raw: dict[str, Any]) -> Definition:
    """校验并构建 Definition；任何静态错误都整体拒绝。"""
    errors: list[DefinitionError] = []
    if not isinstance(raw, dict):
        raise DefinitionRejected(
            [DefinitionError("DEF_SHAPE", "定义根必须是 JSON 对象")]
        )
    version = raw.get("version")
    if not isinstance(version, str) or not version:
        errors.append(DefinitionError("VERSION", "version 必须是非空字符串"))

    nodes: dict[str, Node] = {}
    order: list[str] = []
    roots, child_map = _build_nodes(
        raw.get("nodes"),
        errors,
        None,
        (),
        0,
        [0],
        nodes,
        order,
        set(),
    )
    if not nodes and not errors:
        errors.append(DefinitionError("DEF_EMPTY", "定义没有任何节点"))

    definition = Definition(
        version=version if isinstance(version, str) else "<invalid>",
        nodes=nodes,
        order=tuple(order),
        roots=tuple(roots),
        children={
            parent: tuple(kids) for parent, kids in child_map.items()
        } | {None: tuple(roots)},
    )
    errors.extend(_check_gate_references(definition))
    errors.extend(_check_cycles(definition))
    if errors:
        raise DefinitionRejected(errors)
    return definition


def load_definition(path: str | Path) -> Definition:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return validate_definition(raw)


# ---------------------------------------------------------------------------
# 快照解析
# ---------------------------------------------------------------------------


def _parse_instance(
    raw: Any,
    definition: Definition,
    expected_group: str,  # "" 表示根
    errors: list[SnapshotError],
    loc: str,
    depth: int,
) -> Instance | None:
    if not isinstance(raw, dict):
        errors.append(SnapshotError("SNAP_SHAPE", "实例必须是对象", loc))
        return None
    if expected_group:
        instance_id = raw.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            errors.append(
                SnapshotError("INSTANCE_ID", "重复组实例需要非空稳定 instance_id", loc)
            )
            return None
    else:
        instance_id = ""

    instance = Instance(group_path=expected_group, instance_id=instance_id)

    # 本实例直接拥有的题：静态路径的作用域链必须正好等于该实例的组链。
    # 组静态路径的点分段即组链：outer.inner -> ("outer", "outer.inner")。
    scope_chain: tuple[str, ...] = (
        () if not expected_group else tuple(_accumulate(expected_group.split(".")))
    )
    raw_answers = raw.get("answers", {})
    if not isinstance(raw_answers, dict):
        errors.append(SnapshotError("SNAP_SHAPE", "answers 必须是对象", loc))
        raw_answers = {}
    for key, value in raw_answers.items():
        target = definition.nodes.get(key)
        if target is None:
            errors.append(
                SnapshotError("UNKNOWN_QUESTION", f"未知题路径 {key!r}", f"{loc}.{key}")
            )
            continue
        if not isinstance(target, QuestionNode):
            errors.append(
                SnapshotError("BAD_ANSWER_KEY", f"{key!r} 是重复组，不能放答案", f"{loc}.{key}")
            )
            continue
        if target.scope_chain != scope_chain:
            errors.append(
                SnapshotError(
                    "SCOPE",
                    f"题 {key!r} 不属于实例 {expected_group or '<root>'}",
                    f"{loc}.{key}",
                )
            )
            continue
        if not _is_snapshot_scalar(value):
            errors.append(
                SnapshotError(
                    "ANSWER_SHAPE", "答案必须是标量（null 表示显式缺答）", f"{loc}.{key}"
                )
            )
            continue
        instance.answers[key] = AnswerCell(value=value, present=True)

    raw_groups = raw.get("groups", {})
    if not isinstance(raw_groups, dict):
        errors.append(SnapshotError("SNAP_SHAPE", "groups 必须是对象", loc))
        raw_groups = {}

    # 该实例作用域下，定义里有哪些直接子组（根实例的键为 None）。
    allowed_child_groups = {
        n.static_path
        for n in definition.children.get(expected_group or None, ())
        if isinstance(n, GroupNode)
    }
    seen_ids: set[str] = set()
    for group_path, list_raw in raw_groups.items():
        if group_path not in allowed_child_groups:
            errors.append(
                SnapshotError(
                    "UNKNOWN_GROUP",
                    f"组 {group_path!r} 不存在或不是 {expected_group or '<root>'} 的直接子组",
                    f"{loc}.groups.{group_path}",
                )
            )
            continue
        if not isinstance(list_raw, dict) or not isinstance(
            list_raw.get("instances"), list
        ):
            errors.append(
                SnapshotError(
                    "SNAP_SHAPE",
                    "组清单必须是含 instances 数组的对象",
                    f"{loc}.groups.{group_path}",
                )
            )
            continue
        closed = bool(list_raw.get("closed", True))
        inst_list = InstanceList(group_path=group_path, instances=[], closed=closed)
        for i, inst_raw in enumerate(list_raw["instances"]):
            child = _parse_instance(
                inst_raw,
                definition,
                group_path,
                errors,
                f"{loc}.groups.{group_path}.instances[{i}]",
                depth + 1,
            )
            if child is not None:
                if child.instance_id in seen_ids:
                    errors.append(
                        SnapshotError(
                            "DUP_INSTANCE",
                            f"实例 ID {child.instance_id!r} 在组 {group_path} 内重复",
                            f"{loc}.groups.{group_path}",
                        )
                    )
                seen_ids.add(child.instance_id)
                inst_list.instances.append(child)
        instance.children[group_path] = inst_list

    return instance


def _is_snapshot_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def _accumulate(segments: list[str]) -> list[str]:
    result: list[str] = []
    for segment in segments:
        result.append(f"{result[-1]}.{segment}" if result else segment)
    return result


def load_snapshot(raw: dict[str, Any], definition: Definition) -> Snapshot:
    """基于已通过校验的定义解析快照；版本不匹配或结构错误则拒绝该快照。"""
    errors: list[SnapshotError] = []
    if not isinstance(raw, dict):
        raise SnapshotRejected(
            [SnapshotError("SNAP_SHAPE", "快照根必须是 JSON 对象")]
        )
    submission_id = raw.get("submission_id")
    if not isinstance(submission_id, str) or not submission_id:
        errors.append(SnapshotError("SUBMISSION_ID", "submission_id 必须是非空字符串"))
    version = raw.get("version")
    if version != definition.version:
        errors.append(
            SnapshotError(
                "VERSION_MISMATCH",
                f"快照引用版本 {version!r}，定义版本为 {definition.version!r}",
            )
        )
    root = _parse_instance(raw.get("root"), definition, "", errors, "root", 0)
    if errors:
        raise SnapshotRejected(errors)
    return Snapshot(
        submission_id=submission_id,
        version=definition.version,
        root=root,  # type: ignore[arg-type]
    )


def load_snapshot_file(path: str | Path, definition: Definition) -> Snapshot:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return load_snapshot(raw, definition)
