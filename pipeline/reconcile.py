"""reconcile：同一提交的基准快照 + 两份离线回传快照的三路合并。

现场督导把一份已发出的答卷交给两台离线设备补录，设备各自回传**完整快照**，
但没有可靠的更新时间。本模块只处理这一次合并：

  * 以发出的快照为共同基准（base），三份快照必须是同一非空提交 ID、
    引用同一份定义，并各自通过快照结构校验；
  * 题目原始缓存字段按「字段未出现 / 显式 null / 具体值」三态做三路合并，
    与选项是否合法、题是否展示无关（裁决留给 `one`/`batch`）；
  * 重复组成员身份 = 所属组 + 完整祖先实例链 + 稳定 instance_id，
    不按数组行号对齐；
  * 合并后的快照重新用原定义裁决出完整事实表；绝不拼接两份回传的事实行。

成功时只原子写出一份 JSON：``{"snapshot": ..., "facts": ...}``。
输入错误或合并冲突时非零退出，不写部分答卷，也不替换已有输出。
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engine import evaluate_snapshot, strict_equal
from .errors import DefinitionRejected, SnapshotRejected
from .models import Definition, Snapshot
from .serialize import facts_to_dict
from .validate import load_snapshot, validate_definition

# 本入口的实例 ID：字母、数字、下划线和连字符组成的短码。
INSTANCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class ReconcileInputError(Exception):
    """输入无法读取、解析或未通过校验；指出出错文件与错误类型。"""

    def __init__(self, code: str, message: str, path: str | None = None):
        self.code = code
        self.path = path
        location = f" [{path}]" if path else ""
        super().__init__(f"{code}{location}: {message}")


@dataclass(frozen=True)
class Conflict:
    """一处无法自动合并的冲突。"""

    code: str
    location: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} [{self.location}]: {self.message}"


class MergeConflicts(Exception):
    """存在一处或多处冲突：不交付任何答卷。"""

    def __init__(self, conflicts: list[Conflict]):
        # 冲突报告顺序固定，便于复跑与比较。
        self.conflicts = sorted(
            conflicts, key=lambda c: (c.location, c.code, c.message)
        )
        super().__init__("; ".join(str(c) for c in self.conflicts))


# ---------------------------------------------------------------------------
# 严格 JSON 读取：重复键、非有限数字一律拒绝
# ---------------------------------------------------------------------------


class _DuplicateKey(ValueError):
    def __init__(self, key: str):
        self.key = key
        super().__init__(f"重复的 JSON 键 {key!r}")


def _reject_constant(token: str) -> Any:
    raise ValueError(f"非有限数字 {token!r} 不被接受")


def _parse_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError(f"非有限数字 {token!r} 不被接受")
    return value


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateKey(key)
        obj[key] = value
    return obj


def load_json_strict(path: str | Path) -> Any:
    """读取 JSON；重复键、NaN/Infinity/溢出浮点即拒绝。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(
                fh,
                object_pairs_hook=_object_pairs,
                parse_constant=_reject_constant,
                parse_float=_parse_float,
            )
    except _DuplicateKey as exc:
        raise ReconcileInputError(
            "DUPLICATE_JSON_KEY",
            f"对象中存在重复 JSON 键 {exc.key!r}，无法可靠判定取值",
            str(path),
        ) from exc
    except (OSError, ValueError) as exc:
        raise ReconcileInputError(
            "INVALID_JSON", f"无法读取或解析 JSON: {exc}", str(path)
        ) from exc


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------


def _child_path(path: str, key: Any) -> str:
    if isinstance(key, str) and key.isidentifier():
        return f"{path}.{key}" if path else key
    return f"{path}[{key!r}]" if path else f"[{key!r}]"


