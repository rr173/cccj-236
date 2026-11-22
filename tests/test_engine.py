"""门控三值逻辑、缓存合法性、隐藏/未定语义的测试。"""

from __future__ import annotations

import unittest

from pipeline.engine import (
    ANSWERED,
    DISPLAYED_UNANSWERED,
    ILLEGAL_CACHED,
    NOT_ASKED,
    NOT_ASKED_CACHED,
    UNDETERMINED,
    UNDETERMINED_CACHED,
    evaluate_snapshot,
)
from pipeline.tristate import FALSE, TRUE, UNKNOWN
from tests.helpers import (
    answered,
    conj,
    disj,
    eq,
    exists,
    ilist,
    index,
    inst,
    make_definition,
    make_snapshot,
    neg,
    q,
    root,
)


class GateLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        # q1 选择 1/2；q2 门控 q1==1；q3 门控 q1==2；q4 门控 not answered(q1)
        self.definition = make_definition(
            [
                q("q1", options=[1, 2]),
                q("q2", qtype="text", gate=eq("q1", 1)),
                q("q3", qtype="text", gate=eq("q1", 2)),
                q("q4", qtype="text", gate=neg(answered("q1"))),
            ]
        )

    def test_equality_true_and_false_paths(self):
        snap = make_snapshot(root(answers={"q1": 1, "q2": "x", "q3": "y"}),
                             self.definition)
        rows = index(evaluate_snapshot(self.definition, snap))
        self.assertIs(rows[("q2", "")].visibility, TRUE)
        self.assertIs(rows[("q3", "")].visibility, FALSE)
        # 隐藏题的缓存不算作答。
        self.assertEqual(rows[("q3", "")].answer_status, NOT_ASKED_CACHED)

    def test_missing_value_comparison_is_unknown(self):
        snap = make_snapshot(root(answers={}), self.definition)
        rows = index(evaluate_snapshot(self.definition, snap))
        # q1 展示未答；eq 缺答 -> 未知；not answered -> not false = true。
        self.assertEqual(rows[("q1", "")].answer_status, DISPLAYED_UNANSWERED)
        self.assertIs(rows[("q2", "")].visibility, UNKNOWN)
        self.assertIs(rows[("q3", "")].visibility, UNKNOWN)
        self.assertIs(rows[("q4", "")].visibility, TRUE)
        self.assertEqual(rows[("q2", "")].answer_status, UNDETERMINED)

    def test_illegal_cached_value_ignored_by_gate_but_flagged_when_shown(self):
        # 同一缓存值 99：q1 确实展示 -> 标出非法、无有效回答；
        # 门控读取它时按缺答处理 -> q2 可见性未知，而非真或假。
        snap = make_snapshot(root(answers={"q1": 99, "q2": "z"}), self.definition)
        rows = index(evaluate_snapshot(self.definition, snap))
        self.assertIs(rows[("q1", "")].visibility, TRUE)
        self.assertEqual(rows[("q1", "")].answer_status, ILLEGAL_CACHED)
        self.assertIn("合法值", rows[("q1", "")].reason)
        self.assertIs(rows[("q2", "")].visibility, UNKNOWN)
        self.assertEqual(rows[("q2", "")].answer_status, UNDETERMINED_CACHED)
        # 证据里必须留下“非法值不参与门控”的引用。
        ref_evidence = [e for e in rows[("q2", "")].evidence if e.kind == "ref"]
        self.assertTrue(any(e.status == "illegal" for e in ref_evidence))

    def test_explicit_null_treated_as_missing(self):
        snap = make_snapshot(root(answers={"q1": None}), self.definition)
        rows = index(evaluate_snapshot(self.definition, snap))
        self.assertEqual(rows[("q1", "")].answer_status, DISPLAYED_UNANSWERED)
        self.assertIs(rows[("q2", "")].visibility, UNKNOWN)

    def test_answered_predicate_three_states(self):
        definition = make_definition(
            [
                q("q1", options=["a", "b"]),
                q("q2", qtype="text", gate=answered("q1")),
                q("q3", qtype="text", gate=neg(answered("q1"))),
            ]
        )
        # 展示且已答 -> true
        rows = index(evaluate_snapshot(
            definition, make_snapshot(root(answers={"q1": "a"}), definition)))
        self.assertIs(rows[("q2", "")].visibility, TRUE)
        self.assertIs(rows[("q3", "")].visibility, FALSE)

        # 展示未答 -> false（不是未知）
        rows = index(evaluate_snapshot(
            definition, make_snapshot(root(answers={}), definition)))
        self.assertIs(rows[("q2", "")].visibility, FALSE)
        self.assertIs(rows[("q3", "")].visibility, TRUE)

    def test_kleene_and_or_unknown_propagation(self):
        definition = make_definition(
            [
                q("q1", options=[1]),
                q("q2", qtype="text", gate=conj(eq("q1", 1), eq("q1", 2))),
                q("q3", qtype="text", gate=disj(eq("q1", 1), eq("q1", 2))),
                q("q4", qtype="text", gate=conj(neg(answered("q1")), eq("q1", 1))),
            ]
        )
        rows = index(evaluate_snapshot(
            definition, make_snapshot(root(answers={}), definition)))
        # unknown and false（1!=2 本会假，但缺答是 unknown）-> unknown
        self.assertIs(rows[("q2", "")].visibility, UNKNOWN)
        # unknown or unknown -> unknown
        self.assertIs(rows[("q3", "")].visibility, UNKNOWN)
        # true and unknown -> unknown
        self.assertIs(rows[("q4", "")].visibility, UNKNOWN)


