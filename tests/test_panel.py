"""面板库测试：映射固定、逐人转移、修订水位、撤回、整批原子与家庭隔离。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pipeline.panel import (
    KIND_CATEGORY,
    KIND_NO_OBSERVATION,
    KIND_REASON,
    OP_REPLACE,
    OP_WITHDRAW,
    REASON_NOT_PRESENTED,
    REASON_PRESENTED_UNANSWERED,
    REASON_UNDERTERMINED,
    REASON_UNMAPPED,
    WAVE_FIRST,
    WAVE_SECOND,
    BatchRejected,
    MappingConflict,
    PanelError,
    _TargetRow,
    _TypedCode,
    apply_updates,
    build_mapping,
    classify_row,
    initialize_panel,
    open_panel,
)

# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def make_mapping() -> dict[str, Any]:
    """两个版本：同一代码 1 在两版表示不同类别；并刻意区分 1 与 "1"。"""
    return {
        "indicator": "primary_choice",
        "versions": [
            {
                "version": "v3",
                "target_static_path": "grp_members.q_choice",
                "codes": [
                    {"code": 1, "category": "v3_one"},
                    {"code": 2, "category": "v3_two"},
                    {"code": "1", "category": "v3_string_one"},
                ],
            },
            {
                "version": "v4",
                "target_static_path": "grp_members.q_choice",
                "codes": [
                    {"code": 1, "category": "v4_one"},
                    {"code": 3, "category": "v4_three"},
                ],
            },
        ],
    }


def fact_row(
    instance_path: str,
    *,
    visibility: str = "true",
    answer_status: str = "answered",
    answer_value: Any = 1,
    version: str = "v3",
    submission_id: str = "sub-x",
    static_path: str = "grp_members.q_choice",
) -> dict[str, Any]:
    return {
        "version": version,
        "submission_id": submission_id,
        "static_path": static_path,
        "instance_path": instance_path,
        "visibility": visibility,
        "answer_status": answer_status,
        "answer_value": answer_value,
        "reason": None,
        "list_state": None,
        "evidence": [],
    }


def facts_payload(
    rows: list[dict[str, Any]], *, version: str = "v3", submission_id: str = "sub-x"
) -> dict[str, Any]:
    # 行级 submission_id/version 与顶层保持一致（事实表契约）。
    normalized = []
    for row in rows:
        row = dict(row)
        row["submission_id"] = submission_id
        row["version"] = version
        normalized.append(row)
    return {
        "submission_id": submission_id,
        "version": version,
        "row_count": len(normalized),
        "facts": normalized,
    }


def crosswalk(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [
        {"instance_path": instance_path, "member_id": member_id}
        for instance_path, member_id in pairs
    ]


def update(
    family_id: str,
    wave: str,
    revision: int,
    operation: str = OP_REPLACE,
    *,
    facts: dict[str, Any] | None = None,
    members: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "family_id": family_id,
        "wave": wave,
        "revision": revision,
        "operation": operation,
    }
    if operation == OP_REPLACE:
        item["facts"] = facts
        item["crosswalk"] = members
    return item


class PanelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "panel.db"
        self.mapping = build_mapping(make_mapping())
        initialize_panel(self.db_path, self.mapping)
        self.conn = open_panel(self.db_path, self.mapping)
        self.addCleanup(self.conn.close)

    def apply(self, *updates: dict[str, Any]):
        return apply_updates(self.conn, self.mapping, list(updates))

    def members(self, family_id: str = "F1") -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM panel_member WHERE family_id=? ORDER BY member_id",
            (family_id,),
        ).fetchall()
        return {row["member_id"]: row for row in rows}

    def summary(self) -> dict[tuple[str, str, str, str], int]:
        return {
            (r[0], r[1], r[2], r[3]): r[4]
            for r in self.conn.execute(
                "SELECT first_state_kind, first_state_value, "
                "second_state_kind, second_state_value, person_count "
                "FROM panel_summary"
            )
        }

    def watermark(self, family_id: str, wave: str) -> tuple[int, str]:
        row = self.conn.execute(
            "SELECT revision, status FROM panel_watermark "
            "WHERE family_id=? AND wave=?",
            (family_id, wave),
        ).fetchone()
        return row[0], row[1]


# ---------------------------------------------------------------------------
# 映射
# ---------------------------------------------------------------------------


class MappingTests(unittest.TestCase):
    def test_typed_code_distinguishes_json_types(self):
        self.assertEqual(_TypedCode(1), _TypedCode(1))
        self.assertNotEqual(_TypedCode(1), _TypedCode("1"))
        self.assertNotEqual(_TypedCode(True), _TypedCode(1))
        self.assertEqual(_TypedCode("a"), _TypedCode("a"))
        self.assertEqual(len({_TypedCode(1), _TypedCode("1"), _TypedCode(1)}), 2)

    def test_mapping_requires_nonempty_category_and_scalar_code(self):
        bad = {
            "indicator": "x",
            "versions": [
                {
                    "version": "v",
                    "target_static_path": "g.q",
                    "codes": [{"code": 1, "category": ""}],
                }
            ],
        }
        with self.assertRaises(PanelError) as ctx:
            build_mapping(bad)
        self.assertIn("category", str(ctx.exception))

        bad["versions"][0]["codes"] = [{"code": [1], "category": "ok"}]
        with self.assertRaises(PanelError):
            build_mapping(bad)

    def test_duplicate_typed_code_rejected(self):
        bad = {
            "indicator": "x",
            "versions": [
                {
                    "version": "v",
                    "target_static_path": "g.q",
                    "codes": [
                        {"code": 1, "category": "a"},
                        {"code": 1, "category": "b"},
                    ],
                }
            ],
        }
        with self.assertRaises(PanelError):
            build_mapping(bad)
        # 不同类型的同名代码合法。
        ok = dict(bad)
        ok["versions"] = [
            {
                "version": "v",
                "target_static_path": "g.q",
                "codes": [
                    {"code": 1, "category": "a"},
                    {"code": "1", "category": "b"},
                ],
            }
        ]
        self.assertEqual(
            build_mapping(ok).versions["v"].code_to_category[_TypedCode("1")], "b"
        )

    def test_fingerprint_ignores_key_order_and_whitespace(self):
        a = build_mapping(make_mapping())
        reordered = json.loads(json.dumps(make_mapping(), sort_keys=True))
        b = build_mapping(reordered)
        self.assertEqual(a.fingerprint, b.fingerprint)
        changed = make_mapping()
        changed["versions"][0]["codes"][0]["category"] = "changed"
        self.assertNotEqual(a.fingerprint, build_mapping(changed).fingerprint)

    def test_different_mapping_on_existing_db_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "p.db"
            mapping_a = build_mapping(make_mapping())
            initialize_panel(db, mapping_a)
            changed = make_mapping()
            changed["indicator"] = "other_indicator"
            with self.assertRaises(MappingConflict):
                open_panel(db, build_mapping(changed))
            # 冲突时库未被改动。
            conn = open_panel(db, mapping_a)
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM panel_meta WHERE key='indicator'"
                ).fetchone()[0],
                "primary_choice",
            )
            conn.close()


# ---------------------------------------------------------------------------
# 行状态裁决
# ---------------------------------------------------------------------------


class ClassifyRowTests(PanelTestCase):
    def _vm(self, version: str = "v3"):
        return self.mapping.versions[version]

    @staticmethod
    def _target(row: dict[str, Any]) -> _TargetRow:
        return _TargetRow(
            instance_path=row["instance_path"],
            visibility=row["visibility"],
            answer_status=row["answer_status"],
            answer_value=row["answer_value"],
        )

    def test_answered_maps_by_type_and_version(self):
        row = self._target(fact_row("g#1", answer_value=1))
        self.assertEqual(
            classify_row(row, self._vm("v3")), (KIND_CATEGORY, "v3_one")
        )
        # 同一代码在另一版本映射到不同类别。
        self.assertEqual(
            classify_row(
                self._target(fact_row("g#1", version="v4")), self._vm("v4")
            ),
            (KIND_CATEGORY, "v4_one"),
        )
        # 字符串 "1" 与整数 1 不同。
        self.assertEqual(
            classify_row(
                self._target(fact_row("g#1", answer_value="1")), self._vm("v3")
            ),
            (KIND_CATEGORY, "v3_string_one"),
        )

    def test_answered_but_unmapped_code(self):
        row = self._target(fact_row("g#1", answer_value=99))
        self.assertEqual(
            classify_row(row, self._vm()), (KIND_REASON, REASON_UNMAPPED)
        )

    def test_reason_states_ignore_cached_values(self):
        cases = [
            (
                fact_row("g#1", visibility="false",
                         answer_status="not_asked", answer_value=1),
                REASON_NOT_PRESENTED,
            ),
            (
                fact_row("g#1", visibility="false",
                         answer_status="not_asked_cached", answer_value=1),
                REASON_NOT_PRESENTED,
            ),
            (
                fact_row("g#1", visibility="unknown",
                         answer_status="undetermined", answer_value=None),
                REASON_UNDERTERMINED,
            ),
            (
                fact_row("g#1", visibility="unknown",
                         answer_status="undetermined_cached", answer_value=1),
                REASON_UNDERTERMINED,
            ),
            (
                fact_row("g#1", visibility="true",
                         answer_status="displayed_unanswered", answer_value=None),
                REASON_PRESENTED_UNANSWERED,
            ),
            (
                fact_row("g#1", visibility="true",
                         answer_status="illegal_cached_value", answer_value="fax"),
                REASON_PRESENTED_UNANSWERED,
            ),
        ]
        for row, expected_reason in cases:
            kind, value = classify_row(self._target(row), self._vm())
            self.assertEqual((kind, value), (KIND_REASON, expected_reason))

    def test_unexpected_status_combination_raises(self):
        with self.assertRaises(PanelError):
            classify_row(
                self._target(
                    fact_row("g#1", visibility="true",
                             answer_status="not_asked")
                ),
                self._vm(),
            )


# ---------------------------------------------------------------------------
# 端到端转移语义
# ---------------------------------------------------------------------------


class TransitionTests(PanelTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn.row_factory = sqlite3.Row

    def test_local_position_reused_by_different_people_same_code_diff_category(self):
        # 同一个本地成员位置 g#m1 在两波指向不同的稳定成员。
        first = update(
            "F1", WAVE_FIRST, 1,
            facts=facts_payload(
                [fact_row("g#m1", answer_value=1)],
                submission_id="sub-first",
            ),
            members=crosswalk(("g#m1", "M_ALICE")),
        )
        second = update(
            "F1", WAVE_SECOND, 1,
            facts=facts_payload(
                [fact_row("g#m1", version="v4", answer_value=1)],
                version="v4", submission_id="sub-second",
            ),
            members=crosswalk(("g#m1", "M_BOB")),
        )
        self.apply(first, second)

        members = self.members()
        self.assertEqual(set(members), {"M_ALICE", "M_BOB"})

        alice = members["M_ALICE"]
        self.assertEqual(alice["first_state_value"], "v3_one")
        self.assertEqual(alice["second_state_kind"], KIND_NO_OBSERVATION)
        # 有事实的一端可回指完整来源；无观测一端来源为空。
        self.assertEqual(alice["first_submission_id"], "sub-first")
        self.assertEqual(alice["first_version"], "v3")
        self.assertEqual(alice["first_static_path"], "grp_members.q_choice")
        self.assertEqual(alice["first_instance_path"], "g#m1")
        self.assertIsNone(alice["second_submission_id"])

        bob = members["M_BOB"]
        self.assertEqual(bob["second_state_value"], "v4_one")
        self.assertEqual(bob["first_state_kind"], KIND_NO_OBSERVATION)
        self.assertEqual(bob["second_submission_id"], "sub-second")

        summary = self.summary()
        # 每个明细恰好贡献一人，无零人数格。
        self.assertEqual(sum(summary.values()), 2)
        self.assertEqual(
            summary[(KIND_CATEGORY, "v3_one",
                     KIND_NO_OBSERVATION, KIND_NO_OBSERVATION)],
            1,
        )
        self.assertEqual(
            summary[(KIND_NO_OBSERVATION, KIND_NO_OBSERVATION,
                     KIND_CATEGORY, "v4_one")],
            1,
        )
        for count in summary.values():
            self.assertGreater(count, 0)

    def test_replace_wave_members_leave_and_join_summary_moves_together(self):
        # 初态：首访两人，回访两人（同一批）。
        self.apply(
            update(
                "F1", WAVE_FIRST, 1,
                facts=facts_payload(
                    [
                        fact_row("g#m1", answer_value=1),
                        fact_row("g#m2", answer_value=2),
                    ]
                ),
                members=crosswalk(("g#m1", "M_ALICE"), ("g#m2", "M_CAROL")),
            ),
            update(
                "F1", WAVE_SECOND, 1,
                facts=facts_payload(
                    [
                        fact_row("g#m1", version="v4", answer_value=1),
                        fact_row("g#m2", version="v4", answer_value=3),
                    ],
                    version="v4",
                ),
                members=crosswalk(("g#m1", "M_ALICE"), ("g#m2", "M_CAROL")),
            ),
        )
        self.assertEqual(set(self.members()), {"M_ALICE", "M_CAROL"})
        self.assertEqual(sum(self.summary().values()), 2)
        alice_cell = (
            KIND_CATEGORY, "v3_one", KIND_CATEGORY, "v4_one"
        )
        self.assertEqual(self.summary()[alice_cell], 1)

        # 替换首访：CAROL 退出、DAVE 加入（本地行号 g#m2 被重用到 DAVE）。
        self.apply(
            update(
                "F1", WAVE_FIRST, 2,
                facts=facts_payload(
                    [
                        fact_row("g#m1", answer_value=1),
                        fact_row("g#m2", answer_value=2),
                    ],
                    submission_id="sub-first-r2",
                ),
                members=crosswalk(("g#m1", "M_ALICE"), ("g#m2", "M_DAVE")),
            )
        )
        members = self.members()
        self.assertEqual(set(members), {"M_ALICE", "M_CAROL", "M_DAVE"})
        # CAROL 首访端变成无观测；DAVE 回访端无观测。
        self.assertEqual(members["M_CAROL"]["first_state_kind"],
                         KIND_NO_OBSERVATION)
        self.assertEqual(members["M_DAVE"]["second_state_kind"],
                         KIND_NO_OBSERVATION)
        self.assertEqual(members["M_DAVE"]["first_submission_id"],
                         "sub-first-r2")
        # ALICE 两波均未变，原转移格保留；CAROL/DAVE 形成新格；
        # 总人数与明细一致，且无零人数格。
        summary = self.summary()
        self.assertEqual(summary[alice_cell], 1)
        self.assertEqual(sum(summary.values()), 3)
        self.assertEqual(
            summary[(KIND_CATEGORY, "v3_two",
                     KIND_NO_OBSERVATION, KIND_NO_OBSERVATION)],
            1,
        )
        self.assertEqual(
            summary[(KIND_NO_OBSERVATION, KIND_NO_OBSERVATION,
                     KIND_CATEGORY, "v4_three")],
            1,
        )

    def test_empty_facts_and_empty_crosswalk_allowed_together(self):
        self.apply(
            update(
                "F1", WAVE_FIRST, 1,
                facts=facts_payload([]),
                members=crosswalk(),
            )
        )
        self.assertEqual(self.members(), {})
        self.assertEqual(self.summary(), {})
        # 水位仍然存在。
        self.assertEqual(self.watermark("F1", WAVE_FIRST), (1, "accepted"))

    def test_non_target_questions_are_ignored(self):
        payload = facts_payload(
            [
                fact_row("g#m1", answer_value=1),
                fact_row("g#m1", static_path="grp_members.q_other",
                         answer_value="ignored"),
                fact_row("", static_path="q_consent", answer_value="yes"),
            ]
        )
        self.apply(
            update(
                "F1", WAVE_FIRST, 1,
                facts=payload,
                members=crosswalk(("g#m1", "M_ALICE")),
            )
        )
        self.assertEqual(set(self.members()), {"M_ALICE"})


# ---------------------------------------------------------------------------
# 修订水位 / 撤回 / 幂等 / 冲突
# ---------------------------------------------------------------------------


class RevisionTests(PanelTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn.row_factory = sqlite3.Row
        self.replace_r1 = update(
            "F1", WAVE_FIRST, 1,
            facts=facts_payload([fact_row("g#m1", answer_value=1)]),
            members=crosswalk(("g#m1", "M_ALICE")),
        )

    def test_lower_revision_ignored_and_withdrawn_watermark_kept(self):
        self.apply(self.replace_r1)
        # 撤回整波：状态删除，水位保留。
        self.apply(update("F1", WAVE_FIRST, 2, OP_WITHDRAW))
        self.assertEqual(self.watermark("F1", WAVE_FIRST), (2, "withdrawn"))
        self.assertEqual(self.members(), {})
        wave_state = self.conn.execute(
            "SELECT COUNT(*) FROM panel_wave_state WHERE family_id='F1'"
        ).fetchone()[0]
        self.assertEqual(wave_state, 0)

        # 旧修订重送（无论替换还是撤回）都不能复活旧内容。
        self.apply(self.replace_r1)
        self.assertEqual(self.members(), {})
        self.assertEqual(self.watermark("F1", WAVE_FIRST), (2, "withdrawn"))
        self.apply(update("F1", WAVE_FIRST, 1, OP_WITHDRAW))
        self.assertEqual(self.watermark("F1", WAVE_FIRST), (2, "withdrawn"))

        # 更高修订可以重新建立事实。
        self.apply(
            update(
                "F1", WAVE_FIRST, 3,
                facts=facts_payload(
                    [fact_row("g#m1", answer_value=2)],
                    submission_id="sub-r3",
                ),
                members=crosswalk(("g#m1", "M_ALICE")),
            )
        )
        self.assertEqual(
            self.members()["M_ALICE"]["first_state_value"], "v3_two"
        )
        self.assertEqual(self.watermark("F1", WAVE_FIRST), (3, "accepted"))

    def test_same_revision_same_content_is_idempotent(self):
        self.apply(self.replace_r1)
        report = self.apply(
            json.loads(json.dumps(self.replace_r1))  # 重序列化模拟重送
        )
        self.assertEqual(len(report.idempotent), 1)
        self.assertEqual(len(report.applied), 0)
        # 成员与汇总没有重复计算。
        self.assertEqual(len(self.members()), 1)
        self.assertEqual(sum(self.summary().values()), 1)

        # 键顺序与空白不同但解析内容相同，仍判相同。
        reordered = json.loads(json.dumps(self.replace_r1, sort_keys=True))
        report2 = self.apply(reordered)
        self.assertEqual(len(report2.idempotent), 1)

    def test_withdraw_resend_is_idempotent(self):
        self.apply(update("F1", WAVE_FIRST, 1, OP_WITHDRAW))
        report = self.apply(update("F1", WAVE_FIRST, 1, OP_WITHDRAW))
        self.assertEqual(len(report.idempotent), 1)
        self.assertEqual(self.watermark("F1", WAVE_FIRST), (1, "withdrawn"))

    def test_integer_and_string_one_follow_different_categories_end_to_end(self):
        # 同一事实里整数 1 与字符串 "1" 必须落到不同类别。
        self.apply(
            update(
                "F1", WAVE_FIRST, 1,
                facts=facts_payload(
                    [
                        fact_row("g#m1", answer_value=1),
                        fact_row("g#m2", answer_value="1"),
                    ]
                ),
                members=crosswalk(("g#m1", "M_INT"), ("g#m2", "M_STR")),
            )
        )
        members = self.members()
        self.assertEqual(
            members["M_INT"]["first_state_value"], "v3_one"
        )
        self.assertEqual(
            members["M_STR"]["first_state_value"], "v3_string_one"
        )

    def test_same_revision_different_content_conflicts_and_rolls_back(self):
        self.apply(self.replace_r1)
        conflicting = update(
            "F1", WAVE_FIRST, 1,
            facts=facts_payload(
                [fact_row("g#m1", answer_value=2)]
            ),
            members=crosswalk(("g#m1", "M_ALICE")),
        )
        with self.assertRaises(BatchRejected) as ctx:
            self.apply(conflicting)
        self.assertIn("REVISION_CONFLICT", str(ctx.exception))
        # 旧结果保持不变。
        self.assertEqual(
            self.members()["M_ALICE"]["first_state_value"], "v3_one"
        )
        self.assertEqual(self.watermark("F1", WAVE_FIRST), (1, "accepted"))

    def test_conflict_in_mixed_batch_leaves_prior_results_untouched(self):
        # 先建立家庭 F1 与 F2 的既有结果。
        self.apply(self.replace_r1)
        self.apply(
            update(
                "F2", WAVE_FIRST, 1,
                facts=facts_payload([fact_row("g#m1", answer_value=2)]),
                members=crosswalk(("g#m1", "M_ZOE")),
            )
        )
        before = {
            "members": dict(self.members("F1")),
            "summary": self.summary(),
        }
        # 同批混入：一个合法的新高修订 + 一个同修订冲突。
        mixed = [
            update(
                "F2", WAVE_SECOND, 1,
                facts=facts_payload(
                    [fact_row("g#m1", version="v4", answer_value=3)],
                    version="v4",
                ),
                members=crosswalk(("g#m1", "M_ZOE")),
            ),
            update(
                "F1", WAVE_FIRST, 1,
                facts=facts_payload([fact_row("g#m1", answer_value=2)]),
                members=crosswalk(("g#m1", "M_ALICE")),
            ),
        ]
        with self.assertRaises(BatchRejected):
            self.apply(*mixed)
        # 先前结果保持不变：F2 的回访未写入，F1 未被改动。
        self.assertEqual(set(self.members("F1")), set(before["members"]))
        self.assertEqual(self.summary(), before["summary"])
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM panel_watermark WHERE family_id='F2' AND wave='second'"
            ).fetchone()
        )


# ---------------------------------------------------------------------------
# 输入校验与整批原子
# ---------------------------------------------------------------------------


class ValidationTests(PanelTestCase):
    def _base_replace(self, **overrides: Any) -> dict[str, Any]:
        item = update(
            "F1", WAVE_FIRST, 1,
            facts=facts_payload([fact_row("g#m1", answer_value=1)]),
            members=crosswalk(("g#m1", "M_ALICE")),
        )
        item.update(overrides)
        return item

    def assert_batch_rejected(self, updates: list[dict[str, Any]]) -> list[str]:
        with self.assertRaises(BatchRejected) as ctx:
            self.apply(*updates)
        return ctx.exception.errors

    def test_duplicate_family_wave_in_one_batch_rejected(self):
        errors = self.assert_batch_rejected(
            [self._base_replace(), self._base_replace(revision=2)]
        )
        self.assertTrue(any("同批" in e for e in errors))

    def test_withdraw_carries_no_payload(self):
        bad = update("F1", WAVE_FIRST, 1, OP_WITHDRAW)
        bad["facts"] = facts_payload([])
        errors = self.assert_batch_rejected([bad])
        self.assertTrue(any("撤回" in e for e in errors))

    def test_replace_requires_both_facts_and_crosswalk(self):
        bad = self._base_replace()
        del bad["crosswalk"]
        errors = self.assert_batch_rejected([bad])
        self.assertTrue(any("crosswalk" in e for e in errors))

    def test_crosswalk_and_target_rows_must_match_one_to_one(self):
        # 事实行有、对照缺。
        errors = self.assert_batch_rejected(
            [
                update(
                    "F1", WAVE_FIRST, 1,
                    facts=facts_payload(
                        [fact_row("g#m1"), fact_row("g#m2", answer_value=2)]
                    ),
                    members=crosswalk(("g#m1", "M_ALICE")),
                )
            ]
        )
        self.assertTrue(any("一一对应" in e for e in errors))
        # 对照有、事实行缺。
        errors = self.assert_batch_rejected(
            [
                update(
                    "F1", WAVE_FIRST, 1,
                    facts=facts_payload([fact_row("g#m1")]),
                    members=crosswalk(
                        ("g#m1", "M_ALICE"), ("g#m2", "M_CAROL")
                    ),
                )
            ]
        )
        self.assertTrue(any("一一对应" in e for e in errors))

    def test_duplicate_member_id_in_crosswalk_rejected(self):
        errors = self.assert_batch_rejected(
            [
                update(
                    "F1", WAVE_FIRST, 1,
                    facts=facts_payload(
                        [fact_row("g#m1"), fact_row("g#m2", answer_value=2)]
                    ),
                    members=crosswalk(
                        ("g#m1", "M_ALICE"), ("g#m2", "M_ALICE")
                    ),
                )
            ]
        )
        self.assertTrue(any("member_id" in e for e in errors))

    def test_unknown_version_rejected(self):
        bad = self._base_replace(
            facts=facts_payload(
                [fact_row("g#m1", version="v99")], version="v99"
            )
        )
        errors = self.assert_batch_rejected([bad])
        self.assertTrue(any("v99" in e for e in errors))

    def test_invalid_revision_rejected(self):
        for bad_revision in (0, -1, "1", 1.0, True):
            errors = self.assert_batch_rejected(
                [self._base_replace(revision=bad_revision)]
            )
            self.assertTrue(
                any("revision" in e for e in errors),
                f"revision={bad_revision!r} 应被拒绝",
            )

    def test_invalid_batch_leaves_no_trace(self):
        with self.assertRaises(BatchRejected):
            self.apply(
                self._base_replace(),
                self._base_replace(family_id=""),  # 同批内无效
            )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM panel_watermark").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM panel_member").fetchone()[0], 0
        )

    def test_empty_batch_array_rejected(self):
        with self.assertRaises(BatchRejected):
            apply_updates(self.conn, self.mapping, [])

    def test_root_level_question_uses_empty_instance_path(self):
        root_mapping = build_mapping(
            {
                "indicator": "root_indicator",
                "versions": [
                    {
                        "version": "v3",
                        "target_static_path": "q_root",
                        "codes": [{"code": 1, "category": "root_cat"}],
                    }
                ],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "root.db"
            initialize_panel(db, root_mapping)
            conn = open_panel(db, root_mapping)
            apply_updates(
                conn,
                root_mapping,
                [
                    update(
                        "F1", WAVE_FIRST, 1,
                        facts=facts_payload(
                            [
                                fact_row(
                                    "", static_path="q_root", answer_value=1
                                )
                            ]
                        ),
                        members=crosswalk(("", "M_SOLO")),
                    )
                ],
            )
            row = conn.execute(
                "SELECT first_state_kind, first_state_value, first_instance_path "
                "FROM panel_member"
            ).fetchone()
            self.assertEqual(
                row, ("category", "root_cat", "")
            )
            conn.close()


# ---------------------------------------------------------------------------
# 家庭隔离：常规入库不扫描其他家庭
# ---------------------------------------------------------------------------


class FamilyIsolationTests(PanelTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn.row_factory = sqlite3.Row

    def test_other_families_untouched_when_one_family_changes(self):
        self.apply(
            update(
                "F1", WAVE_FIRST, 1,
                facts=facts_payload([fact_row("g#m1", answer_value=1)]),
                members=crosswalk(("g#m1", "M_ALICE")),
            ),
            update(
                "F2", WAVE_FIRST, 1,
                facts=facts_payload([fact_row("g#m1", answer_value=2)]),
                members=crosswalk(("g#m1", "M_ZOE")),
            ),
        )
        f2_before = self.conn.execute(
            "SELECT * FROM panel_member WHERE family_id='F2'"
        ).fetchall()

        # 只更新 F1：把 ALICE 改成与 ZOE 相同的转移格，该格人数应从 1 变 2，
        # 而 F2 的明细行本身保持原样（未被扫描重建）。
        self.apply(
            update(
                "F1", WAVE_FIRST, 2,
                facts=facts_payload([fact_row("g#m1", answer_value=2)]),
                members=crosswalk(("g#m1", "M_ALICE")),
            )
        )
        f2_after = self.conn.execute(
            "SELECT * FROM panel_member WHERE family_id='F2'"
        ).fetchall()
        self.assertEqual(
            [tuple(r) for r in f2_before], [tuple(r) for r in f2_after]
        )
        shared_cell = (
            KIND_CATEGORY, "v3_two",
            KIND_NO_OBSERVATION, KIND_NO_OBSERVATION,
        )
        self.assertEqual(self.summary()[shared_cell], 2)
        # F1 旧格子已清零移除，不残留零人数格。
        self.assertNotIn(
            (KIND_CATEGORY, "v3_one",
             KIND_NO_OBSERVATION, KIND_NO_OBSERVATION),
            self.summary(),
        )
        # 汇总总人数始终等于明细总人数。
        detail_count = self.conn.execute(
            "SELECT COUNT(*) FROM panel_member"
        ).fetchone()[0]
        self.assertEqual(sum(self.summary().values()), detail_count)

    def test_summary_recomputed_consistently_across_repeated_rebuilds(self):
        # 多次重建同一家庭，汇总人数始终与明细人数一致。
        for revision in range(1, 4):
            self.apply(
                update(
                    "F1", WAVE_FIRST, revision,
                    facts=facts_payload([fact_row("g#m1", answer_value=1)]),
                    members=crosswalk(("g#m1", "M_ALICE")),
                )
            )
            detail_count = self.conn.execute(
                "SELECT COUNT(*) FROM panel_member"
            ).fetchone()[0]
            summary_count = self.conn.execute(
                "SELECT COALESCE(SUM(person_count), 0) FROM panel_summary"
            ).fetchone()[0]
            self.assertEqual(detail_count, summary_count)
        self.assertEqual(sum(self.summary().values()), 1)


if __name__ == "__main__":
    unittest.main()