def _check_closed_flags(value: Any, path: str) -> None:
    """快照中任何位置出现的 ``closed`` 都必须是 JSON 布尔值。

    快照的其它嵌套位置（答案、清单）都不允许对象，因此这里出现的 ``closed``
    只可能是组清单字段或错放的结构字段，一律按同一规则拒绝。
    """
    if isinstance(value, dict):
        if "closed" in value and not isinstance(value["closed"], bool):
            raise ReconcileInputError(
                "NON_BOOLEAN_CLOSED",
                "组清单的 closed 必须是布尔值，"
                f"实际为 {value['closed']!r}",
                _child_path(path, "closed"),
            )
        for key, child in value.items():
            _check_closed_flags(child, _child_path(path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check_closed_flags(child, f"{path}[{index}]" if path else f"[{index}]")


def _check_instance_id(instance_id: Any, path: str) -> None:
    if not isinstance(instance_id, str) or not instance_id:
        # 交给 load_snapshot 统一报 INSTANCE_ID。
        return
    if INSTANCE_ID_PATTERN.fullmatch(instance_id) is None:
        raise ReconcileInputError(
            "INVALID_INSTANCE_ID",
            "实例 ID 必须是字母、数字、下划线和连字符组成的短码，"
            f"实际为 {instance_id!r}",
            path,
        )


def _check_instance_ids(instance: Any, path: str, check_self: bool) -> None:
    """递归检查重复组实例 ID 的短码格式（根实例没有身份 ID）。"""
    if not isinstance(instance, dict):
        return
    if check_self:
        _check_instance_id(instance.get("instance_id"), _child_path(path, "instance_id"))
    groups = instance.get("groups")
    if not isinstance(groups, dict):
        return
    for group_path, raw_list in groups.items():
        if not isinstance(raw_list, dict):
            continue
        instances = raw_list.get("instances")
        if not isinstance(instances, list):
            continue
        for index, child in enumerate(instances):
            child_path = (
                f"{path}.groups[{group_path!r}].instances[{index}]"
                if path
                else f"groups[{group_path!r}].instances[{index}]"
            )
            _check_instance_ids(child, child_path, True)


def load_definition_strict(path: str | Path) -> Definition:
    raw = load_json_strict(path)
    try:
        return validate_definition(raw)
    except DefinitionRejected as exc:
        raise ReconcileInputError(
            "DEFINITION_REJECTED",
            "定义未通过校验: " + "; ".join(str(e) for e in exc.errors),
            str(path),
        ) from exc


def load_snapshot_strict(
    path: str | Path, definition: Definition
) -> tuple[dict[str, Any], Snapshot]:
    raw = load_json_strict(path)
    _check_closed_flags(raw, "document")
    if isinstance(raw, dict):
        _check_instance_ids(raw.get("root"), "root", False)
    try:
        snapshot = load_snapshot(raw, definition)
    except SnapshotRejected as exc:
        raise ReconcileInputError(
            "SNAPSHOT_REJECTED",
            "快照未通过结构校验: " + "; ".join(str(e) for e in exc.errors),
            str(path),
        ) from exc
    return raw, snapshot


# ---------------------------------------------------------------------------
# 三路合并
# ---------------------------------------------------------------------------


def _answers_of(instance: Any) -> dict[str, Any]:
    answers = instance.get("answers") if isinstance(instance, dict) else None
    return answers if isinstance(answers, dict) else {}


def _groups_of(instance: Any) -> dict[str, Any]:
    groups = instance.get("groups") if isinstance(instance, dict) else None
    return groups if isinstance(groups, dict) else {}


def _cell(answers: dict[str, Any], key: str) -> tuple[bool, Any]:
    """题目原始字段的两种状态：未出现 / 出现（值可能是显式 null）。"""
    if key in answers:
        return True, answers[key]
    return False, None


def _cell_equal(left: tuple[bool, Any], right: tuple[bool, Any]) -> bool:
    if left[0] != right[0]:
        return False
    if not left[0]:
        return True
    return strict_equal(left[1], right[1])


def _list_closed(raw_list: Any) -> bool:
    """省略 ``closed`` 视为封闭；完全省略清单也由调用方按封闭空清单处理。"""
    if not isinstance(raw_list, dict):
        return True
    return bool(raw_list.get("closed", True))


def _list_instances(raw_list: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_list, dict):
        return []
    instances = raw_list.get("instances")
    if not isinstance(instances, list):
        return []
    return [instance for instance in instances if isinstance(instance, dict)]


def _member_map(raw_list: Any) -> dict[str, dict[str, Any]]:
    return {
        instance["instance_id"]: instance
        for instance in _list_instances(raw_list)
    }


def _instance_path(parent_path: str, group_path: str, instance_id: str) -> str:
    segment = f"{group_path}#{instance_id}"
    return f"{parent_path}.{segment}" if parent_path else segment


def _canonical_value(value: Any) -> tuple[str, Any]:
    """严格类型化的值键：true / 1 / 1.0 / "1" / null 互不混同。"""
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", value)
    if isinstance(value, str):
        return ("str", value)
    return ("other", repr(value))


def _canonical_instance(instance: dict[str, Any], is_root: bool = False) -> tuple:
    """忽略对象键顺序与实例数组顺序；省略清单 = 封闭空清单。"""
    group_items = []
    groups = _groups_of(instance)
    for group_path in sorted(groups):
        raw_list = groups[group_path]
        closed = _list_closed(raw_list)
        members = tuple(
            sorted(
                (
                    _canonical_instance(member)
                    for member in _list_instances(raw_list)
                ),
                key=lambda canonical: canonical[0],
            )
        )
        if closed and not members:
            continue
        group_items.append((group_path, closed, members))
    answers = _answers_of(instance)
    return (
        "" if is_root else instance.get("instance_id"),
        tuple(
            sorted(
                (key, _canonical_value(value))
                for key, value in answers.items()
            )
        ),
        tuple(group_items),
    )


def _render_instance(instance: dict[str, Any], is_root: bool = False) -> dict[str, Any]:
    """确定性输出：键与成员排序；封闭空清单等价于省略，故省略。"""
    out: dict[str, Any] = {}
    if not is_root:
        out["instance_id"] = instance["instance_id"]
    answers = _answers_of(instance)
    if answers:
        out["answers"] = {key: answers[key] for key in sorted(answers)}
    rendered_groups: dict[str, Any] = {}
    groups = _groups_of(instance)
    for group_path in sorted(groups):
        raw_list = groups[group_path]
        closed = _list_closed(raw_list)
        members = [
            _render_instance(member)
            for member in sorted(
                _list_instances(raw_list),
                key=lambda member: member["instance_id"],
            )
        ]
        if closed and not members:
            continue  # 封闭的空清单与省略等价
        rendered_groups[group_path] = {"instances": members, "closed": closed}
    if rendered_groups:
        out["groups"] = rendered_groups
    return out


def _answer_location(instance_path: str, static_path: str) -> str:
    return f"{instance_path or '<root>'} :: {static_path}"


def _group_location(parent_instance_path: str, group_path: str) -> str:
    parent = parent_instance_path or "<root>"
    return f"{parent} :: group {group_path}"


def _merge_cell(
    base: tuple[bool, Any],
    left: tuple[bool, Any],
    right: tuple[bool, Any],
    static_path: str,
    instance_path: str,
    conflicts: list[Conflict],
) -> tuple[bool, Any]:
    # 两边相同（含都去掉字段、都显式 null）只算一次。
    if _cell_equal(left, right):
        return left
    if _cell_equal(left, base):
        return right
    if _cell_equal(right, base):
        return left
    conflicts.append(
        Conflict(
            "ANSWER_CONFLICT",
            _answer_location(instance_path, static_path),
            "该题的原始字段被两边从基准改成不同结果（字段未出现与显式 null 也算不同状态）",
        )
    )
    return base


def _merge_answers(
    base: dict[str, Any],
    left: dict[str, Any],
    right: dict[str, Any],
    instance_path: str,
    conflicts: list[Conflict],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for static_path in sorted(set(base) | set(left) | set(right)):
        cell = _merge_cell(
            _cell(base, static_path),
            _cell(left, static_path),
            _cell(right, static_path),
            static_path,
            instance_path,
            conflicts,
        )
        if cell[0]:
            merged[static_path] = cell[1]
    return merged


def _merge_closed(
    base: bool,
    left: bool,
    right: bool,
    location: str,
    conflicts: list[Conflict],
) -> bool:
    if left == right:
        return left
    if left == base:
        return right
    if right == base:
        return left
    conflicts.append(
        Conflict(
            "CLOSED_CONFLICT",
            location,
            "该组清单的 closed 被两边改成不同结果",
        )
    )
    return base


def _merge_lists(
    base_list: Any,
    left_list: Any,
    right_list: Any,
    group_path: str,
    parent_instance_path: str,
    conflicts: list[Conflict],
) -> dict[str, Any]:
    location = _group_location(parent_instance_path, group_path)
    merged_closed = _merge_closed(
        _list_closed(base_list),
        _list_closed(left_list),
        _list_closed(right_list),
        location,
        conflicts,
    )

    base_members = _member_map(base_list)
    left_members = _member_map(left_list)
    right_members = _member_map(right_list)

    merged_members: list[dict[str, Any]] = []
    for instance_id in sorted(
        set(base_members) | set(left_members) | set(right_members)
    ):
        member_path = _instance_path(
            parent_instance_path, group_path, instance_id
        )
        base_member = base_members.get(instance_id)
        left_member = left_members.get(instance_id)
        right_member = right_members.get(instance_id)

        if base_member is not None:
            if left_member is None and right_member is None:
                continue  # 两方都删：删除
            if left_member is None:
                # 一方删除、另一方保留：原样保留则删除；改过（含任何后代）则冲突。
                if _canonical_instance(right_member) != _canonical_instance(base_member):
                    conflicts.append(
                        Conflict(
                            "DELETE_MODIFIED_CONFLICT",
                            member_path,
                            "该成员在一方被删除，另一方却修改了它或其任一后代；"
                            "不能复活成员，也不能丢掉修改",
                        )
                    )
                continue
            if right_member is None:
                if _canonical_instance(left_member) != _canonical_instance(base_member):
                    conflicts.append(
                        Conflict(
                            "DELETE_MODIFIED_CONFLICT",
                            member_path,
                            "该成员在一方被删除，另一方却修改了它或其任一后代；"
                            "不能复活成员，也不能丢掉修改",
                        )
                    )
                continue
            # 同一成员保留在两边：合并其内部互不冲突的改动。
            merged_members.append(
                _render_instance(
                    _merge_instance(
                        base_member,
                        left_member,
                        right_member,
                        instance_id,
                        False,
                        member_path,
                        conflicts,
                    )
                )
            )
            continue

        # 基准中原来没有的成员：只能由一方新增；两方同身份新增必须完整等价。
        if left_member is not None and right_member is not None:
            if _canonical_instance(left_member) != _canonical_instance(right_member):
                conflicts.append(
                    Conflict(
                        "NEW_MEMBER_CONFLICT",
                        member_path,
                        "两方以同一成员身份各自新增了内容不等价的成员；"
                        "不拼接成一个新成员",
                    )
                )
                continue
            merged_members.append(_render_instance(left_member))
        elif left_member is not None:
            merged_members.append(_render_instance(left_member))
        elif right_member is not None:
            merged_members.append(_render_instance(right_member))

    return {"instances": merged_members, "closed": merged_closed}


def _merge_instance(
    base: dict[str, Any],
    left: dict[str, Any],
    right: dict[str, Any],
    instance_id: str,
    is_root: bool,
    instance_path: str,
    conflicts: list[Conflict],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if not is_root:
        merged["instance_id"] = instance_id
    merged["answers"] = _merge_answers(
        _answers_of(base),
        _answers_of(left),
        _answers_of(right),
        instance_path,
        conflicts,
    )

    base_groups = _groups_of(base)
    left_groups = _groups_of(left)
    right_groups = _groups_of(right)
    merged_groups: dict[str, Any] = {}
    for group_path in sorted(set(base_groups) | set(left_groups) | set(right_groups)):
        merged_groups[group_path] = _merge_lists(
            base_groups.get(group_path),
            left_groups.get(group_path),
            right_groups.get(group_path),
            group_path,
            instance_path,
            conflicts,
        )
    merged["groups"] = merged_groups
    return merged


# ---------------------------------------------------------------------------
# 原子输出
# ---------------------------------------------------------------------------


def write_json_atomic(out_path: str | Path, payload: dict[str, Any]) -> None:
    """先写同目录临时文件再替换；失败时已有输出保持不变。"""
    target = Path(out_path)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


@dataclass
class ReconcileResult:
    payload: dict[str, Any]
    submission_id: str
    version: str
    fact_row_count: int


def reconcile(
    definition_path: str | Path,
    base_path: str | Path,
    left_path: str | Path,
    right_path: str | Path,
    out_path: str | Path,
) -> ReconcileResult:
    """执行一次三路合并；成功时只写出 ``{"snapshot": ..., "facts": ...}``。"""
    definition = load_definition_strict(definition_path)
    base_raw, base_snapshot = load_snapshot_strict(base_path, definition)
    left_raw, left_snapshot = load_snapshot_strict(left_path, definition)
    right_raw, right_snapshot = load_snapshot_strict(right_path, definition)

    submission_ids = {
        "base": base_snapshot.submission_id,
        "left": left_snapshot.submission_id,
        "right": right_snapshot.submission_id,
    }
    if len(set(submission_ids.values())) != 1:
        detail = "，".join(f"{name}={value!r}" for name, value in submission_ids.items())
        raise ReconcileInputError(
            "SUBMISSION_ID_MISMATCH",
            "三份快照必须属于同一个提交 ID：" + detail,
        )

    conflicts: list[Conflict] = []
    merged_root = _merge_instance(
        base_raw.get("root"),
        left_raw.get("root"),
        right_raw.get("root"),
        "",
        True,
        "",
        conflicts,
    )
    if conflicts:
        raise MergeConflicts(conflicts)

    merged_raw = {
        "submission_id": base_snapshot.submission_id,
        "version": definition.version,
        "root": _render_instance(merged_root, is_root=True),
    }

    # 合并后的快照重新通过结构校验，并用原定义裁决完整事实表。
    try:
        merged_snapshot = load_snapshot(merged_raw, definition)
    except SnapshotRejected as exc:  # 防御：输入均已校验，正常不会到这里。
        raise ReconcileInputError(
            "MERGED_SNAPSHOT_REJECTED",
            "合并结果未通过快照结构校验: " + "; ".join(str(e) for e in exc.errors),
        ) from exc
    rows = evaluate_snapshot(definition, merged_snapshot)
    payload = {
        "snapshot": merged_raw,
        "facts": facts_to_dict(
            merged_snapshot.submission_id, definition.version, rows
        ),
    }
    write_json_atomic(out_path, payload)
    return ReconcileResult(
        payload=payload,
        submission_id=merged_snapshot.submission_id,
        version=definition.version,
        fact_row_count=len(rows),
    )
