"""验收测试：同一缓存值两条门控路径、未封闭/空组、跨版本与跨实例隔离、批量隔离。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pipeline.batch import run_batch
from pipeline.engine import (
    ANSWERED,
    ILLEGAL_CACHED,
    NOT_ASKED_CACHED,
    evaluate_snapshot,
)
from pipeline.tristate import FALSE, TRUE, UNKNOWN
from tests.helpers import (
    eq,
    ilist,
    index,
    inst,
    make_definition,
    make_snapshot,
    q,
    root,
)


class AcceptanceTests(unittest.TestCase):
    def test_same_cached_value_valid_and_invalid_under_two_gate_paths(self):
        # q1 合法值 {1,2}，门控路径 A：q2 在 q1==1 时展示；
        # 门控路径 B：q3 在 q1==2 时展示。缓存 q1=99（非法）。
        definition = make_definition(
            [
                q("q1", options=[1, 2]),
                q("q2", qtype="text", options=None, gate=eq("q1", 1)),
                q("q3", qtype="text", options=None, gate=eq("q1", 2)),
            ]
        )
        snap = make_snapshot(
            root(answers={"q1": 99, "q2": "note-a", "q3": "note-b"}),
            definition,
        )
        rows = index(evaluate_snapshot(definition, snap))

        # q1 确实展示：非法缓存必须标出无有效回答与原因。
        self.assertEqual(rows[("q1", "")].answer_status, ILLEGAL_CACHED)
        self.assertIn("99", rows[("q1", "")].reason)

        # 两条门控路径都不能把 99 当作作答 -> 可见性未知，而非真/假。
        self.assertIs(rows[("q2", "")].visibility, UNKNOWN)
        self.assertIs(rows[("q3", "")].visibility, UNKNOWN)

        # 对照：同一题在合法缓存下，路径 A 有效、路径 B 无效（隐藏）。
        snap_ok = make_snapshot(
            root(answers={"q1": 1, "q2": "note-a", "q3": "note-b"}),
            definition,
        )
        rows_ok = index(evaluate_snapshot(definition, snap_ok))
        self.assertIs(rows_ok[("q2", "")].visibility, TRUE)
        self.assertEqual(rows_ok[("q2", "")].answer_status, ANSWERED)
        self.assertIs(rows_ok[("q3", "")].visibility, FALSE)
        self.assertEqual(rows_ok[("q3", "")].answer_status, NOT_ASKED_CACHED)

    def test_open_vs_closed_empty_group_difference(self):
        from tests.helpers import exists, grp

        definition = make_definition(
            [
                grp("grp", [q("inside", options=["yes"])]),
                q("q_after", qtype="text", options=None,
                  gate=exists("grp", "grp.inside", "yes")),
            ]
        )
        closed_rows = index(
            evaluate_snapshot(
                definition,
                make_snapshot(root(groups={"grp": ilist(closed=True)}), definition),
            )
        )
        open_rows = index(
            evaluate_snapshot(
                definition,
                make_snapshot(root(groups={"grp": ilist(closed=False)}), definition),
            )
        )
        self.assertIs(closed_rows[("q_after", "")].visibility, FALSE)
        self.assertIs(open_rows[("q_after", "")].visibility, UNKNOWN)
        # 两种空组都不造假实例行。
        self.assertEqual(
            [r.static_path for r in evaluate_snapshot(
                definition,
                make_snapshot(root(groups={"grp": ilist(closed=False)}), definition))],
            ["q_after"],
        )

    def test_same_named_question_across_versions_does_not_share_answer(self):
        # 同静态路径 q1、同缓存值 3：v1 合法，v2 非法（合法值集合由版本确定）。
        v1 = make_definition([q("q1", options=[1, 2, 3])], version="v1")
        v2 = make_definition([q("q1", options=[1, 2])], version="v2")
        raw_snapshot = {
            "submission_id": "sub-x",
            "version": "v1",
            "root": {"answers": {"q1": 3}},
        }
        from pipeline.validate import load_snapshot

        rows_v1 = index(evaluate_snapshot(v1, load_snapshot(raw_snapshot, v1)))
        self.assertEqual(rows_v1[("q1", "")].answer_status, ANSWERED)

        raw_snapshot_v2 = dict(raw_snapshot, version="v2")
        rows_v2 = index(
            evaluate_snapshot(v2, load_snapshot(raw_snapshot_v2, v2))
        )
        self.assertEqual(rows_v2[("q1", "")].answer_status, ILLEGAL_CACHED)
        self.assertEqual(rows_v2[("q1", "")].version, "v2")
        # 两份事实各自带版本，值不串用。
        self.assertNotEqual(
            rows_v1[("q1", "")].answer_status,
            rows_v2[("q1", "")].answer_status,
        )

    def test_batch_isolates_bad_definition_and_bad_snapshot(self):
        good_def = {"version": "gd", "nodes": [{"id": "q1", "type": "choice",
                                                 "options": ["a"]}]}
        bad_def = {"version": "bd", "nodes": [
            {"id": "q1", "type": "choice", "options": ["a"],
             "gate": {"op": "eq", "ref": "missing", "value": "a"}}]}
        good_snap = {"submission_id": "good-sub", "version": "gd",
                     "root": {"answers": {"q1": "a"}}}
        bad_snap = {"submission_id": "bad-sub", "version": "gd",
                    "root": {"answers": {"unknown_q": 1}}}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            d_good = tmp_path / "good.json"
            d_bad = tmp_path / "bad.json"
            s_good = tmp_path / "good-snap.json"
            s_bad = tmp_path / "bad-snap.json"
            for path, payload in [
                (d_good, good_def),
                (d_bad, bad_def),
                (s_good, good_snap),
                (s_bad, bad_snap),
            ]:
                path.write_text(json.dumps(payload, ensure_ascii=False),
                                encoding="utf-8")

            outputs, report = run_batch([d_good, d_bad], [s_good, s_bad])

        # 坏定义整体拒绝，但好定义下的好快照照常产出。
        self.assertIn(str(d_bad), report.definition_errors)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]["submission_id"], "good-sub")
        # 坏快照只拒绝自己，不连累好快照。
        self.assertIn(str(s_bad), report.snapshot_errors)
        self.assertEqual(report.processed, ["good-sub"])


if __name__ == "__main__":
    unittest.main()
