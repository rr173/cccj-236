"""测试辅助：内联构建定义/快照。"""

from __future__ import annotations

from typing import Any

from pipeline.validate import load_snapshot, validate_definition


def make_definition(raw_nodes: list[dict[str, Any]], version: str = "v1"):
    return validate_definition({"version": version, "nodes": raw_nodes})


def make_snapshot(raw_root: dict[str, Any], definition, submission_id: str = "s1",
                  version: str | None = None):
    return load_snapshot(
        {
            "submission_id": submission_id,
            "version": version or definition.version,
            "root": raw_root,
        },
        definition,
    )


def index(rows):
    """按 (静态路径, 实例路径) 索引事实行。"""
    return {(r.static_path, r.instance_path): r for r in rows}


def q(node_id: str, qtype: str = "choice", options: list[Any] | None = None,
      gate: dict[str, Any] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"id": node_id, "type": qtype}
    if qtype == "choice":
        node["options"] = options if options is not None else []
    if gate is not None:
        node["gate"] = gate
    return node


def grp(node_id: str, children: list[dict[str, Any]],
        gate: dict[str, Any] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"id": node_id, "type": "group", "children": children}
    if gate is not None:
        node["gate"] = gate
    return node


def eq(ref: str, value: Any) -> dict[str, Any]:
    return {"op": "eq", "ref": ref, "value": value}


def answered(ref: str) -> dict[str, Any]:
    return {"op": "answered", "ref": ref}


def exists(group: str, ref: str, value: Any) -> dict[str, Any]:
    return {"op": "exists", "group": group, "ref": ref, "value": value}


def neg(arg: dict[str, Any]) -> dict[str, Any]:
    return {"op": "not", "arg": arg}


def conj(*args: dict[str, Any]) -> dict[str, Any]:
    return {"op": "and", "args": list(args)}


def disj(*args: dict[str, Any]) -> dict[str, Any]:
    return {"op": "or", "args": list(args)}


def inst(instance_id: str, answers: dict[str, Any] | None = None,
         groups: dict[str, Any] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"instance_id": instance_id}
    if answers is not None:
        node["answers"] = answers
    if groups is not None:
        node["groups"] = groups
    return node


def ilist(*instances: dict[str, Any], closed: bool = True) -> dict[str, Any]:
    return {"instances": list(instances), "closed": closed}


def root(answers: dict[str, Any] | None = None,
         groups: dict[str, Any] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {}
    if answers is not None:
        node["answers"] = answers
    if groups is not None:
        node["groups"] = groups
    return node
