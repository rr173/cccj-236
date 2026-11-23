"""双波次面板转移库（一个库负责一项指标）。

把调查组交来的**波次事实**（``one``/``batch`` 产出的事实 JSON）与**成员对照**
按修订水位入仓，维护两张可直接查询的结果：

  * ``panel_member``：以「家庭 ID + 稳定成员 ID」唯一标识的双波次转移明细；
  * ``panel_summary``：按两端状态组成的转移格汇总的人数（不保留零人数格）。

设计约束（对应需求）：
  * 映射（问卷版本 -> 目标选择题静态路径 + 回答代码 -> 统一类别）在建库时固定，
    之后带入不同映射直接报错；代码按 JSON 类型识别，整数 ``1`` 与字符串 ``"1"``
    是两个不同代码，类别必须是非空字符串。
  * 成员身份只来自成员对照（instance_path -> 稳定成员 ID），绝不从路径推导；
    目标题事实行与对照一一对应，允许两者同时为空。
  * 仅 ``visibility=true`` 且 ``answer_status=answered`` 的行可按代码换算类别；
    已采信但无代码映射为「未映射」；未展示 / 展示未定 / 展示但无有效作答各有
    独立原因状态，这些行里残留的缓存值一律不读。
  * 每个家庭每波独立以最高**已接受**修订为准；撤回后水位保留，低修订不复活，
    同修订同内容幂等、同修订异内容冲突；内容比较基于解析后的 JSON（忽略空白
    与对象键顺序），与文件名无关。
  * 常规入库只重建本批涉及家庭的成员与汇总格，绝不扫描其他家庭；水位、成员
    转移、人数在同一 SQLite 事务内生效，批内任一输入无效则整批回滚。

仅使用 Python 标准库，以 ``python -I`` 运行。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 领域常量
# ---------------------------------------------------------------------------

WAVE_FIRST = "first"  # 首访
WAVE_SECOND = "second"  # 回访
WAVES = (WAVE_FIRST, WAVE_SECOND)

OP_REPLACE = "replace"  # 替换：给出整波事实 JSON + 成员对照
OP_WITHDRAW = "withdraw"  # 撤回：不带事实或对照

# 明细行的状态种类（state_kind）：统一类别 vs 原因状态，分开标识。
KIND_CATEGORY = "category"  # 成功按代码换算出统一类别
KIND_REASON = "reason"  # 未展示 / 未定 / 无有效作答 / 未映射
KIND_NO_OBSERVATION = "no_observation"  # 该成员只在另一波出现

# 原因状态（state_value，state_kind='reason' 时使用）。
REASON_NOT_PRESENTED = "not_presented"  # visibility=false：根本没问
REASON_UNDERTERMINED = "undetermined"  # visibility=unknown：展示与否未定
REASON_PRESENTED_UNANSWERED = "presented_unanswered"  # 展示但无有效作答
REASON_UNMAPPED = "unmapped"  # 已采信作答，但代码在该版本映射中不存在

# visibility=false 对应的两种 answer_status 都属于「未展示」。
_NOT_PRESENTED_STATUSES = frozenset(("not_asked", "not_asked_cached"))
# visibility=unknown 对应的两种 answer_status 都属于「展示未定」。
_UNDERTERMINED_STATUSES = frozenset(("undetermined", "undetermined_cached"))
# visibility=true 但没有有效作答的 answer_status（缓存值不得采信）。
_PRESENTED_UNANSWERED_STATUSES = frozenset(
    ("displayed_unanswered", "illegal_cached_value")
)

_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class PanelError(Exception):
    """面板库层面的可预期错误（坏输入、映射不一致、修订冲突等）。"""


class MappingConflict(PanelError):
    """建库映射已固定，本次调用带入了不同映射。"""


class BatchRejected(PanelError):
    """整批被拒绝：聚合批内全部问题，绝不留下部分改动。"""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(errors))


# ---------------------------------------------------------------------------
# 映射
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionMapping:
    """一个问卷版本内目标选择题的定位与代码换算规则。"""

    version: str
    target_static_path: str
    # 代码按解析后的 JSON 值保存（类型敏感）：整数 1 与字符串 "1" 不同键。
    code_to_category: dict[Any, str]


@dataclass(frozen=True)
class PanelMapping:
    """建库映射：指标名 + 各问卷版本的目标题规则。"""

    indicator: str
    versions: dict[str, VersionMapping]
    fingerprint: str  # 规范化 JSON 指纹，用于固定后比对


def _canonical_json(value: Any) -> str:
    """规范化 JSON 文本：sort_keys 消除键顺序影响，空白不参与比较。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def load_mapping(mapping_path: str | Path) -> PanelMapping:
    """从 JSON 文件读取并校验建库映射。

    结构::

        {
          "indicator": "指标名（非空字符串）",
          "versions": [
            {"version": "v3",
             "target_static_path": "grp.q",
             "codes": [{"code": 1, "category": "cat_a"},
                       {"code": "1", "category": "cat_b"}]}
          ]
        }
    """
    with open(mapping_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return build_mapping(raw)


def build_mapping(raw: Any) -> PanelMapping:
    """校验内存中的映射结构并构造 :class:`PanelMapping`。"""
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise PanelError("MAPPING_SHAPE: 映射必须是 JSON 对象")

    indicator = raw.get("indicator")
    if not _is_nonempty_str(indicator):
        errors.append("MAPPING_INDICATOR: indicator 必须是非空字符串")

    versions_raw = raw.get("versions")
    if not isinstance(versions_raw, list) or not versions_raw:
        errors.append("MAPPING_VERSIONS: versions 必须是非空数组")
        versions_raw = []

    versions: dict[str, VersionMapping] = {}
    for index, item in enumerate(versions_raw):
        prefix = f"versions[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: 版本映射必须是对象")
            continue
        version = item.get("version")
        if not _is_nonempty_str(version):
            errors.append(f"{prefix}.version: 必须是非空字符串")
            continue
        if version in versions:
            errors.append(f"{prefix}.version: 版本 {version!r} 重复")
            continue
        target = item.get("target_static_path")
        if not _is_nonempty_str(target):
            errors.append(f"{prefix}.target_static_path: 必须是非空字符串")
        codes_raw = item.get("codes")
        if not isinstance(codes_raw, list) or not codes_raw:
            errors.append(f"{prefix}.codes: 必须是非空数组")
            continue
        code_to_category: dict[Any, str] = {}
        for code_index, code_item in enumerate(codes_raw):
            cprefix = f"{prefix}.codes[{code_index}]"
            if not isinstance(code_item, dict):
                errors.append(f"{cprefix}: 代码映射必须是对象")
                continue
            if "code" not in code_item:
                errors.append(f"{cprefix}.code: 缺失")
                continue
            code = code_item["code"]
            # 仅接受 JSON 标量代码；容器类型没有稳定的映射语义。
            if code is None or isinstance(code, (dict, list)):
                errors.append(f"{cprefix}.code: 代码必须是 JSON 标量")
                continue
            category = code_item.get("category")
            if not _is_nonempty_str(category):
                errors.append(f"{cprefix}.category: 必须是非空字符串")
                continue
            # 类型敏感：1 与 "1" 是两个代码；同值同类型重复才算重复。
            code_key = _TypedCode(code)
            for existing in code_to_category:
                if existing == code_key:
                    errors.append(
                        f"{cprefix}.code: 代码 {code!r}（类型 {type(code).__name__}）重复"
                    )
                    break
            else:
                code_to_category[code_key] = category
        if _is_nonempty_str(version) and _is_nonempty_str(target) and code_to_category:
            versions[version] = VersionMapping(
                version=version,
                target_static_path=target,
                code_to_category=dict(code_to_category),
            )

    if errors:
        raise PanelError("; ".join(errors))

    # 指纹基于规范化后的结构，忽略输入 JSON 的空白与键顺序。
    fingerprint = _canonical_json(
        {
            "indicator": indicator,
            "versions": [
                {
                    "version": vm.version,
                    "target_static_path": vm.target_static_path,
                    "codes": [
                        {"code": key.value, "category": category}
                        for key, category in sorted(
                            vm.code_to_category.items(),
                            key=lambda kv: _canonical_json(kv[0].value),
                        )
                    ],
                }
                for vm in sorted(versions.values(), key=lambda vm: vm.version)
            ],
        }
    )
    return PanelMapping(
        indicator=indicator, versions=versions, fingerprint=fingerprint
    )


