"""reconcile：基准 + 两份回传快照三路合并的验收测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pipeline.cli import main as cli_main
from pipeline.engine import evaluate_snapshot
from pipeline.models import Snapshot
from pipeline.reconcile import (
    Conflict,
    MergeConflicts,
    ReconcileInputError,
    reconcile,
)
from pipeline.serialize import facts_to_dict
from pipeline.validate import load_snapshot, validate_definition
from tests.helpers import eq, exists, grp, ilist, inst, q, root


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def make_definition(nodes: list[dict[str, Any]], version: str = "v1"):
    return validate_definition({"version": version, "nodes": nodes})


def snapshot_doc(
    root_doc: dict[str, Any],
    submission_id: str = "sub-1",
    version: str = "v1",
) -> dict[str, Any]:
    return {
        "submission_id": submission_id,
        "version": version,
        "root": root_doc,
    }


class ReconcileCliCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "merged.json"

    def write(self, name: str, payload: Any) -> Path:
        path = self.dir / name
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return path

    def run_cli(
        self,
        definition: dict[str, Any],
        base: dict[str, Any],
        left: dict[str, Any],
        right: dict[str, Any],
        out: Path | None = None,
        names: tuple[str, str, str] = ("base.json", "left.json", "right.json"),
    ) -> int:
        return cli_main(
            [
                "reconcile",
                "--definition", str(self.write("def.json", definition)),
                "--base", str(self.write(names[0], base)),
                "--left", str(self.write(names[1], left)),
                "--right", str(self.write(names[2], right)),
                "--out", str(out or self.out),
            ]
        )

    def merged(self) -> dict[str, Any]:
        return json.loads(self.out.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 基础：原始字段三路合并
# ---------------------------------------------------------------------------


class FieldMergeTests(ReconcileCliCase):
    def test_independent_field_changes_from_both_devices_are_kept(self):
        definition = {
            "version": "v1",
            "nodes": [
                q("q1", options=["a", "b"]),
                q("q2", qtype="text", options=None),
                q("q3", qtype="number", options=None),
            ],
        }
        base = snapshot_doc(root(answers={"q1": "a", "q2": "base", "q3": 1}))
        left = snapshot_doc(root(answers={"q1": "b", "q2": "base", "q3": 1}))
        right = snapshot_doc(root(answers={"q1": "a", "q2": "base", "q3": 2}))
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        answers = self.merged()["snapshot"]["root"]["answers"]
        self.assertEqual(answers, {"q1": "b", "q2": "base", "q3": 2})

    def test_removing_a_field_counts_as_a_change(self):
        definition = {"version": "v1", "nodes": [q("q1", qtype="text", options=None)]}
        base = snapshot_doc(root(answers={"q1": "x"}))
        left = snapshot_doc(root(answers={}))
        right = snapshot_doc(root(answers={"q1": "x"}))
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        self.assertNotIn("answers", self.merged()["snapshot"]["root"])

    def test_added_field_from_one_side_is_kept(self):
        definition = {"version": "v1", "nodes": [q("q1", qtype="text", options=None)]}
        base = snapshot_doc(root(answers={}))
        left = snapshot_doc(root(answers={"q1": "new"}))
        right = snapshot_doc(root(answers={}))
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        self.assertEqual(
            self.merged()["snapshot"]["root"]["answers"], {"q1": "new"}
        )

    def test_same_change_on_both_sides_counts_once(self):
        definition = {"version": "v1", "nodes": [q("q1", qtype="text", options=None)]}
        base = snapshot_doc(root(answers={"q1": "base"}))
        left = snapshot_doc(root(answers={"q1": "new"}))
        right = snapshot_doc(root(answers={"q1": "new"}))
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        self.assertEqual(self.merged()["snapshot"]["root"]["answers"], {"q1": "new"})

    def test_different_changes_conflict_with_location_and_no_output(self):
        definition = {"version": "v1", "nodes": [q("q1", qtype="text", options=None)]}
        base = snapshot_doc(root(answers={"q1": "base"}))
        left = snapshot_doc(root(answers={"q1": "L"}))
        right = snapshot_doc(root(answers={"q1": "R"}))
        self.assertEqual(self.run_cli(definition, base, left, right), 8)
        self.assertFalse(self.out.exists())

    def test_conflict_message_names_group_instance_and_question(self):
        definition = {
            "version": "v1",
            "nodes": [grp("grp", [q("q_inside", qtype="text", options=None)])],
        }
        base = snapshot_doc(
            root(groups={"grp": ilist(inst("m1", answers={"grp.q_inside": "base"}))})
        )
        left = snapshot_doc(
            root(groups={"grp": ilist(inst("m1", answers={"grp.q_inside": "L"}))})
        )
        right = snapshot_doc(
            root(groups={"grp": ilist(inst("m1", answers={"grp.q_inside": "R"}))})
        )
        out_path = self.dir / "out.json"
        with self.assertRaises(MergeConflicts) as ctx:
            self.call_reconcile(definition, base, left, right, out_path)
        (conflict,) = ctx.exception.conflicts
        self.assertEqual(conflict.code, "ANSWER_CONFLICT")
        self.assertEqual(conflict.location, "grp#m1 :: grp.q_inside")
        self.assertFalse(out_path.exists())

    def call_reconcile(self, definition, base, left, right, out_path):
        return reconcile(
            self.write("def2.json", definition),
            self.write("b2.json", base),
            self.write("l2.json", left),
            self.write("r2.json", right),
            out_path,
        )

    def test_missing_field_vs_explicit_null_is_not_equal(self):
        definition = {"version": "v1", "nodes": [q("q1", qtype="text", options=None)]}
        base = snapshot_doc(root(answers={"q1": "base"}))
        left = snapshot_doc(root(answers={"q1": None}))
        right = snapshot_doc(root(answers={}))
        # 一边显式 null、一边删掉字段：与基准都不同且彼此不同 -> 冲突。
        self.assertEqual(self.run_cli(definition, base, left, right), 8)
        self.assertFalse(self.out.exists())

    def test_explicit_null_from_one_side_is_kept(self):
        definition = {"version": "v1", "nodes": [q("q1", qtype="text", options=None)]}
        base = snapshot_doc(root(answers={"q1": "base"}))
        left = snapshot_doc(root(answers={"q1": None}))
        right = snapshot_doc(root(answers={"q1": "base"}))
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        self.assertEqual(
            self.merged()["snapshot"]["root"]["answers"], {"q1": None}
        )

    def test_illegal_option_value_is_merged_not_dropped(self):
        # 缓存值不在本版本选项里：仍属原始字段，必须照常合并（裁决交给 one）。
        definition = {"version": "v1", "nodes": [q("q1", options=["a"])]}
        base = snapshot_doc(root(answers={"q1": "a"}))
        left = snapshot_doc(root(answers={"q1": "legacy"}))
        right = snapshot_doc(root(answers={"q1": "a"}))
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        payload = self.merged()
        self.assertEqual(payload["snapshot"]["root"]["answers"], {"q1": "legacy"})
        facts = {(r["static_path"], r["instance_path"]): r for r in payload["facts"]["facts"]}
        self.assertEqual(facts[("q1", "")]["answer_status"], "illegal_cached_value")
        self.assertEqual(facts[("q1", "")]["answer_value"], "legacy")

    def test_strict_scalar_type_equality(self):
        # true 与 1 是不同值：两边分别改成 true 和 1 -> 冲突。
        definition = {"version": "v1", "nodes": [q("q1", qtype="bool", options=None)]}
        base = snapshot_doc(root(answers={"q1": False}))
        left = snapshot_doc(root(answers={"q1": True}))
        right = snapshot_doc(root(answers={"q1": 1}))
        self.assertEqual(self.run_cli(definition, base, left, right), 8)


# ---------------------------------------------------------------------------
# 规范化：键序、数组顺序、省略与封闭空清单
# ---------------------------------------------------------------------------


class CanonicalFormTests(ReconcileCliCase):
    def test_key_order_and_instance_order_are_not_changes(self):
        definition = {
            "version": "v1",
            "nodes": [
                q("q1", qtype="text", options=None),
                q("q2", qtype="text", options=None),
                grp("grp", [q("inner", qtype="text", options=None)]),
            ],
        }
        base = snapshot_doc(
            root(
                answers={"q1": "a", "q2": "b"},
                groups={
                    "grp": ilist(
                        inst("m1", answers={"grp.inner": "1"}),
                        inst("m2", answers={"grp.inner": "2"}),
                    )
                },
            )
        )
        left = snapshot_doc(
            root(
                answers={"q2": "b", "q1": "a"},
                groups={
                    "grp": ilist(
                        inst("m2", answers={"grp.inner": "2"}),
                        inst("m1", answers={"grp.inner": "1"}),
                    )
                },
            )
        )
        right = snapshot_doc(
            root(
                answers={"q1": "a", "q2": "b"},
                groups={
                    "grp": ilist(
                        inst("m1", answers={"grp.inner": "1"}),
                        inst("m2", answers={"grp.inner": "2"}),
                    )
                },
            )
        )
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        merged = self.merged()["snapshot"]
        # 输出确定性：答案键排序、实例按 ID 排序。
        self.assertEqual(list(merged["root"]["answers"]), ["q1", "q2"])
        self.assertEqual(
            [i["instance_id"] for i in merged["root"]["groups"]["grp"]["instances"]],
            ["m1", "m2"],
        )

    def test_omitted_list_equals_closed_empty_list(self):
        definition = {
            "version": "v1",
            "nodes": [grp("grp", [q("inside", qtype="text", options=None)])],
        }
        base = snapshot_doc(root())
        left = snapshot_doc(root())
        right = snapshot_doc(root(groups={"grp": {"instances": [], "closed": True}}))
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        # 省略清单与封闭空清单等价：合并结果输出为省略，且无冲突。
        self.assertNotIn("groups", self.merged()["snapshot"]["root"])

    def test_omitted_closed_means_closed(self):
        definition = {
            "version": "v1",
            "nodes": [grp("grp", [q("inside", qtype="text", options=None)])],
        }
        base = snapshot_doc(root(groups={"grp": {"instances": []}}))
        left = snapshot_doc(root(groups={"grp": {"instances": [], "closed": True}}))
        right = snapshot_doc(root(groups={"grp": {"instances": []}}))
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        self.assertNotIn("groups", self.merged()["snapshot"]["root"])

    def test_closed_change_and_member_delete_merge_separately(self):
        definition = {
            "version": "v1",
            "nodes": [grp("grp", [q("inside", qtype="text", options=None)])],
        }
        base = snapshot_doc(
            root(
                groups={
                    "grp": {
                        "instances": [inst("m1", answers={"grp.inside": "x"})],
                        "closed": True,
                    }
                }
            )
        )
        left = snapshot_doc(
            root(
                groups={
                    "grp": {
                        "instances": [inst("m1", answers={"grp.inside": "x"})],
                        "closed": False,
                    }
                }
            )
        )
        right = snapshot_doc(root(groups={"grp": {"instances": [], "closed": True}}))
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        groups = self.merged()["snapshot"]["root"]["groups"]
        self.assertEqual(groups["grp"], {"instances": [], "closed": False})

    def test_closed_change_and_member_add_merge_independently(self):
        definition = {
            "version": "v1",
            "nodes": [grp("grp", [q("inside", qtype="text", options=None)])],
        }
        base = snapshot_doc(root(groups={"grp": {"instances": [], "closed": True}}))
        # 一方只改 closed；另一方只新增成员并保持封闭：两处改动分属不同维度。
        left = snapshot_doc(root(groups={"grp": {"instances": [], "closed": False}}))
        right = snapshot_doc(
            root(groups={"grp": {"instances": [inst("m1")], "closed": True}})
        )
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        self.assertEqual(
            self.merged()["snapshot"]["root"]["groups"]["grp"],
            {"instances": [{"instance_id": "m1"}], "closed": False},
        )


# ---------------------------------------------------------------------------
# 成员身份与增删
# ---------------------------------------------------------------------------


class MembershipTests(ReconcileCliCase):
    def definition(self):
        return {
            "version": "v1",
            "nodes": [
                grp(
                    "grp_hh",
                    [
                        q("q_name", qtype="text", options=None),
                        grp("grp_member", [q("q_age", qtype="number", options=None)]),
                    ],
                ),
            ],
        }

    def base_doc(self):
        return snapshot_doc(
            root(
                groups={
                    "grp_hh": ilist(
                        inst(
                            "hh1",
                            answers={"grp_hh.q_name": "A"},
                            groups={
                                "grp_hh.grp_member": ilist(
                                    inst("m1", answers={"grp_hh.grp_member.q_age": 10})
                                )
                            },
                        ),
                        inst(
                            "hh2",
                            answers={"grp_hh.q_name": "B"},
                            groups={
                                "grp_hh.grp_member": ilist(
                                    inst("m1", answers={"grp_hh.grp_member.q_age": 20})
                                )
                            },
                        ),
                    )
                }
            )
        )

    def test_same_short_id_under_different_parents_is_independent(self):
        base = self.base_doc()
        left = json.loads(json.dumps(base))
        right = json.loads(json.dumps(base))
        # 删掉 hh2（连同其下同名子成员）；改 hh1 下的 m1。
        left["root"]["groups"]["grp_hh"]["instances"] = [
            i for i in left["root"]["groups"]["grp_hh"]["instances"]
            if i["instance_id"] == "hh1"
        ]
        right["root"]["groups"]["grp_hh"]["instances"][0]["groups"][
            "grp_hh.grp_member"
        ]["instances"][0]["answers"]["grp_hh.grp_member.q_age"] = 11
        self.assertEqual(self.run_cli(self.definition(), base, left, right), 0)
        members = self.merged()["snapshot"]["root"]["groups"]["grp_hh"]["instances"]
        self.assertEqual([m["instance_id"] for m in members], ["hh1"])
        self.assertEqual(
            members[0]["groups"]["grp_hh.grp_member"]["instances"][0]["answers"][
                "grp_hh.grp_member.q_age"
            ],
            11,
        )

    def test_modifying_child_of_deleted_parent_conflicts_and_keeps_old_output(self):
        base = self.base_doc()
        self.out.write_text('{"previous": true}\n', encoding="utf-8")
        left = json.loads(json.dumps(base))
        right = json.loads(json.dumps(base))
        left["root"]["groups"]["grp_hh"]["instances"] = [
            i for i in left["root"]["groups"]["grp_hh"]["instances"]
            if i["instance_id"] == "hh1"
        ]
        # 右方改的是被删父成员 hh2 下面的同名子成员。
        right["root"]["groups"]["grp_hh"]["instances"][1]["groups"][
            "grp_hh.grp_member"
        ]["instances"][0]["answers"]["grp_hh.grp_member.q_age"] = 21
        self.assertEqual(self.run_cli(self.definition(), base, left, right), 8)
        self.assertEqual(
            json.loads(self.out.read_text(encoding="utf-8")), {"previous": True}
        )

    def test_one_side_deletes_other_keeps_unchanged_member(self):
        definition = {
            "version": "v1",
            "nodes": [grp("grp", [q("inside", qtype="text", options=None)])],
        }
        base = snapshot_doc(
            root(groups={"grp": ilist(inst("m1"), inst("m2"))})
        )
        left = snapshot_doc(root(groups={"grp": ilist(inst("m2"))}))
        right = snapshot_doc(root(groups={"grp": ilist(inst("m1"), inst("m2"))}))
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        ids = [
            i["instance_id"]
            for i in self.merged()["snapshot"]["root"]["groups"]["grp"]["instances"]
        ]
        self.assertEqual(ids, ["m2"])

    def test_both_sides_delete_member(self):
        definition = {
            "version": "v1",
            "nodes": [grp("grp", [q("inside", qtype="text", options=None)])],
        }
        base = snapshot_doc(root(groups={"grp": ilist(inst("m1"), inst("m2"))}))
        left = snapshot_doc(root(groups={"grp": ilist(inst("m2"))}))
        right = snapshot_doc(root(groups={"grp": ilist(inst("m2"))}))
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        ids = [
            i["instance_id"]
            for i in self.merged()["snapshot"]["root"]["groups"]["grp"]["instances"]
        ]
        self.assertEqual(ids, ["m2"])

    def test_one_side_deletes_other_modifies_member_itself_conflicts(self):
        definition = {
            "version": "v1",
            "nodes": [grp("grp", [q("inside", qtype="text", options=None)])],
        }
        base = snapshot_doc(
            root(groups={"grp": ilist(inst("m1", answers={"grp.inside": "x"}))})
        )
        left = snapshot_doc(root(groups={"grp": ilist(inst("m1", answers={"grp.inside": "x"}))}))
        left = snapshot_doc(root(groups={"grp": ilist()}))
        right = snapshot_doc(
            root(groups={"grp": ilist(inst("m1", answers={"grp.inside": "y"}))})
        )
        self.assertEqual(self.run_cli(definition, base, left, right), 8)

    def test_new_member_from_one_side_is_kept(self):
        definition = {
            "version": "v1",
            "nodes": [grp("grp", [q("inside", qtype="text", options=None)])],
        }
        base = snapshot_doc(root(groups={"grp": ilist()}))
        left = snapshot_doc(
            root(groups={"grp": ilist(inst("new1", answers={"grp.inside": "a"}))})
        )
        right = snapshot_doc(root(groups={"grp": ilist()}))
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        members = self.merged()["snapshot"]["root"]["groups"]["grp"]["instances"]
        self.assertEqual(members, [{"instance_id": "new1", "answers": {"grp.inside": "a"}}])

    def test_new_member_created_identically_on_both_sides_is_accepted(self):
        definition = {
            "version": "v1",
            "nodes": [grp("grp", [q("inside", qtype="text", options=None)])],
        }
        base = snapshot_doc(root(groups={"grp": ilist()}))
        left = snapshot_doc(
            root(groups={"grp": ilist(inst("new1", answers={"grp.inside": "a"}))})
        )
        right = snapshot_doc(
            root(groups={"grp": ilist(inst("new1", answers={"grp.inside": "a"}))})
        )
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        members = self.merged()["snapshot"]["root"]["groups"]["grp"]["instances"]
        self.assertEqual(len(members), 1)

    def test_new_member_created_identically_with_different_order_is_accepted(self):
        definition = {
            "version": "v1",
            "nodes": [
                grp(
                    "grp",
                    [
                        q("a", qtype="text", options=None),
                        q("b", qtype="text", options=None),
                    ],
                )
            ],
        }
        base = snapshot_doc(root(groups={"grp": ilist()}))
        left = snapshot_doc(
            root(groups={"grp": ilist(inst("new1", answers={"grp.a": "1", "grp.b": "2"}))})
        )
        right = snapshot_doc(
            root(groups={"grp": ilist(inst("new1", answers={"grp.b": "2", "grp.a": "1"}))})
        )
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        members = self.merged()["snapshot"]["root"]["groups"]["grp"]["instances"]
        self.assertEqual(len(members), 1)

    def test_kept_member_merges_independent_internal_changes(self):
        definition = {
            "version": "v1",
            "nodes": [
                grp(
                    "grp",
                    [
                        q("a", qtype="text", options=None),
                        q("b", qtype="text", options=None),
                        grp("sub", [q("c", qtype="text", options=None)]),
                    ],
                )
            ],
        }
        base = snapshot_doc(
            root(
                groups={
                    "grp": ilist(
                        inst(
                            "m1",
                            answers={"grp.a": "A", "grp.b": "B"},
                            groups={"grp.sub": ilist(inst("s1", answers={"grp.sub.c": "C"}))},
                        )
                    )
                }
            )
        )
        left = json.loads(json.dumps(base))
        right = json.loads(json.dumps(base))
        left["root"]["groups"]["grp"]["instances"][0]["answers"]["grp.a"] = "A2"
        left["root"]["groups"]["grp"]["instances"][0]["groups"]["grp.sub"][
            "instances"
        ][0]["answers"]["grp.sub.c"] = "C2"
        right["root"]["groups"]["grp"]["instances"][0]["answers"]["grp.b"] = "B2"
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        member = self.merged()["snapshot"]["root"]["groups"]["grp"]["instances"][0]
        self.assertEqual(member["answers"], {"grp.a": "A2", "grp.b": "B2"})
        self.assertEqual(
            member["groups"]["grp.sub"]["instances"][0]["answers"],
            {"grp.sub.c": "C2"},
        )

    def test_new_member_created_differently_on_both_sides_conflicts(self):
        definition = {
            "version": "v1",
            "nodes": [grp("grp", [q("inside", qtype="text", options=None)])],
        }
        base = snapshot_doc(root(groups={"grp": ilist()}))
        left = snapshot_doc(
            root(groups={"grp": ilist(inst("new1", answers={"grp.inside": "a"}))})
        )
        right = snapshot_doc(
            root(groups={"grp": ilist(inst("new1", answers={"grp.inside": "b"}))})
        )
        self.assertEqual(self.run_cli(definition, base, left, right), 8)

    def test_nested_conflict_names_full_path(self):
        definition = {
            "version": "v1",
            "nodes": [
                grp("outer", [grp("inner", [q("deep", qtype="text", options=None)])])
            ],
        }
        base = snapshot_doc(
            root(
                groups={
                    "outer": ilist(
                        inst("o1", groups={"outer.inner": ilist(inst("i1", answers={"outer.inner.deep": "x"}))})
                    )
                }
            )
        )
        left = json.loads(json.dumps(base))
        right = json.loads(json.dumps(base))
        left["root"]["groups"]["outer"]["instances"][0]["groups"][
            "outer.inner"
        ]["instances"][0]["answers"]["outer.inner.deep"] = "L"
        right["root"]["groups"]["outer"]["instances"][0]["groups"][
            "outer.inner"
        ]["instances"][0]["answers"]["outer.inner.deep"] = "R"
        with self.assertRaises(MergeConflicts) as ctx:
            self.call_reconcile(definition, base, left, right, self.dir / "o.json")
        self.assertEqual(
            ctx.exception.conflicts[0].location,
            "outer#o1.outer.inner#i1 :: outer.inner.deep",
        )

    def call_reconcile(self, definition, base, left, right, out_path):
        return reconcile(
            self.write("def2.json", definition),
            self.write("b2.json", base),
            self.write("l2.json", left),
            self.write("r2.json", right),
            out_path,
        )


# ---------------------------------------------------------------------------
# 主场景：未封闭空组 + 依赖「是否存在命中」的题
# ---------------------------------------------------------------------------


class ExistsScenarioTests(ReconcileCliCase):
    def definition(self):
        return {
            "version": "v1",
            "nodes": [
                grp("grp_visit", [q("q_hit", options=["yes", "no"])]),
                q(
                    "q_followup",
                    qtype="text",
                    options=None,
                    gate=exists("grp_visit", "grp_visit.q_hit", "yes"),
                ),
            ],
        }

    def test_open_empty_group_makes_exists_question_undetermined(self):
        definition = self.definition()
        base = snapshot_doc(
            root(
                groups={
                    "grp_visit": ilist(inst("v1", answers={"grp_visit.q_hit": "yes"}))
                }
            )
        )
        # 一台设备标未封闭但保留成员；另一台删掉成员但保持封闭。
        left = snapshot_doc(
            root(
                groups={
                    "grp_visit": {
                        "instances": [inst("v1", answers={"grp_visit.q_hit": "yes"})],
                        "closed": False,
                    }
                }
            )
        )
        right = snapshot_doc(root(groups={"grp_visit": ilist(closed=True)}))
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        payload = self.merged()
        self.assertEqual(
            payload["snapshot"]["root"]["groups"]["grp_visit"],
            {"instances": [], "closed": False},
        )
        facts = {
            (r["static_path"], r["instance_path"]): r
            for r in payload["facts"]["facts"]
        }
        self.assertNotIn(("grp_visit.q_hit", "grp_visit#v1"), facts)
        self.assertEqual(facts[("q_followup", "")]["visibility"], "unknown")
        self.assertEqual(facts[("q_followup", "")]["answer_status"], "undetermined")

    def test_merged_facts_are_readjudicated_not_borrowed(self):
        # 左方改了 q1（把题隐藏），右方保持 answered；事实必须按合并快照重判。
        definition = {
            "version": "v1",
            "nodes": [
                q("q1", options=["a", "b"]),
                q("q2", qtype="text", options=None, gate=eq("q1", "a")),
            ],
        }
        base = snapshot_doc(root(answers={"q1": "a", "q2": "keep"}))
        left = snapshot_doc(root(answers={"q1": "b", "q2": "keep"}))
        right = snapshot_doc(root(answers={"q1": "a", "q2": "keep"}))
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        payload = self.merged()
        self.assertEqual(payload["snapshot"]["root"]["answers"]["q1"], "b")
        facts = {
            (r["static_path"], r["instance_path"]): r
            for r in payload["facts"]["facts"]
        }
        self.assertEqual(facts[("q2", "")]["visibility"], "false")
        self.assertEqual(facts[("q2", "")]["answer_status"], "not_asked_cached")
        self.assertEqual(facts[("q2", "")]["answer_value"], "keep")


# ---------------------------------------------------------------------------
# 确定性
# ---------------------------------------------------------------------------


class DeterminismTests(ReconcileCliCase):
    def test_swapping_sides_and_reordering_members_changes_nothing(self):
        definition = {
            "version": "v1",
            "nodes": [
                q("q1", qtype="text", options=None),
                grp("grp", [q("inside", qtype="text", options=None)]),
            ],
        }
        base = snapshot_doc(
            root(
                answers={"q1": "b"},
                groups={
                    "grp": ilist(
                        inst("m1", answers={"grp.inside": "1"}),
                        inst("m2", answers={"grp.inside": "2"}),
                        inst("m3", answers={"grp.inside": "3"}),
                    )
                },
            )
        )
        left = json.loads(json.dumps(base))
        right = json.loads(json.dumps(base))
        left["root"]["answers"]["q1"] = "L"
        left["root"]["groups"]["grp"]["instances"][0]["answers"]["grp.inside"] = "one"
        left["root"]["groups"]["grp"]["instances"].reverse()
        right["root"]["groups"]["grp"]["instances"][2]["answers"]["grp.inside"] = "three"
        right["root"]["groups"]["grp"]["instances"][1]["groups"] = {}
        del right["root"]["groups"]["grp"]["instances"][1]["groups"]
        right["root"]["groups"]["grp"]["instances"].reverse()

        out1 = self.dir / "a.json"
        out2 = self.dir / "b.json"
        self.assertEqual(self.run_cli(definition, base, left, right, out=out1), 0)
        self.assertEqual(self.run_cli(definition, base, right, left, out=out2), 0)
        self.assertEqual(
            out1.read_text(encoding="utf-8"), out2.read_text(encoding="utf-8")
        )
        payload = json.loads(out1.read_text(encoding="utf-8"))
        ids = [
            i["instance_id"]
            for i in payload["snapshot"]["root"]["groups"]["grp"]["instances"]
        ]
        self.assertEqual(ids, ["m1", "m2", "m3"])

    def test_output_snapshot_can_be_fed_to_one_and_matches_facts(self):
        definition = {"version": "v1", "nodes": [q("q1", qtype="text", options=None)]}
        base = snapshot_doc(root(answers={"q1": "base"}))
        left = snapshot_doc(root(answers={"q1": "new"}))
        right = snapshot_doc(root(answers={"q1": "base"}))
        self.assertEqual(self.run_cli(definition, base, left, right), 0)
        payload = self.merged()
        parsed = make_definition(
            [q("q1", qtype="text", options=None)]
        )
        snapshot = load_snapshot(payload["snapshot"], parsed)
        rows = evaluate_snapshot(parsed, snapshot)
        self.assertEqual(
            payload["facts"],
            facts_to_dict(snapshot.submission_id, parsed.version, rows),
        )


# ---------------------------------------------------------------------------
# 输入错误
# ---------------------------------------------------------------------------


class InputErrorTests(ReconcileCliCase):
    def good_definition(self):
        return {
            "version": "v1",
            "nodes": [
                q("q1", options=["a", "b"]),
                grp("grp", [q("inside", qtype="text", options=None)]),
            ],
        }

    def good_base(self):
        return snapshot_doc(
            root(
                answers={"q1": "a"},
                groups={"grp": ilist(inst("m1", answers={"grp.inside": "x"}))},
            )
        )

    def test_duplicate_json_key_rejected(self):
        definition = self.write("d.json", self.good_definition())
        base = self.write("b.json", self.good_base())
        left = self.write(
            "l.json",
            '{"submission_id":"sub-1","submission_id":"sub-1","version":"v1",'
            '"root":{}}',
        )
        right = self.write("r.json", self.good_base())
        out = self.dir / "o.json"
        with self.assertRaises(ReconcileInputError) as ctx:
            reconcile(definition, base, left, right, out)
        self.assertEqual(ctx.exception.code, "DUPLICATE_JSON_KEY")
        self.assertFalse(out.exists())

    def test_non_finite_number_rejected(self):
        definition = self.write("d.json", self.good_definition())
        base = self.write("b.json", self.good_base())
        for token in ("NaN", "Infinity", "1e999"):
            left = self.write(
                "l.json",
                '{"submission_id":"sub-1","version":"v1",'
                f'"root":{{"answers":{{"q1":{token}}}}}}}',
            )
            right = self.write("r.json", self.good_base())
            with self.assertRaises(ReconcileInputError) as ctx:
                reconcile(definition, base, left, right, self.dir / "o.json")
            self.assertEqual(ctx.exception.code, "INVALID_JSON", token)

    def test_non_boolean_closed_rejected(self):
        definition = self.write("d.json", self.good_definition())
        base = self.write("b.json", self.good_base())
        left = self.write(
            "l.json",
            '{"submission_id":"sub-1","version":"v1","root":{"groups":'
            '{"grp":{"instances":[],"closed":"true"}}}}',
        )
        right = self.write("r.json", self.good_base())
        with self.assertRaises(ReconcileInputError) as ctx:
            reconcile(definition, base, left, right, self.dir / "o.json")
        self.assertEqual(ctx.exception.code, "NON_BOOLEAN_CLOSED")

    def test_invalid_instance_id_rejected(self):
        definition = self.write("d.json", self.good_definition())
        base = self.write("b.json", self.good_base())
        left = self.write(
            "l.json",
            '{"submission_id":"sub-1","version":"v1","root":{"groups":'
            '{"grp":{"instances":[{"instance_id":"bad id"}]}}}}',
        )
        right = self.write("r.json", self.good_base())
        with self.assertRaises(ReconcileInputError) as ctx:
            reconcile(definition, base, left, right, self.dir / "o.json")
        self.assertEqual(ctx.exception.code, "INVALID_INSTANCE_ID")

    def test_submission_id_mismatch_is_rejected_by_cli(self):
        self.assertEqual(
            self.run_cli(
                self.good_definition(),
                self.good_base(),
                snapshot_doc(root(), submission_id="other"),
                self.good_base(),
            ),
            7,
        )
        self.assertFalse(self.out.exists())

    def test_empty_submission_id_rejected(self):
        self.assertEqual(
            self.run_cli(
                self.good_definition(),
                snapshot_doc(root(), submission_id=""),
                self.good_base(),
                self.good_base(),
            ),
            7,
        )

    def test_version_mismatch_rejected(self):
        self.assertEqual(
            self.run_cli(
                self.good_definition(),
                self.good_base(),
                snapshot_doc(root(), version="v2"),
                self.good_base(),
            ),
            7,
        )

    def test_structurally_invalid_snapshot_rejected(self):
        self.assertEqual(
            self.run_cli(
                self.good_definition(),
                self.good_base(),
                snapshot_doc(root(answers={"ghost": 1})),
                self.good_base(),
            ),
            7,
        )

    def test_bad_definition_rejected(self):
        bad = {
            "version": "v1",
            "nodes": [q("q1", options=["a"], gate=eq("missing", "a"))],
        }
        self.assertEqual(
            self.run_cli(bad, self.good_base(), self.good_base(), self.good_base()),
            7,
        )


if __name__ == "__main__":
    unittest.main()