class HiddenReferenceTests(unittest.TestCase):
    def test_gate_reading_hidden_question(self):
        # q1=no 隐藏 q2；q3 用 answered(q2)，q4 用 eq(q2, "x")。
        definition = make_definition(
            [
                q("q1", options=["yes", "no"]),
                q("q2", options=["x"], gate=eq("q1", "yes")),
                q("q3", qtype="text", gate=answered("q2")),
                q("q4", qtype="text", gate=eq("q2", "x")),
            ]
        )
        snap = make_snapshot(
            root(answers={"q1": "no", "q2": "x"}), definition
        )
        rows = index(evaluate_snapshot(definition, snap))
        self.assertIs(rows[("q2", "")].visibility, FALSE)
        # 确定隐藏 -> answered 为假（即使有缓存）。
        self.assertIs(rows[("q3", "")].visibility, FALSE)
        # 隐藏题的缓存不参与等值，缺答比较 -> 未知，绝不判成假或真。
        self.assertIs(rows[("q4", "")].visibility, UNKNOWN)
        ref = next(
            e for e in rows[("q4", "")].evidence
            if e.kind == "ref" and e.static_path == "q2"
        )
        self.assertEqual(ref.status, "hidden_unused")  # 合法但隐藏，未真正采用

    def test_missing_group_list_treated_as_closed_empty(self):
        from tests.helpers import grp

        definition = make_definition(
            [
                grp("grp", [q("inside", options=["yes"])]),
                q("q_after", qtype="text",
                  gate=exists("grp", "grp.inside", "yes")),
            ]
        )
        # 快照根本不带 grp 清单。
        snap = make_snapshot(root(), definition)
        rows = index(evaluate_snapshot(definition, snap))
        self.assertIs(rows[("q_after", "")].visibility, FALSE)
        self.assertEqual(
            [r.static_path for r in evaluate_snapshot(definition, snap)],
            ["q_after"],
        )


class HiddenCachedTests(unittest.TestCase):
    def test_cached_answer_on_hidden_question_is_not_an_answer(self):
        definition = make_definition(
            [
                q("q1", options=["yes", "no"]),
                q("q2", options=["detail"], gate=eq("q1", "yes")),
            ]
        )
        # q1=no 决定 q2 隐藏，但采集器缓存了 q2 的旧答案。
        snap = make_snapshot(
            root(answers={"q1": "no", "q2": "detail"}), definition
        )
        rows = index(evaluate_snapshot(definition, snap))
        self.assertIs(rows[("q2", "")].visibility, FALSE)
        self.assertEqual(rows[("q2", "")].answer_status, NOT_ASKED_CACHED)
        self.assertEqual(rows[("q2", "")].answer_value, "detail")
        self.assertIsNotNone(rows[("q2", "")].reason)

    def test_shown_question_with_no_cache_is_displayed_unanswered(self):
        definition = make_definition([q("q1", options=[1])])
        rows = index(evaluate_snapshot(
            definition, make_snapshot(root(answers={}), definition)))
        self.assertIs(rows[("q1", "")].visibility, TRUE)
        self.assertEqual(rows[("q1", "")].answer_status, DISPLAYED_UNANSWERED)