@dataclass(frozen=True)
class _TypedCode:
    """包装 JSON 标量，令哈希/相等严格区分类型（1 != "1"，True != 1）。"""

    value: Any

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, _TypedCode):
            return NotImplemented
        left, right = self.value, other.value
        if isinstance(left, bool) or isinstance(right, bool):
            return isinstance(left, bool) and isinstance(right, bool) and left == right
        if type(left) is not type(right):
            return False
        return left == right

    def __hash__(self) -> int:
        return hash((type(self.value).__name__, self.value))


# ---------------------------------------------------------------------------
# 批输入的数据载体（解析 + 校验后）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TargetRow:
    """从事实 JSON 中筛出的一个目标题事实行。"""

    instance_path: str
    visibility: str
    answer_status: str
    answer_value: Any


@dataclass(frozen=True)
class _ParsedWavePayload:
    """一条替换操作解析后的规范化内容（幂等/冲突比较的单位）。"""

    operation: str
    facts: dict[str, Any] | None = None
    crosswalk: list[dict[str, str]] | None = None


@dataclass(frozen=True)
class _ParsedUpdate:
    family_id: str
    wave: str
    revision: int
    operation: str
    payload: _ParsedWavePayload | None  # 撤回为 None


@dataclass
class ApplyReport:
    """一次批处理的结果说明（整批成功时才有）。"""

    batch_index: int
    updates: int
    applied: list[dict[str, Any]] = field(default_factory=list)
    idempotent: list[dict[str, Any]] = field(default_factory=list)
    superseded: list[dict[str, Any]] = field(default_factory=list)
    rebuilt_families: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_index": self.batch_index,
            "updates": self.updates,
            "applied": self.applied,
            "idempotent": self.idempotent,
            "superseded": self.superseded,
            "rebuilt_families": self.rebuilt_families,
        }


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------


