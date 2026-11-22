"""定义级拒绝（非法引用、环、非法作用域、嵌套上限等）与快照级拒绝测试。"""

from __future__ import annotations

import unittest

from pipeline.errors import DefinitionRejected, SnapshotRejected
from tests.helpers import (
    answered,
    eq,
    exists,
    grp,
    ilist,
    inst,
    make_definition,
    make_snapshot,
    q,
    root,
)


class DefinitionRejectionTests(unittest.TestCase):
    def _assert_codes(self, raw_nodes, expected_codes, version="v1"):
        with self.assertRaises(DefinitionRejected) as ctx:
            make_definition(raw_nodes, version=version)
        codes = {e.code for e in ctx.exception.errors}
        for code in expected_codes:
            self.assertIn(code, codes, f"缺少错误码 {code}，实际 {sorted(codes)}")

    def test_missing_reference(self):
        self._assert_codes(
            [q("q1", options=[1]), q("q2", gate=eq("ghost", 1))],
            ["BAD_REF"],
        )

    def test_forward_reference_rejected(self):
        self._assert_codes(
            [q("q1", gate=eq("q2", 1)), q("q2", options=[1])],
            ["ORDER"],
        )

    def test_cycle_rejected(self):
        # 顺序约束通常已排除环；构造互指环验证 CYCLE 兜底。
        self._assert_codes(
            [
                q("q1", options=[1], gate=eq("q2", 1)),
                q("q2", options=[1], gate=eq("q1", 1)),
            ],
            ["ORDER", "CYCLE"],
        )

    def test_sibling_scope_reference_rejected(self):
        # 不同实例链的题不可引用。
        self._assert_codes(
            [
                grp("g1", [q("inner", options=[1])]),
                grp(
                    "g2",
                    [q("other", options=[1], gate=eq("g1.inner", 1))],
                ),
            ],
            ["SCOPE"],
        )

    def test_descendant_scope_reference_rejected(self):
        # 祖先实例不能引用子组内的题。
        self._assert_codes(
            [
                grp("g1", [q("inner", options=[1])]),
                q("after", options=[1], gate=eq("g1.inner", 1)),
            ],
            ["SCOPE"],
        )

    def test_exists_group_must_be_direct_child_and_earlier(self):
        self._assert_codes(
            [
                q("after", gate=exists("grp", "grp.inside", "yes")),
                grp("grp", [q("inside", options=["yes"])]),
            ],
            ["ORDER"],
        )
        self._assert_codes(
            [
                grp("outer", [grp("inner", [q("inside", options=["yes"])])]),
                q(
                    "after",
                    gate=exists("outer.inner", "outer.inner.inside", "yes"),
                ),
            ],
            ["SCOPE"],
        )

    def test_exists_ref_must_belong_to_group(self):
        self._assert_codes(
            [
                grp("grp", [q("inside", options=["yes"])]),
                q("sibling", options=["yes"]),
                q("after", gate=exists("grp", "sibling", "yes")),
            ],
            ["SCOPE"],
        )

    def test_duplicate_id_and_bad_options_rejected(self):
        self._assert_codes(
            [
                q("same", options=[1]),
                q("same", options=[2]),
                q("bad", options=[]),
            ],
            ["DUP_ID"],
        )
        # 空 options 单独可被检出（重复 id 会让节点被跳过，故分开验证）。
        self._assert_codes([q("bad", options=[])], ["OPTIONS"])

    def test_more_than_two_levels_rejected(self):
        nested = grp(
            "l1",
            [grp("l2", [grp("l3", [q("deep", qtype="text")])])],
        )
        self._assert_codes([nested], ["NESTING"])

    def test_unknown_gate_op_rejected(self):
        self._assert_codes(
            [q("q1", options=[1]), q("q2", gate={"op": "gte", "ref": "q1"})],
            ["GATE_OP"],
        )

    def test_no_partial_facts_from_rejected_definition(self):
        with self.assertRaises(DefinitionRejected):
            make_definition(
                [
                    q("good", options=[1]),  # 这个题本身合法
                    q("bad", gate=answered("nope")),  # 但定义整体非法
                ]
            )


class SnapshotRejectionTests(unittest.TestCase):
    def setUp(self):
        self.definition = make_definition(
            [
                q("q1", options=["a", "b"]),
                grp("grp", [q("inside", options=["x", "y"])]),
            ]
        )

    def test_version_mismatch_rejects_snapshot_only(self):
        with self.assertRaises(SnapshotRejected) as ctx:
            make_snapshot(
                root(answers={"q1": "a"}),
                self.definition,
                version="other-version",
            )
        self.assertEqual(ctx.exception.errors[0].code, "VERSION_MISMATCH")

    def test_unknown_question_rejected(self):
        with self.assertRaises(SnapshotRejected) as ctx:
            make_snapshot(root(answers={"ghost": 1}), self.definition)
        self.assertEqual(ctx.exception.errors[0].code, "UNKNOWN_QUESTION")

    def test_answer_in_wrong_scope_rejected(self):
        with self.assertRaises(SnapshotRejected) as ctx:
            make_snapshot(
                root(
                    answers={"grp.inside": "x"},  # 子组题放到根实例
                ),
                self.definition,
            )
        self.assertEqual(ctx.exception.errors[0].code, "SCOPE")

    def test_missing_instance_id_rejected(self):
        with self.assertRaises(SnapshotRejected) as ctx:
            make_snapshot(
                root(groups={"grp": ilist({"answers": {"grp.inside": "x"}})}),
                self.definition,
            )
        self.assertEqual(ctx.exception.errors[0].code, "INSTANCE_ID")

    def test_duplicate_instance_id_rejected(self):
        with self.assertRaises(SnapshotRejected) as ctx:
            make_snapshot(
                root(
                    groups={
                        "grp": ilist(
                            inst("dup", answers={"grp.inside": "x"}),
                            inst("dup", answers={"grp.inside": "y"}),
                        )
                    }
                ),
                self.definition,
            )
        self.assertEqual(ctx.exception.errors[0].code, "DUP_INSTANCE")

    def test_non_scalar_answer_rejected(self):
        with self.assertRaises(SnapshotRejected) as ctx:
            make_snapshot(root(answers={"q1": ["a"]}), self.definition)
        self.assertEqual(ctx.exception.errors[0].code, "ANSWER_SHAPE")


if __name__ == "__main__":
    unittest.main()