class ExistsTests(unittest.TestCase):
    def _definition(self):
        # grp 在前，root 题 q_after 询问 grp 中是否有实例答 yes。
        from tests.helpers import grp

        return make_definition(
            [
                grp("grp", [q("inside", options=["yes", "no"])]),
                q("q_after", qtype="text", gate=exists("grp", "grp.inside", "yes")),
                q("plain", qtype="text", options=None),
            ]
        )

    def test_closed_empty_group_is_false(self):
        definition = self._definition()
        snap = make_snapshot(
            root(groups={"grp": ilist(closed=True)}), definition
        )
        rows = index(evaluate_snapshot(definition, snap))
        self.assertIs(rows[("q_after", "")].visibility, FALSE)
        # 空组不产生 inside 行，也不造假。
        self.assertNotIn(("grp.inside", "grp#any"), rows)
        self.assertEqual(
            [r.static_path for r in evaluate_snapshot(definition, snap)],
            ["q_after", "plain"],
        )

    def test_open_empty_group_is_unknown_not_false(self):
        definition = self._definition()
        snap = make_snapshot(
            root(groups={"grp": ilist(closed=False)}), definition
        )
        rows = index(evaluate_snapshot(definition, snap))
        self.assertIs(rows[("q_after", "")].visibility, UNKNOWN)
        summary = next(e for e in rows[("q_after", "")].evidence if e.kind == "exists")
        self.assertEqual(summary.status, "open_unknown")

    def test_open_list_with_known_true_is_true(self):
        definition = self._definition()
        snap = make_snapshot(
            root(
                groups={
                    "grp": ilist(
                        inst("i1", answers={"grp.inside": "no"}),
                        inst("i2", answers={"grp.inside": "yes"}),
                        closed=False,
                    )
                }
            ),
            definition,
        )
        rows = index(evaluate_snapshot(definition, snap))
        self.assertIs(rows[("q_after", "")].visibility, TRUE)
        match = [e for e in rows[("q_after", "")].evidence
                 if e.kind == "exists_match"]
        self.assertEqual(len(match), 1)
        self.assertIn("grp#i2", match[0].instance_path)

    def test_open_list_no_match_is_unknown(self):
        definition = self._definition()
        snap = make_snapshot(
            root(
                groups={
                    "grp": ilist(
                        inst("i1", answers={"grp.inside": "no"}),
                        closed=False,
                    )
                }
            ),
            definition,
        )
        rows = index(evaluate_snapshot(definition, snap))
        self.assertIs(rows[("q_after", "")].visibility, UNKNOWN)

    def test_closed_list_no_match_is_false(self):
        definition = self._definition()
        snap = make_snapshot(
            root(
                groups={
                    "grp": ilist(
                        inst("i1", answers={"grp.inside": "no"}),
                        closed=True,
                    )
                }
            ),
            definition,
        )
        rows = index(evaluate_snapshot(definition, snap))
        self.assertIs(rows[("q_after", "")].visibility, FALSE)

    def test_hidden_parent_overrides_open_list_unknown(self):
        from tests.helpers import grp

        # 外层 outer 有门控（由 root 的 q1 控制）；inner 是未封闭清单；
        # outer 之后的 q_after 询问 inner 是否存在 yes。
        definition = make_definition(
            [
                q("q1", options=["yes", "no"]),
                grp(
                    "outer",
                    [
                        grp("inner", [q("inner_q", options=["yes", "no"])]),
                        q(
                            "outer_q",
                            qtype="text",
                            gate=exists(
                                "outer.inner", "outer.inner.inner_q", "yes"
                            ),
                        ),
                    ],
                    gate=eq("q1", "yes"),
                ),
            ]
        )
        snap = make_snapshot(
            root(
                answers={"q1": "no"},
                groups={
                    "outer": ilist(
                        inst(
                            "o1",
                            groups={"outer.inner": ilist(closed=False)},
                        ),
                        closed=True,
                    )
                },
            ),
            definition,
        )
        rows = index(evaluate_snapshot(definition, snap))
        # outer 确定隐藏：内部未封闭清单不得把 exists 变成未知。
        self.assertIs(rows[("outer.outer_q", "outer#o1")].visibility, FALSE)
        self.assertEqual(
            rows[("outer.outer_q", "outer#o1")].answer_status, NOT_ASKED
        )
        # 未列出 inner 实例 -> 没有 inner_q 行。
        self.assertFalse(
            any(r.static_path == "outer.inner.inner_q"
                for r in evaluate_snapshot(definition, snap))
        )

    def test_parent_unknown_lists_only_real_instances_and_ands_visibility(self):
        from tests.helpers import grp

        definition = make_definition(
            [
                q("q1", options=["yes", "no"]),
                grp(
                    "outer",
                    # 子题自身无门控：可见性完全等于父实例可见性。
                    [q("inside", options=["a", "b"])],
                    gate=eq("q1", "yes"),  # q1 缺答 -> outer 可见性未知
                ),
            ]
        )
        snap = make_snapshot(
            root(
                answers={},  # q1 缺答
                groups={
                    "outer": ilist(
                        inst("o1", answers={"outer.inside": "a"}),
                        closed=False,
                    )
                },
            ),
            definition,
        )
        rows = evaluate_snapshot(definition, snap)
        inside = [r for r in rows if r.static_path == "outer.inside"]
        self.assertEqual(len(inside), 1)  # 只列出 o1，不造假行
        self.assertEqual(inside[0].instance_path, "outer#o1")
        self.assertIs(inside[0].visibility, UNKNOWN)  # 未知 and 真 = 未知
        self.assertFalse(inside[0].list_closed)  # 清单状态保留
        self.assertEqual(inside[0].list_instance_count, 1)