def _validate_facts(facts: Any, mapping: PanelMapping, context: str) -> dict[str, Any]:
    if not isinstance(facts, dict):
        raise PanelError(f"{context}: facts 必须是事实 JSON 对象")
    version = facts.get("version")
    if not _is_nonempty_str(version):
        raise PanelError(f"{context}: facts.version 必须是非空字符串")
    if version not in mapping.versions:
        raise PanelError(
            f"{context}: 事实版本 {version!r} 不在建库映射的版本集合 "
            f"{sorted(mapping.versions)!r} 内"
        )
    submission_id = facts.get("submission_id")
    if not _is_nonempty_str(submission_id):
        raise PanelError(f"{context}: facts.submission_id 必须是非空字符串")
    rows = facts.get("facts")
    if not isinstance(rows, list):
        raise PanelError(f"{context}: facts.facts 必须是数组")
    for row_index, row in enumerate(rows):
        rcontext = f"{context}.facts[{row_index}]"
        if not isinstance(row, dict):
            raise PanelError(f"{rcontext}: 事实行必须是对象")
        row_version = row.get("version")
        if row_version != version:
            raise PanelError(
                f"{rcontext}.version: 行版本 {row_version!r} 与顶层版本 {version!r} 不一致"
            )
        row_submission = row.get("submission_id")
        if row_submission != submission_id:
            raise PanelError(
                f"{rcontext}.submission_id: 与顶层 submission_id 不一致"
            )
        if not _is_nonempty_str(row.get("static_path")):
            raise PanelError(f"{rcontext}.static_path: 必须是非空字符串")
        if not isinstance(row.get("instance_path"), str):
            raise PanelError(f"{rcontext}.instance_path: 必须是字符串（根级题为空串）")
        if row.get("visibility") not in ("true", "false", "unknown"):
            raise PanelError(
                f"{rcontext}.visibility: 必须是 'true' | 'false' | 'unknown'"
            )
        if not _is_nonempty_str(row.get("answer_status")):
            raise PanelError(f"{rcontext}.answer_status: 必须是非空字符串")
    return facts


def _extract_target_rows(
    facts: dict[str, Any], target_static_path: str, context: str
) -> dict[str, _TargetRow]:
    """只读取目标静态路径的事实行，其他题一律不入仓。"""
    rows: dict[str, _TargetRow] = {}
    for row_index, row in enumerate(facts["facts"]):
        if row["static_path"] != target_static_path:
            continue
        instance_path = row["instance_path"]
        if instance_path in rows:
            raise PanelError(
                f"{context}.facts[{row_index}]: 目标题 {target_static_path!r} 的 "
                f"instance_path {instance_path!r} 重复"
            )
        rows[instance_path] = _TargetRow(
            instance_path=instance_path,
            visibility=row["visibility"],
            answer_status=row["answer_status"],
            answer_value=row.get("answer_value"),
        )
    return rows


