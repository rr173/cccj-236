"""copy-audit：同一轮问卷的疑似复填线索核查（只发现线索，不改写任何答卷）。

输入：一个目录一级所有 ``*.facts.json``（现有 ``one``/``batch`` 产出的单份
事实表；``batch_report.json`` 与其他文件一律不读），以及操作员给出的同一
问卷版本下 4~8 个不同的根级题路径。

采信规则：只取根实例（``instance_path`` 为空）中所选题里
``visibility=true`` 且 ``answer_status=answered`` 的非空标量作答；没有采信
作答的题视为缺项。标量相等沿用引擎的 ``strict_equal``：布尔 ``true``、整数
``1``、浮点 ``1.0``、字符串 ``"1"`` 互不混同；不做模糊文本匹配，也不做
跨版本换码。未采信行的缓存值一律不校验、不读取。

直接线索：两份答卷在双方都采信作答的所选题上，同答案题数 >= 4 且不同
答案题数 <= 1；一侧或两侧缺项不计为相同或不同。

输出：JSON 清单（原子写入；本轮失败时已有清单保持不变）。能沿直接线索
互相到达的答卷归为同一簇，孤立答卷不入清单。每簇列出成员提交 ID 与簇内
**全部**直接线索（两端 ID、相同题路径、不同题路径）：不把间接相连的两份
说成直接相似，也不只输出把成员连起来的那几条。清单不复制答案或原始证据
值；内容不随读文件顺序、文件名或事实行顺序变化。

性能：直接线索要求至少 4 题同答案，因此合格的一对必然在某个 4 题子集上
取值完全一致。按「题子集 × 类型化取值」分桶即可不重不漏地找出全部候选
对，再逐对核验；绝不因常见答案覆盖全轮而逐对枚举全部答卷。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

from .engine import strict_equal

MIN_QUESTIONS = 4   # 操作员至少给出的所选题数
MAX_QUESTIONS = 8   # 操作员最多给出的所选题数
MIN_SAME = 4        # 直接线索要求的最少同答案题数
MAX_DIFFERENT = 1   # 直接线索允许的最多不同答案题数

# 事实行 answer_status 的全部合法取值（与 engine 的状态码一致）。
_ANSWER_STATUSES = frozenset({
    "answered",
    "displayed_unanswered",
    "illegal_cached_value",
    "not_asked",
    "not_asked_cached",
    "undetermined",
    "undetermined_cached",
})
_VISIBILITIES = frozenset({"true", "false", "unknown"})

# 缺项哨兵：任何合法标量都不会等于它。
_MISSING = object()


class CopyAuditError(Exception):
    """复填核查输入不合法：指出出错文件，本轮不交付任何清单。"""

    def __init__(self, code: str, message: str, path: str | None = None):
        self.code = code
        self.path = path
        location = f" [{path}]" if path else ""
        super().__init__(f"{code}{location}: {message}")


@dataclass(frozen=True)
class DirectClue:
    """一条直接线索：两端提交 ID（已按字典序排列）+ 相同/不同题路径。"""

    pair: tuple[str, str]
    same: tuple[str, ...]
    different: tuple[str, ...]


@dataclass
class AuditStats:
    """一轮核查的工作量统计（用于核查留痕，不写入清单）。"""

    submission_count: int
    candidate_pair_count: int   # 实际进入逐对核验的答卷对数
    clue_count: int
    cluster_count: int


# ---------------------------------------------------------------------------
# 题目参数
# ---------------------------------------------------------------------------


def validate_question_args(questions: list[str]) -> tuple[str, ...]:
    """校验操作员给出的所选题：4~8 个、互不相同、根级题路径。"""
    if not (MIN_QUESTIONS <= len(questions) <= MAX_QUESTIONS):
        raise CopyAuditError(
            "QUESTION_ARGS",
            f"所选题路径必须 {MIN_QUESTIONS}~{MAX_QUESTIONS} 个，"
            f"实际收到 {len(questions)} 个",
        )
    for question in questions:
        if not isinstance(question, str) or not question:
            raise CopyAuditError("QUESTION_ARGS", "所选题路径不能为空")
        if "." in question or "#" in question:
            raise CopyAuditError(
                "QUESTION_ARGS",
                f"所选题必须是根级题路径（不含 '.'/'#'）: {question!r}",
            )
    if len(set(questions)) != len(questions):
        raise CopyAuditError("QUESTION_ARGS", "所选题路径存在重复")
    return tuple(sorted(questions))


# ---------------------------------------------------------------------------
# 事实文件读取与校验（任一问题即指出文件并中止，不交付部分清单）
# ---------------------------------------------------------------------------


def _reject_constant(token: str) -> Any:
    raise ValueError(f"非标准 JSON 常量 {token!r}")


def _read_facts_file(path: Path) -> tuple[str, str, list[Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh, parse_constant=_reject_constant)
    except (OSError, ValueError) as exc:
        raise CopyAuditError(
            "BAD_FACTS_FILE", f"无法读取或解析事实文件: {exc}", str(path)
        ) from exc
    if not isinstance(payload, dict):
        raise CopyAuditError(
            "BAD_FACTS_FILE", "事实文件顶层必须是 JSON 对象", str(path)
        )
    submission_id = payload.get("submission_id")
    if not isinstance(submission_id, str) or not submission_id:
        raise CopyAuditError(
            "EMPTY_SUBMISSION_ID",
            "表头 submission_id 缺失、为空或不是字符串",
            str(path),
        )
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        raise CopyAuditError(
            "BAD_FACTS_FILE", "表头 version 缺失、为空或不是字符串", str(path)
        )
    facts = payload.get("facts")
    if not isinstance(facts, list):
        raise CopyAuditError(
            "BAD_FACTS_FILE", "表头 facts 必须是数组", str(path)
        )
    return submission_id, version, facts


def _is_legal_scalar(value: Any) -> bool:
    """合法标量：字符串/整数/浮点/布尔；null、数组、对象均不合法。"""
    return isinstance(value, (str, int, float, bool))


def _extract_answers(
    path: Path,
    submission_id: str,
    version: str,
    facts: list[Any],
    questions: tuple[str, ...],
) -> dict[str, Any]:
    """从一份事实表中提取所选根题的采信作答（根实例、展示、answered）。"""
    selected = set(questions)
    answers: dict[str, Any] = {}
    seen_root_rows: set[str] = set()
    for row in facts:
        if not isinstance(row, dict):
            raise CopyAuditError(
                "BAD_FACTS_FILE", "事实行必须是 JSON 对象", str(path)
            )
        if row.get("submission_id") != submission_id or row.get("version") != version:
            raise CopyAuditError(
                "ROW_HEADER_MISMATCH",
                f"事实行的 submission_id/version 与表头 "
                f"{submission_id!r}/{version!r} 不一致",
                str(path),
            )
        static_path = row.get("static_path")
        if not isinstance(static_path, str) or static_path not in selected:
            continue  # 非所选题：不属于本轮核查范围
        if row.get("instance_path") != "":
            continue  # 只取根实例
        if static_path in seen_root_rows:
            raise CopyAuditError(
                "DUPLICATE_QUESTION_ROW",
                f"同一答卷内所选根题 {static_path!r} 的根实例行重复",
                str(path),
            )
        seen_root_rows.add(static_path)
        visibility = row.get("visibility")
        status = row.get("answer_status")
        if (
            not isinstance(visibility, str)
            or visibility not in _VISIBILITIES
            or not isinstance(status, str)
            or status not in _ANSWER_STATUSES
        ):
            raise CopyAuditError(
                "BAD_FACTS_FILE",
                f"所选根题 {static_path!r} 行的 visibility/answer_status "
                f"非法: {visibility!r}/{status!r}",
                str(path),
            )
        if visibility == "true" and status == "answered":
            value = row.get("answer_value")
            if not _is_legal_scalar(value):
                raise CopyAuditError(
                    "ACCEPTED_WITHOUT_SCALAR",
                    f"所选根题 {static_path!r} 的行已采信（展示且 answered），"
                    f"但 answer_value 不是合法标量: {value!r}",
                    str(path),
                )
            answers[static_path] = value
        # 未采信行：不校验、不读取其缓存值。
    return answers


def load_round(
    facts_dir: str | Path, questions: tuple[str, ...]
) -> tuple[str, dict[str, dict[str, Any]]]:
    """读取一轮事实目录，返回（版本, 提交 ID -> 采信作答表）。

    只读目录一级的 ``*.facts.json``；按文件名排序处理，首个错误即中止。
    """
    dir_path = Path(facts_dir)
    if not dir_path.is_dir():
        raise CopyAuditError(
            "DIR_INVALID", f"事实目录不存在或不是目录: {facts_dir}"
        )
    files = sorted(
        (
            p
            for p in dir_path.iterdir()
            if p.is_file() and p.name.endswith(".facts.json")
        ),
        key=lambda p: p.name,
    )
    if not files:
        raise CopyAuditError(
            "NO_FACTS_FILES",
            "目录一级没有任何 *.facts.json 事实文件",
            str(dir_path),
        )
    version: str | None = None
    submissions: dict[str, dict[str, Any]] = {}
    first_seen_file: dict[str, str] = {}
    for path in files:
        submission_id, file_version, facts = _read_facts_file(path)
        if version is None:
            version = file_version
        elif file_version != version:
            raise CopyAuditError(
                "VERSION_MISMATCH",
                f"版本 {file_version!r} 与本轮版本 {version!r} 不一致；"
                f"输入必须是同一版本",
                str(path),
            )
        if submission_id in submissions:
            raise CopyAuditError(
                "DUPLICATE_SUBMISSION",
                f"提交 ID {submission_id!r} 重复"
                f"（首次出现于 {first_seen_file[submission_id]}）",
                str(path),
            )
        submissions[submission_id] = _extract_answers(
            path, submission_id, file_version, facts, questions
        )
        first_seen_file[submission_id] = path.name
    assert version is not None  # files 非空时必然已赋值
    return version, submissions


# ---------------------------------------------------------------------------
# 候选对与直接线索
# ---------------------------------------------------------------------------


def _typed_key(value: Any) -> tuple[str, Any]:
    """与 engine.strict_equal 对齐的类型化键：bool/int/float/str 互不混同。"""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", value)
    return ("str", value)


def find_candidate_pairs(
    submissions: dict[str, dict[str, Any]], questions: tuple[str, ...]
) -> set[tuple[str, str]]:
    """找出全部需要逐对核验的答卷对（(小ID, 大ID)）。

    直接线索要求至少 MIN_SAME 题同答案，故合格的一对必然在某个
    MIN_SAME 题子集上取值完全一致；按「题子集 × 类型化取值」分桶，
    同桶即候选。不重不漏，且绝不逐对枚举全部答卷。
    """
    candidates: set[tuple[str, str]] = set()
    for subset in combinations(sorted(questions), MIN_SAME):
        buckets: dict[tuple[tuple[str, Any], ...], list[str]] = {}
        for sid, answers in submissions.items():
            key: list[tuple[str, Any]] = []
            for qpath in subset:
                value = answers.get(qpath, _MISSING)
                if value is _MISSING:
                    break
                key.append(_typed_key(value))
            else:
                buckets.setdefault(tuple(key), []).append(sid)
        for bucket in buckets.values():
            size = len(bucket)
            for i in range(size - 1):
                left = bucket[i]
                for j in range(i + 1, size):
                    right = bucket[j]
                    candidates.add(
                        (left, right) if left < right else (right, left)
                    )
    return candidates


def find_clues(
    submissions: dict[str, dict[str, Any]],
    questions: tuple[str, ...],
    candidates: set[tuple[str, str]],
) -> list[DirectClue]:
    """对候选对逐对核验：同答案 >= MIN_SAME 且不同答案 <= MAX_DIFFERENT。"""
    clues: list[DirectClue] = []
    ordered = sorted(questions)
    for left, right in sorted(candidates):
        answers_left = submissions[left]
        answers_right = submissions[right]
        same: list[str] = []
        different: list[str] = []
        for qpath in ordered:
            value_left = answers_left.get(qpath, _MISSING)
            value_right = answers_right.get(qpath, _MISSING)
            if value_left is _MISSING or value_right is _MISSING:
                continue  # 一侧或两侧缺项：不计为相同或不同
            if strict_equal(value_left, value_right):
                same.append(qpath)
            else:
                different.append(qpath)
        if len(same) >= MIN_SAME and len(different) <= MAX_DIFFERENT:
            clues.append(
                DirectClue((left, right), tuple(same), tuple(different))
            )
    return clues


# ---------------------------------------------------------------------------
# 聚簇与清单
# ---------------------------------------------------------------------------


def build_listing(
    version: str,
    questions: tuple[str, ...],
    submission_count: int,
    clues: list[DirectClue],
) -> dict[str, Any]:
    """把直接线索聚成簇并渲染为确定性清单（不复制任何答案值）。"""
    adjacency: dict[str, set[str]] = {}
    for clue in clues:
        left, right = clue.pair
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)

    # 连通分量：能沿直接线索互相到达的答卷归为同一簇；孤立答卷不出现。
    cluster_members: list[list[str]] = []
    member_cluster: dict[str, int] = {}
    seen: set[str] = set()
    for sid in sorted(adjacency):
        if sid in seen:
            continue
        stack = [sid]
        seen.add(sid)
        members: list[str] = []
        while stack:
            current = stack.pop()
            members.append(current)
            for neighbor in adjacency[current]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        members.sort()
        for member in members:
            member_cluster[member] = len(cluster_members)
        cluster_members.append(members)

    cluster_clues: list[list[DirectClue]] = [
        [] for _ in cluster_members
    ]
    for clue in clues:
        cluster_clues[member_cluster[clue.pair[0]]].append(clue)

    order = sorted(
        range(len(cluster_members)), key=lambda i: cluster_members[i]
    )
    clusters = []
    for index in order:
        entries = sorted(cluster_clues[index], key=lambda c: c.pair)
        clusters.append(
            {
                "members": cluster_members[index],
                "clues": [
                    {
                        "pair": [clue.pair[0], clue.pair[1]],
                        "same_questions": list(clue.same),
                        "different_questions": list(clue.different),
                    }
                    for clue in entries
                ],
            }
        )
    return {
        "version": version,
        "question_paths": sorted(questions),
        "submission_count": submission_count,
        "cluster_count": len(clusters),
        "clue_count": len(clues),
        "clusters": clusters,
    }


def write_listing_atomic(out_path: str | Path, payload: dict[str, Any]) -> None:
    """原子写出清单：先写同目录临时文件再替换；失败时已有清单不变。"""
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


def run_copy_audit(
    facts_dir: str | Path,
    questions: tuple[str, ...] | list[str],
    out_path: str | Path,
) -> AuditStats:
    """完整执行一轮复填核查：读取 -> 候选 -> 核验 -> 聚簇 -> 原子写清单。"""
    validated = validate_question_args(list(questions))
    version, submissions = load_round(facts_dir, validated)
    candidates = find_candidate_pairs(submissions, validated)
    clues = find_clues(submissions, validated, candidates)
    payload = build_listing(version, validated, len(submissions), clues)
    write_listing_atomic(out_path, payload)
    return AuditStats(
        submission_count=len(submissions),
        candidate_pair_count=len(candidates),
        clue_count=len(clues),
        cluster_count=payload["cluster_count"],
    )