class CrossInstanceTests(unittest.TestCase):
    def test_same_named_question_does_not_leak_across_instances(self):
        from tests.helpers import grp

        definition = make_definition(
            [
                grp(
                    "grp",
                    [
                        q("q", options=["yes", "no"]),
                        q("follow", options=["f"], gate=eq("grp.q", "yes")),
                    ],
                )
            ]
        )
        snap = make_snapshot(
            root(
                groups={
                    "grp": ilist(
                        inst("i1", answers={"grp.q": "yes", "grp.follow": "f"}),
                        inst("i2", answers={"grp.q": "no", "grp.follow": "f"}),
                        closed=True,
                    )
                }
            ),
            definition,
        )
        rows = index(evaluate_snapshot(definition, snap))
        self.assertIs(rows[("grp.follow", "grp#i1")].visibility, TRUE)
        self.assertEqual(
            rows[("grp.follow", "grp#i1")].answer_status, ANSWERED
        )
        self.assertIs(rows[("grp.follow", "grp#i2")].visibility, FALSE)
        self.assertEqual(
            rows[("grp.follow", "grp#i2")].answer_status, NOT_ASKED_CACHED
        )

    def test_ancestor_instance_reference_works_but_siblings_are_isolated(self):
        from tests.helpers import grp

        definition = make_definition(
            [
                grp(
                    "outer",
                    [
                        q("outer_q", options=["yes", "no"]),
                        grp(
                            "inner",
                            [
                                q(
                                    "inner_q",
                                    options=["x"],
                                    gate=eq("outer.outer_q", "yes"),
                                )
                            ],
                        ),
                    ],
                )
            ]
        )
        snap = make_snapshot(
            root(
                groups={
                    "outer": ilist(
                        inst(
                            "o1",
                            answers={"outer.outer_q": "yes"},
                            groups={
                                "outer.inner": ilist(
                                    inst("n1", answers={"outer.inner.inner_q": "x"})
                                )
                            },
                        ),
                        inst(
                            "o2",
                            answers={"outer.outer_q": "no"},
                            groups={
                                "outer.inner": ilist(
                                    inst("n2", answers={"outer.inner.inner_q": "x"})
                                )
                            },
                        ),
                    )
                }
            ),
            definition,
        )
        rows = index(evaluate_snapshot(definition, snap))
        self.assertIs(
            rows[("outer.inner.inner_q", "outer#o1.outer.inner#n1")].visibility,
            TRUE,
        )
        self.assertIs(
            rows[("outer.inner.inner_q", "outer#o2.outer.inner#n2")].visibility,
            FALSE,
        )


if __name__ == "__main__":
    unittest.main()