def _validate_crosswalk(
    crosswalk: Any, context: str
) -> list[dict[str, str]]:
    if not isinstance(crosswalk, list):
        raise PanelError(f"{context}: crosswalk 必须是数组")
    normalized: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    seen_members: set[str] = set()
    for index, item in enumerate(crosswalk):
        ccontext = f"{context}.crosswalk[{index}]"
        if not isinstance(item, dict):
            raise PanelError(f"{ccontext}: 对照条目必须是对象")
        instance_path = item.get("instance_path")
        member_id = item.get("member_id")
        if not isinstance(instance_path, str):
            raise PanelError(f"{ccontext}.instance_path: 必须是字符串")
        if not _is_nonempty_str(member_id):
            raise PanelError(f"{ccontext}.member_id: 必须是非空字符串")
        if instance_path in seen_paths:
            raise PanelError(
                f"{ccontext}.instance_path: {instance_path!r} 在对照中重复"
            )
        if member_id in seen_members:
            raise PanelError(
                f"{ccontext}.member_id: 稳定成员 ID {member_id!r} 在对照中重复"
            )
        seen_paths.add(instance_path)
        seen_members.add(member_id)
        normalized.append(
            {"instance_path": instance_path, "member_id": member_id}
        )
    return normalized


def _check_one_to_one(
    target_rows: dict[str, _TargetRow],
    crosswalk: list[dict[str, str]],
    context: str,
) -> None:
    """目标题行与成员对照必须一一对应（允许同时为空）。"""
    target_paths = set(target_rows)
    crosswalk_paths = {item["instance_path"] for item in crosswalk}
    if target_paths == crosswalk_paths:
        return
    only_in_facts = sorted(target_paths - crosswalk_paths)
    only_in_crosswalk = sorted(crosswalk_paths - target_paths)
    details = []
    if only_in_facts:
        details.append(f"事实行有而对照缺失: {only_in_facts}")
    if only_in_crosswalk:
        details.append(f"对照有而事实行缺失: {only_in_crosswalk}")
    raise PanelError(
        f"{context}: 目标题事实行与成员对照不一一对应；" + "；".join(details)
    )


def parse_updates(
    raw_updates: Any, mapping: PanelMapping
) -> list[_ParsedUpdate]:
    """解析并全量校验一批更新；任一无效即抛 :class:`BatchRejected`。

    本函数不触碰数据库，保证整批校验先于任何写入。
    """
    errors: list[str] = []
    if not isinstance(raw_updates, list) or not raw_updates:
        raise BatchRejected(["BATCH_SHAPE: updates 必须是非空数组"])

    parsed: list[_ParsedUpdate] = []
    seen_family_wave: set[tuple[str, str]] = set()
    for index, update in enumerate(raw_updates):
        context = f"updates[{index}]"
        structural_error = False
        if not isinstance(update, dict):
            errors.append(f"{context}: 更新必须是对象")
            continue
        family_id = update.get("family_id")
        if not _is_nonempty_str(family_id):
            errors.append(f"{context}.family_id: 必须是非空字符串")
            structural_error = True
        wave = update.get("wave")
        if wave not in WAVES:
            errors.append(
                f"{context}.wave: 必须是 {WAVE_FIRST!r} 或 {WAVE_SECOND!r}"
            )
            structural_error = True
        revision = update.get("revision")
        # bool 是 int 子类，显式排除。
        if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
            errors.append(f"{context}.revision: 必须是正整数")
            structural_error = True
        operation = update.get("operation")
        if operation not in (OP_REPLACE, OP_WITHDRAW):
            errors.append(
                f"{context}.operation: 必须是 {OP_REPLACE!r} 或 {OP_WITHDRAW!r}"
            )
            structural_error = True
        if not structural_error:
            key = (family_id, wave)
            if key in seen_family_wave:
                errors.append(
                    f"{context}: 同批出现两个针对家庭 {family_id!r} 波次 {wave!r} 的操作"
                )
                structural_error = True
            seen_family_wave.add(key)

        # 结构字段已出错时不再解析载荷，避免连锁误报。
        if structural_error:
            continue

        payload: _ParsedWavePayload | None = None
        if operation == OP_REPLACE:
            if "facts" not in update or "crosswalk" not in update:
                errors.append(
                    f"{context}: 替换操作必须同时携带 facts 与 crosswalk"
                )
            else:
                try:
                    facts = _validate_facts(update["facts"], mapping, context)
                    crosswalk = _validate_crosswalk(update["crosswalk"], context)
                    target_rows = _extract_target_rows(
                        facts,
                        mapping.versions[facts["version"]].target_static_path,
                        context,
                    )
                    _check_one_to_one(target_rows, crosswalk, context)
                except PanelError as exc:
                    errors.append(str(exc))
                    continue
                payload = _ParsedWavePayload(
                    operation=OP_REPLACE,
                    facts=facts,
                    crosswalk=crosswalk,
                )
        else:  # withdraw
            if "facts" in update or "crosswalk" in update:
                errors.append(
                    f"{context}: 撤回操作不得携带 facts 或 crosswalk"
                )
                continue
            payload = _ParsedWavePayload(operation=OP_WITHDRAW)

        if family_id and wave and isinstance(revision, int) and revision > 0:
            parsed.append(
                _ParsedUpdate(
                    family_id=family_id,
                    wave=wave,
                    revision=revision,
                    operation=operation,
                    payload=payload,
                )
            )

    if errors:
        raise BatchRejected(errors)
    return parsed


# ---------------------------------------------------------------------------
# 状态裁决：一个目标题事实行 -> (state_kind, state_value)
# ---------------------------------------------------------------------------


def classify_row(row: _TargetRow, version_mapping: VersionMapping) -> tuple[str, str]:
    """把单个目标行裁决为明细状态。

    只有 visibility=true + answered 的行可读 ``answer_value`` 并按代码换算；
    其余情况下缓存值可能残留但一律不读。
    """
    if row.visibility == "true":
        if row.answer_status == "answered":
            code_key = _TypedCode(row.answer_value)
            category = version_mapping.code_to_category.get(code_key)
            if category is None:
                return KIND_REASON, REASON_UNMAPPED
            return KIND_CATEGORY, category
        if row.answer_status in _PRESENTED_UNANSWERED_STATUSES:
            return KIND_REASON, REASON_PRESENTED_UNANSWERED
        raise PanelError(
            f"STATE_UNEXPECTED: visibility=true 搭配未知 answer_status "
            f"{row.answer_status!r}"
        )
    if row.visibility == "false":
        if row.answer_status in _NOT_PRESENTED_STATUSES:
            return KIND_REASON, REASON_NOT_PRESENTED
        raise PanelError(
            f"STATE_UNEXPECTED: visibility=false 搭配未知 answer_status "
            f"{row.answer_status!r}"
        )
    # visibility == "unknown"
    if row.answer_status in _UNDERTERMINED_STATUSES:
        return KIND_REASON, REASON_UNDERTERMINED
    raise PanelError(
        f"STATE_UNEXPECTED: visibility=unknown 搭配未知 answer_status "
        f"{row.answer_status!r}"
    )


# ---------------------------------------------------------------------------
# SQLite 库
# ---------------------------------------------------------------------------


_DDL = """
CREATE TABLE panel_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE panel_version_mapping (
    version            TEXT NOT NULL,
    target_static_path TEXT NOT NULL,
    code_json          TEXT NOT NULL,
    category           TEXT NOT NULL,
    PRIMARY KEY (version, code_json)
);

-- 每家庭每波的修订水位（撤回后仍保留）。
CREATE TABLE panel_watermark (
    family_id        TEXT NOT NULL,
    wave             TEXT NOT NULL CHECK (wave IN ('first', 'second')),
    revision         INTEGER NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('accepted', 'withdrawn')),
    content_hash     TEXT,
    PRIMARY KEY (family_id, wave)
);

-- 已采信的波次级状态（撤回即删行；水位保留在 panel_watermark）。
CREATE TABLE panel_wave_state (
    family_id      TEXT NOT NULL,
    wave           TEXT NOT NULL,
    member_id      TEXT NOT NULL,
    state_kind     TEXT NOT NULL,
    state_value    TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    submission_id  TEXT,
    version        TEXT,
    static_path    TEXT,
    instance_path  TEXT,
    PRIMARY KEY (family_id, wave, member_id)
);

-- 双波次转移明细：家庭 + 稳定成员唯一。
CREATE TABLE panel_member (
    family_id                TEXT NOT NULL,
    member_id                TEXT NOT NULL,
    first_state_kind         TEXT NOT NULL,
    first_state_value        TEXT NOT NULL,
    second_state_kind        TEXT NOT NULL,
    second_state_value       TEXT NOT NULL,
    first_revision           INTEGER,
    second_revision          INTEGER,
    first_submission_id      TEXT,
    first_version            TEXT,
    first_static_path        TEXT,
    first_instance_path      TEXT,
    second_submission_id     TEXT,
    second_version           TEXT,
    second_static_path       TEXT,
    second_instance_path     TEXT,
    PRIMARY KEY (family_id, member_id)
);

-- 转移格人数（只保留人数 > 0 的格子）。
CREATE TABLE panel_summary (
    first_state_kind   TEXT NOT NULL,
    first_state_value  TEXT NOT NULL,
    second_state_kind  TEXT NOT NULL,
    second_state_value TEXT NOT NULL,
    person_count       INTEGER NOT NULL CHECK (person_count > 0),
    PRIMARY KEY (first_state_kind, first_state_value,
                 second_state_kind, second_state_value)
);
"""


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize_panel(
    db_path: str | Path, mapping: PanelMapping, *, overwrite: bool = False
) -> None:
    """按映射建库。映射一经固定；再次以不同映射建库/打开即报错。"""
    path = Path(db_path)
    if path.exists():
        if not overwrite:
            raise PanelError(
                f"DB_EXISTS: {path} 已存在；如需覆盖请显式 overwrite=True"
            )
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = _connect(path)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.executescript(_DDL)
        with conn:
            conn.execute(
                "INSERT INTO panel_meta(key, value) VALUES ('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT INTO panel_meta(key, value) VALUES ('indicator', ?)",
                (mapping.indicator,),
            )
            conn.execute(
                "INSERT INTO panel_meta(key, value) VALUES ('mapping_fingerprint', ?)",
                (mapping.fingerprint,),
            )
            for vm in mapping.versions.values():
                for code_key, category in vm.code_to_category.items():
                    conn.execute(
                        "INSERT INTO panel_version_mapping"
                        "(version, target_static_path, code_json, category) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            vm.version,
                            vm.target_static_path,
                            _canonical_json(code_key.value),
                            category,
                        ),
                    )
    finally:
        conn.close()


def open_panel(db_path: str | Path, mapping: PanelMapping) -> sqlite3.Connection:
    """打开已建库并校验映射指纹；不一致直接报错，不做任何写入。"""
    path = Path(db_path)
    if not path.exists():
        raise PanelError(f"DB_MISSING: {path} 不存在，请先 initialize_panel")
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT value FROM panel_meta WHERE key = 'mapping_fingerprint'"
        ).fetchone()
        if row is None:
            raise PanelError("DB_CORRUPT: 缺少 mapping_fingerprint")
        if row[0] != mapping.fingerprint:
            raise MappingConflict(
                "MAPPING_CONFLICT: 带入的映射与建库映射不一致"
                "（一个库只负责一项指标，映射建库后固定）"
            )
        schema = conn.execute(
            "SELECT value FROM panel_meta WHERE key = 'schema_version'"
        ).fetchone()
        if schema is None or int(schema[0]) != _SCHEMA_VERSION:
            raise PanelError("DB_SCHEMA: 不支持的库结构版本")
    except Exception:
        conn.close()
        raise
    return conn


# ---------------------------------------------------------------------------
# 批处理
# ---------------------------------------------------------------------------


def _content_hash(payload: _ParsedWavePayload) -> str:
    if payload.operation == OP_WITHDRAW:
        return _canonical_json({"operation": OP_WITHDRAW})
    return _canonical_json(
        {
            "operation": OP_REPLACE,
            "facts": payload.facts,
            "crosswalk": payload.crosswalk,
        }
    )


def _build_member_states(
    facts: dict[str, Any],
    crosswalk: list[dict[str, str]],
    mapping: PanelMapping,
    revision: int,
) -> list[dict[str, Any]]:
    """把一条替换载荷裁决成逐成员的波次状态行。"""
    version = facts["version"]
    version_mapping = mapping.versions[version]
    target_rows = _extract_target_rows(
        facts, version_mapping.target_static_path, context="<accepted>"
    )
    result: list[dict[str, Any]] = []
    for item in crosswalk:
        instance_path = item["instance_path"]
        member_id = item["member_id"]
        target = target_rows[instance_path]
        state_kind, state_value = classify_row(target, version_mapping)
        result.append(
            {
                "member_id": member_id,
                "state_kind": state_kind,
                "state_value": state_value,
                "revision": revision,
                # 有事实的一端可回指完整来源。
                "submission_id": facts["submission_id"],
                "version": version,
                "static_path": version_mapping.target_static_path,
                "instance_path": instance_path,
            }
        )
    return result


def _rebuild_family(conn: sqlite3.Connection, family_id: str) -> None:
    """按当前两波已采信状态重建该家庭的明细与受影响的汇总格。

    只触碰本家庭：先算出**其他家庭**在受影响格子上的保留人数，删除这些格子，
    再把保留人数与本家庭的新贡献一起写回。其他家庭的明细全程不扫描、不改动，
    其汇总贡献按格子聚合值原样保留。
    """
    # 1) 找出该家庭当前贡献过的转移格，以及其他家庭在这些格子上的保留人数。
    affected_cells = conn.execute(
        "SELECT DISTINCT first_state_kind, first_state_value, "
        "second_state_kind, second_state_value "
        "FROM panel_member WHERE family_id = ?",
        (family_id,),
    ).fetchall()
    retained: dict[tuple[str, str, str, str], int] = {}
    for kind1, val1, kind2, val2 in affected_cells:
        row = conn.execute(
            "SELECT COUNT(*) FROM panel_member "
            "WHERE family_id <> ? AND first_state_kind=? AND first_state_value=? "
            "AND second_state_kind=? AND second_state_value=?",
            (family_id, kind1, val1, kind2, val2),
        ).fetchone()
        retained[(kind1, val1, kind2, val2)] = row[0]
        conn.execute(
            "DELETE FROM panel_summary "
            "WHERE first_state_kind=? AND first_state_value=? "
            "AND second_state_kind=? AND second_state_value=?",
            (kind1, val1, kind2, val2),
        )
    for cell, count in retained.items():
        if count > 0:  # 不保留零人数格
            conn.execute(
                "INSERT INTO panel_summary(first_state_kind, first_state_value, "
                "second_state_kind, second_state_value, person_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (*cell, count),
            )
    conn.execute("DELETE FROM panel_member WHERE family_id = ?", (family_id,))

    # 2) 取两波状态，成员并集（只在一波出现的成员另一端为无观测）。
    wave_rows: dict[str, dict[str, dict[str, Any]]] = {
        WAVE_FIRST: {},
        WAVE_SECOND: {},
    }
    for wave in WAVES:
        for row in conn.execute(
            "SELECT member_id, state_kind, state_value, revision, "
            "submission_id, version, static_path, instance_path "
            "FROM panel_wave_state WHERE family_id=? AND wave=?",
            (family_id, wave),
        ):
            wave_rows[wave][row[0]] = {
                "state_kind": row[1],
                "state_value": row[2],
                "revision": row[3],
                "submission_id": row[4],
                "version": row[5],
                "static_path": row[6],
                "instance_path": row[7],
            }

    all_members = sorted(set(wave_rows[WAVE_FIRST]) | set(wave_rows[WAVE_SECOND]))
    pending_summary: dict[tuple[str, str, str, str], int] = {}
    for member_id in all_members:
        ends: dict[str, dict[str, Any]] = {}
        for wave in WAVES:
            ends[wave] = wave_rows[wave].get(member_id)

        def columns(wave: str) -> list[Any]:
            state = ends[wave]
            if state is None:
                # 无观测：状态列为固定标记，来源与修订列才允许为空。
                return [
                    KIND_NO_OBSERVATION,
                    KIND_NO_OBSERVATION,
                    None,
                    None,
                    None,
                    None,
                    None,
                ]
            return [
                state["state_kind"],
                state["state_value"],
                state["revision"],
                state["submission_id"],
                state["version"],
                state["static_path"],
                state["instance_path"],
            ]

        first_cols = columns(WAVE_FIRST)
        second_cols = columns(WAVE_SECOND)
        # 参数顺序必须与表定义一致：首波（状态2 + 来源5）、再回访（状态2 + 来源5）。
        params = [
            family_id,
            member_id,
            first_cols[0],
            first_cols[1],
            second_cols[0],
            second_cols[1],
            first_cols[2],
            second_cols[2],
            first_cols[3],
            first_cols[4],
            first_cols[5],
            first_cols[6],
            second_cols[3],
            second_cols[4],
            second_cols[5],
            second_cols[6],
        ]
        conn.execute(
            "INSERT INTO panel_member(family_id, member_id, "
            "first_state_kind, first_state_value, "
            "second_state_kind, second_state_value, "
            "first_revision, second_revision, "
            "first_submission_id, first_version, "
            "first_static_path, first_instance_path, "
            "second_submission_id, second_version, "
            "second_static_path, second_instance_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            params,
        )
        cell = (
            first_cols[0],
            first_cols[1],
            second_cols[0],
            second_cols[1],
        )
        pending_summary[cell] = pending_summary.get(cell, 0) + 1

    # 3) 把本家庭的新贡献并入汇总；与其他家庭的保留人数重合时累加。
    for (kind1, val1, kind2, val2), count in pending_summary.items():
        conn.execute(
            "INSERT INTO panel_summary(first_state_kind, first_state_value, "
            "second_state_kind, second_state_value, person_count) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(first_state_kind, first_state_value, "
            "second_state_kind, second_state_value) "
            "DO UPDATE SET person_count = person_count + excluded.person_count",
            (kind1, val1, kind2, val2, count),
        )


def apply_updates(
    conn: sqlite3.Connection,
    mapping: PanelMapping,
    raw_updates: list[dict[str, Any]],
    *,
    batch_index: int = 0,
) -> ApplyReport:
    """把一批更新入仓。整批原子：任一无效或同修订冲突则全部回滚。"""
    parsed = parse_updates(raw_updates, mapping)

    report = ApplyReport(batch_index=batch_index, updates=len(parsed))
    # 事务在 with conn 内提交；任何异常都会回滚，不留改动。
    with conn:
        for update in parsed:
            current = conn.execute(
                "SELECT revision, status, content_hash FROM panel_watermark "
                "WHERE family_id=? AND wave=?",
                (update.family_id, update.wave),
            ).fetchone()
            incoming_hash = _content_hash(update.payload)

            if current is not None:
                current_revision, current_status, current_hash = current
                if update.revision < current_revision:
                    # 更低修订不改变状态（撤回的旧内容也不能借此复活）。
                    report.superseded.append(
                        {
                            "family_id": update.family_id,
                            "wave": update.wave,
                            "revision": update.revision,
                            "watermark_revision": current_revision,
                        }
                    )
                    continue
                if update.revision == current_revision:
                    if incoming_hash == current_hash:
                        # 相同修订、相同内容：幂等重送，不重复计算。
                        report.idempotent.append(
                            {
                                "family_id": update.family_id,
                                "wave": update.wave,
                                "revision": update.revision,
                            }
                        )
                        continue
                    # 相同修订、不同内容：冲突，整批拒绝。
                    raise BatchRejected(
                        [
                            f"REVISION_CONFLICT: 家庭 {update.family_id!r} 波次 "
                            f"{update.wave!r} 修订号 {update.revision} 已接受过不同内容"
                        ]
                    )

            # 更高修订：接受并落水位。
            watermark_status = (
                "withdrawn"
                if update.operation == OP_WITHDRAW
                else "accepted"
            )
            conn.execute(
                "INSERT INTO panel_watermark(family_id, wave, revision, status, "
                "content_hash) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(family_id, wave) DO UPDATE SET "
                "revision=excluded.revision, status=excluded.status, "
                "content_hash=excluded.content_hash",
                (
                    update.family_id,
                    update.wave,
                    update.revision,
                    watermark_status,
                    incoming_hash,
                ),
            )
            # 重写该家庭该波的已采信状态。
            conn.execute(
                "DELETE FROM panel_wave_state WHERE family_id=? AND wave=?",
                (update.family_id, update.wave),
            )
            if update.operation == OP_REPLACE:
                member_states = _build_member_states(
                    update.payload.facts,
                    update.payload.crosswalk,
                    mapping,
                    update.revision,
                )
                for state in member_states:
                    conn.execute(
                        "INSERT INTO panel_wave_state(family_id, wave, member_id, "
                        "state_kind, state_value, revision, submission_id, version, "
                        "static_path, instance_path) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            update.family_id,
                            update.wave,
                            state["member_id"],
                            state["state_kind"],
                            state["state_value"],
                            state["revision"],
                            state["submission_id"],
                            state["version"],
                            state["static_path"],
                            state["instance_path"],
                        ),
                    )
            # 撤回时仅删状态（上面已删），水位保留 withdrawn。
            report.applied.append(
                {
                    "family_id": update.family_id,
                    "wave": update.wave,
                    "revision": update.revision,
                    "operation": update.operation,
                }
            )

        # 仅重建本批涉及家庭，且每个家庭只重建一次（幂等/低修订条目不产生
        # 新状态，重复重建会把同一家庭的汇总贡献重复计入）。
        affected: list[str] = []
        seen_families: set[str] = set()
        for item in report.applied:
            if item["family_id"] not in seen_families:
                seen_families.add(item["family_id"])
                affected.append(item["family_id"])
        for family_id in affected:
            _rebuild_family(conn, family_id)
        report.rebuilt_families = affected
    return report


def load_updates_file(path: str | Path) -> Any:
    """读取一批更新的 JSON 文件（文件本身不可信，校验在 parse_updates 完成）。"""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
