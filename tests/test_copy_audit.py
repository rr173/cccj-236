"""copy-audit 测试：小样本簇/线索、全有或全无的错误处理、确定性、性能形状。"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pipeline.cli import main as cli_main
from pipeline.copyaudit import run_copy_audit

Q5 = ["q1", "q2", "q3", "q4", "q5"]
Q8 = [f"q{i}" for i in range(1, 9)]


def make_row(
    sub_id: str,
    static_path: str,
    value: Any = None,
    *,
    visibility: str = "true",
    status: str = "answered",
    version: str = "v1",
    instance_path: str = "",
) -> dict[str, Any]:
    """结构与 one/batch 产出的事实行一致。"""
    return {
        "version": version,
        "submission_id": sub_id,
        "static_path": static_path,
        "instance_path": instance_path,
        "visibility": visibility,
        "answer_status": status,
        "answer_value": value,
        "reason": None,
        "list_state": None,
        "evidence": [],
    }


def payload(sub_id: str, rows: list[dict[str, Any]], *, version: str = "v1"):
    return {
        "submission_id": sub_id,
        "version": version,
        "row_count": len(rows),
        "facts": rows,
    }


def answers_payload(
    sub_id: str,
    answers: dict[str, Any],
    *,
    version: str = "v1",
    extra_rows: list[dict[str, Any]] | None = None,
):
    rows = [
        make_row(sub_id, path, value, version=version)
        for path, value in answers.items()
    ]
    if extra_rows:
        rows.extend(extra_rows)
    return payload(sub_id, rows, version=version)


class CopyAuditTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "listing.json"

    def write(self, name: str, data: Any) -> Path:
        path = self.dir / name
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return path

    def run_cli(self, *argv: str) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = cli_main(list(argv))
        return rc, stderr.getvalue()

    def run_audit(self, *questions: str) -> tuple[int, str]:
        return self.run_cli(
            "copy-audit", "--facts-dir", str(self.dir),
            "--questions", *(questions or Q5), "--out", str(self.out),
        )

    def read_listing(self) -> dict[str, Any]:
        return json.loads(self.out.read_text(encoding="utf-8"))

    def build_sample(self, *, omit_b_q1: bool = False) -> None:
        """三份答卷：只有相邻两对（a-b、b-c）成立直接线索。

        a = (1,1,1,1,1)；b = (2,1,1,1,1)；c = (2,2,1,1,1)。
        a-c 在 q1/q2 上有两处真实分歧 -> 不是线索。
        omit_b_q1 时移除 b 的 q1 有效作答（变为问了没答 -> 缺项）。
        """
        self.write(
            "sub-a.facts.json",
            answers_payload("sub-a", {q: 1 for q in Q5}),
        )
        b_answers = {"q1": 2, **{q: 1 for q in Q5[1:]}}
        b_extra = []
        if omit_b_q1:
            del b_answers["q1"]
            b_extra.append(
                make_row("sub-b", "q1", None, status="displayed_unanswered")
            )
        self.write(
            "sub-b.facts.json",
            answers_payload("sub-b", b_answers, extra_rows=b_extra),
        )
        self.write(
            "sub-c.facts.json",
            answers_payload("sub-c", {"q1": 2, "q2": 2, **{q: 1 for q in Q5[2:]}}),
        )

    def clue_index(self, listing: dict[str, Any]) -> dict[tuple[str, str], Any]:
        return {
            tuple(clue["pair"]): clue
            for cluster in listing["clusters"]
            for clue in cluster["clues"]
        }


class SmallSampleTests(CopyAuditTestBase):
    def test_adjacent_pairs_only(self):
        self.build_sample()
        rc, stderr = self.run_audit()
        self.assertEqual(rc, 0, stderr)
        listing = self.read_listing()
        self.assertEqual(listing["version"], "v1")
        self.assertEqual(listing["submission_count"], 3)
        self.assertEqual(listing["cluster_count"], 1)
        self.assertEqual(listing["clue_count"], 2)
        cluster = listing["clusters"][0]
        # 沿直接线索互相到达的三份答卷归为一簇。
        self.assertEqual(cluster["members"], ["sub-a", "sub-b", "sub-c"])
        clues = self.clue_index(listing)
        self.assertEqual(set(clues), {("sub-a", "sub-b"), ("sub-b", "sub-c")})
        self.assertEqual(
            clues[("sub-a", "sub-b")]["same_questions"],
            ["q2", "q3", "q4", "q5"],
        )
        self.assertEqual(
            clues[("sub-a", "sub-b")]["different_questions"], ["q1"]
        )
        self.assertEqual(
            clues[("sub-b", "sub-c")]["same_questions"],
            ["q1", "q3", "q4", "q5"],
        )
        self.assertEqual(
            clues[("sub-b", "sub-c")]["different_questions"], ["q2"]
        )
        # 间接相连的 sub-a/sub-c 不得写成直接线索（两处真实分歧被排除）。
        self.assertNotIn(("sub-a", "sub-c"), clues)

    def test_removal_of_one_answer_changes_cluster_and_clues(self):
        self.build_sample()
        rc, stderr = self.run_audit()
        self.assertEqual(rc, 0, stderr)
        before = self.read_listing()

        # 移除 sub-b 的一项有效作答（q1 变为问了没答 -> 缺项）。
        (self.dir / "sub-b.facts.json").unlink()
        self.build_sample(omit_b_q1=True)
        rc, stderr = self.run_audit()
        self.assertEqual(rc, 0, stderr)
        after = self.read_listing()

        # 簇与线索同时变化：sub-c 变孤立答卷退出清单，b-c 线索消失。
        self.assertNotEqual(before, after)
        self.assertEqual(after["cluster_count"], 1)
        self.assertEqual(after["clue_count"], 1)
        self.assertEqual(after["clusters"][0]["members"], ["sub-a", "sub-b"])
        clue = after["clusters"][0]["clues"][0]
        self.assertEqual(clue["pair"], ["sub-a", "sub-b"])
        # 一侧缺项不计为相同或不同：a-b 仍有 4 题相同、0 题不同。
        self.assertEqual(clue["same_questions"], ["q2", "q3", "q4", "q5"])
        self.assertEqual(clue["different_questions"], [])

    def test_two_real_differences_vs_one_side_missing(self):
        self.build_sample()
        # sub-d 缺 q5（问了没答），其余四题与 sub-a 全同。
        d_rows = [make_row("sub-d", q, 1) for q in Q5[:4]]
        d_rows.append(
            make_row("sub-d", "q5", None, status="displayed_unanswered")
        )
        self.write("sub-d.facts.json", payload("sub-d", d_rows))
        rc, stderr = self.run_audit()
        self.assertEqual(rc, 0, stderr)
        listing = self.read_listing()
        clues = self.clue_index(listing)
        # 一侧缺项不算分歧：a-d 成立直接线索（4 题相同、0 题不同）。
        self.assertIn(("sub-a", "sub-d"), clues)
        self.assertEqual(clues[("sub-a", "sub-d")]["different_questions"], [])
        self.assertEqual(
            clues[("sub-a", "sub-d")]["same_questions"],
            ["q1", "q2", "q3", "q4"],
        )
        # 两处真实分歧：a-c 不是线索（缺项与分歧在回放中可分辨）。
        self.assertNotIn(("sub-a", "sub-c"), clues)
        # 全部四份答卷沿线索可达，归为一簇。
        self.assertEqual(
            listing["clusters"][0]["members"],
            ["sub-a", "sub-b", "sub-c", "sub-d"],
        )

    def test_cluster_lists_all_direct_clues_not_just_connecting_ones(self):
        # 三份完全相同的答卷互为直接线索：簇内必须列出全部 3 条，
        # 而不是只输出把成员连起来的 2 条。
        qs = ["q1", "q2", "q3", "q4"]
        for name in ("sub-a", "sub-b", "sub-c"):
            self.write(
                f"{name}.facts.json",
                answers_payload(name, {q: "s3cr3t-value" for q in qs}),
            )
        # 孤立答卷不入清单。
        self.write(
            "sub-z.facts.json", answers_payload("sub-z", {q: 2 for q in qs})
        )
        rc, stderr = self.run_audit(*qs)
        self.assertEqual(rc, 0, stderr)
        listing = self.read_listing()
        self.assertEqual(listing["cluster_count"], 1)
        self.assertEqual(listing["clue_count"], 3)
        cluster = listing["clusters"][0]
        self.assertEqual(cluster["members"], ["sub-a", "sub-b", "sub-c"])
        self.assertEqual(
            {tuple(c["pair"]) for c in cluster["clues"]},
            {("sub-a", "sub-b"), ("sub-a", "sub-c"), ("sub-b", "sub-c")},
        )
        # 清单不复制答案或原始证据值。
        raw = self.out.read_text(encoding="utf-8")
        self.assertNotIn("s3cr3t-value", raw)
        self.assertNotIn("answer_value", raw)
        self.assertNotIn("evidence", raw)

    def test_typed_scalar_equality(self):
        # 布尔 true、整数 1、浮点 1.0、字符串 "1" 互不混认。
        qs = ["q1", "q2", "q3", "q4"]
        self.write("a.facts.json", answers_payload("sub-int", {q: 1 for q in qs}))
        self.write(
            "b.facts.json", answers_payload("sub-str", {q: "1" for q in qs})
        )
        self.write(
            "c.facts.json", answers_payload("sub-float", {q: 1.0 for q in qs})
        )
        self.write(
            "d.facts.json", answers_payload("sub-bool", {q: True for q in qs})
        )
        self.write(
            "e.facts.json", answers_payload("sub-str2", {q: "1" for q in qs})
        )
        rc, stderr = self.run_audit(*qs)
        self.assertEqual(rc, 0, stderr)
        listing = self.read_listing()
        self.assertEqual(listing["clue_count"], 1)
        self.assertEqual(
            listing["clusters"][0]["members"], ["sub-str", "sub-str2"]
        )


class QuestionArgTests(CopyAuditTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.write(
            "sub-a.facts.json", answers_payload("sub-a", {q: 1 for q in Q5})
        )

    def assert_arg_error(self, *questions: str) -> None:
        rc, stderr = self.run_audit(*questions)
        self.assertEqual(rc, 6)
        self.assertIn("QUESTION_ARGS", stderr)
        self.assertFalse(self.out.exists())

    def test_too_few_questions(self):
        self.assert_arg_error("q1", "q2", "q3")

    def test_too_many_questions(self):
        self.assert_arg_error(*[f"q{i}" for i in range(1, 10)])

    def test_duplicate_questions(self):
        self.assert_arg_error("q1", "q2", "q3", "q4", "q4")

    def test_non_root_question_path(self):
        self.assert_arg_error("q1", "q2", "q3", "grp.q4")
        self.assert_arg_error("q1", "q2", "q3", "q4#i1")

    def test_empty_question_path(self):
        self.assert_arg_error("q1", "q2", "q3", "")

    def test_exactly_four_and_eight_questions_accepted(self):
        for qs in (Q5[:4], Q8):
            rc, stderr = self.run_audit(*qs)
            self.assertEqual(rc, 0, stderr)
            self.assertTrue(self.out.exists())
            self.out.unlink()


class InputErrorTests(CopyAuditTestBase):
    """任一输入问题：指出出错文件、退出码 6、已有清单保持不变。"""

    def setUp(self) -> None:
        super().setUp()
        # 每份文件都含一份合法答卷，再按需破坏其中一份。
        self.write(
            "good.facts.json", answers_payload("sub-good", {q: 1 for q in Q5})
        )

    def assert_failure(self, bad_file: str, code: str) -> str:
        # 预置一份已有清单：失败时它必须原样保留。
        self.out.write_text('{"sentinel": true}\n', encoding="utf-8")
        rc, stderr = self.run_audit()
        self.assertEqual(rc, 6, stderr)
        self.assertIn(code, stderr)
        self.assertIn(bad_file, stderr)
        self.assertEqual(
            self.out.read_text(encoding="utf-8"), '{"sentinel": true}\n'
        )
        return stderr

    def test_unparseable_json(self):
        self.write("broken.facts.json", "{not json")
        self.assert_failure("broken.facts.json", "BAD_FACTS_FILE")

    def test_non_standard_json_constant(self):
        # 严格 JSON 不允许 NaN/Infinity：整份文件按坏事实文件拒绝。
        bad = answers_payload("sub-nan", {q: 1 for q in Q5})
        text = json.dumps(bad, ensure_ascii=False, indent=2).replace(
            '"answer_value": 1', '"answer_value": NaN'
        )
        self.assertIn("NaN", text)
        self.write("nan.facts.json", text)
        self.assert_failure("nan.facts.json", "BAD_FACTS_FILE")

    def test_top_level_not_object(self):
        self.write("list.facts.json", [1, 2, 3])
        self.assert_failure("list.facts.json", "BAD_FACTS_FILE")

    def test_facts_not_a_list(self):
        self.write(
            "shape.facts.json",
            {"submission_id": "sub-x", "version": "v1", "facts": {}},
        )
        self.assert_failure("shape.facts.json", "BAD_FACTS_FILE")

    def test_row_not_object(self):
        self.write(
            "rows.facts.json",
            {"submission_id": "sub-x", "version": "v1", "facts": ["oops"]},
        )
        self.assert_failure("rows.facts.json", "BAD_FACTS_FILE")

    def test_empty_submission_id(self):
        self.write(
            "empty.facts.json",
            {"submission_id": "", "version": "v1", "facts": []},
        )
        self.assert_failure("empty.facts.json", "EMPTY_SUBMISSION_ID")

    def test_missing_version(self):
        self.write(
            "noversion.facts.json", {"submission_id": "sub-x", "facts": []}
        )
        self.assert_failure("noversion.facts.json", "BAD_FACTS_FILE")

    def test_version_mismatch_across_files(self):
        self.write(
            "other.facts.json",
            answers_payload("sub-other", {q: 1 for q in Q5}, version="v2"),
        )
        self.assert_failure("other.facts.json", "VERSION_MISMATCH")

    def test_duplicate_submission_id(self):
        self.write(
            "aaa.facts.json", answers_payload("sub-dup", {q: 1 for q in Q5})
        )
        self.write(
            "zzz.facts.json", answers_payload("sub-dup", {q: 2 for q in Q5})
        )
        stderr = self.assert_failure("zzz.facts.json", "DUPLICATE_SUBMISSION")
        self.assertIn("aaa.facts.json", stderr)  # 同时指出首次出现的文件

    def test_row_header_id_mismatch(self):
        bad = answers_payload("sub-x", {q: 1 for q in Q5})
        bad["facts"][0]["submission_id"] = "sub-y"
        self.write("mismatch.facts.json", bad)
        self.assert_failure("mismatch.facts.json", "ROW_HEADER_MISMATCH")

    def test_row_header_version_mismatch(self):
        bad = answers_payload("sub-x", {q: 1 for q in Q5})
        bad["facts"][2]["version"] = "v9"
        self.write("rowver.facts.json", bad)
        self.assert_failure("rowver.facts.json", "ROW_HEADER_MISMATCH")

    def test_duplicate_selected_root_question(self):
        rows = [make_row("sub-x", q, 1) for q in Q5]
        rows.append(make_row("sub-x", "q3", 2))  # 同一所选根题第二次出现
        self.write("dup.facts.json", payload("sub-x", rows))
        self.assert_failure("dup.facts.json", "DUPLICATE_QUESTION_ROW")

    def test_accepted_row_without_scalar(self):
        for bad_value in (None, [1], {"x": 1}):
            rows = [make_row("sub-x", q, 1) for q in Q5]
            rows[1] = make_row("sub-x", "q2", bad_value)  # answered 但无标量
            self.write("noscalar.facts.json", payload("sub-x", rows))
            self.assert_failure("noscalar.facts.json", "ACCEPTED_WITHOUT_SCALAR")
            (self.dir / "noscalar.facts.json").unlink()
            self.out.unlink()

    def test_unaccepted_rows_cached_values_not_validated(self):
        # 未采信行的缓存值（哪怕是对象/数组/非法枚举外的奇怪值）一律不校验。
        extra = [
            make_row("sub-x", "q1", {"cached": "object"},
                     visibility="false", status="not_asked_cached"),
            make_row("sub-x", "q2", [1, 2],
                     visibility="unknown", status="undetermined_cached"),
            make_row("sub-x", "q3", "fax", status="illegal_cached_value"),
        ]
        rows = [make_row("sub-x", q, 1) for q in ("q4", "q5")] + extra
        self.write("cached.facts.json", payload("sub-x", rows))
        rc, stderr = self.run_audit()
        self.assertEqual(rc, 0, stderr)
        # q1~q3 均未采信 -> 该答卷只有 q4/q5 两题作答，无任何线索。
        listing = self.read_listing()
        self.assertEqual(listing["clue_count"], 0)
        self.assertEqual(listing["clusters"], [])

    def test_non_selected_rows_not_audited(self):
        # 非所选题的行不参与核查（只校验行与表头一致）。
        extra = [
            make_row("sub-x", "q_other", {"odd": 1}, visibility="bogus",
                     status="whatever"),
            make_row("sub-x", "q1", 9, instance_path="grp#i1"),  # 非根实例
        ]
        rows = [make_row("sub-x", q, 1) for q in Q5] + extra
        self.write("extra.facts.json", payload("sub-x", rows))
        rc, stderr = self.run_audit()
        self.assertEqual(rc, 0, stderr)

    def test_bad_visibility_on_selected_root_row(self):
        rows = [make_row("sub-x", q, 1) for q in Q5]
        rows[0]["visibility"] = "yes"
        self.write("badvis.facts.json", payload("sub-x", rows))
        self.assert_failure("badvis.facts.json", "BAD_FACTS_FILE")

    def test_missing_facts_dir(self):
        self.out.write_text('{"sentinel": true}\n', encoding="utf-8")
        rc, stderr = self.run_cli(
            "copy-audit", "--facts-dir", str(self.dir / "nope"),
            "--questions", *Q5, "--out", str(self.out),
        )
        self.assertEqual(rc, 6)
        self.assertIn("DIR_INVALID", stderr)
        self.assertEqual(
            self.out.read_text(encoding="utf-8"), '{"sentinel": true}\n'
        )

    def test_no_facts_files(self):
        (self.dir / "good.facts.json").unlink()
        rc, stderr = self.run_audit()
        self.assertEqual(rc, 6)
        self.assertIn("NO_FACTS_FILES", stderr)


class IgnoredFilesTests(CopyAuditTestBase):
    def test_only_first_level_facts_json_files_are_read(self):
        self.build_sample()
        # batch_report.json 即使内容垃圾也不读。
        self.write("batch_report.json", "{garbage")
        # 非 .facts.json 后缀不读。
        self.write("notes.txt", "{garbage")
        self.write("sub-x.facts.json.bak", "{garbage")
        # 子目录里的 .facts.json 不读（只看目录一级）。
        nested = self.dir / "nested"
        nested.mkdir()
        (nested / "sub-a.facts.json").write_text("{garbage", encoding="utf-8")
        rc, stderr = self.run_audit()
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(self.read_listing()["submission_count"], 3)


class DeterminismTests(CopyAuditTestBase):
    def test_listing_invariant_to_file_names_and_row_order(self):
        self.build_sample()
        rc, stderr = self.run_audit()
        self.assertEqual(rc, 0, stderr)
        first = self.out.read_bytes()

        # 同一批答卷：换文件名（改变读取顺序）、颠倒事实行顺序。
        for old in self.dir.glob("*.facts.json"):
            old.unlink()
        self.build_sample()
        renamed = {
            "sub-a.facts.json": "zz-a.facts.json",
            "sub-b.facts.json": "mm-b.facts.json",
            "sub-c.facts.json": "aa-c.facts.json",
        }
        for old_name, new_name in renamed.items():
            data = json.loads(
                (self.dir / old_name).read_text(encoding="utf-8")
            )
            data["facts"] = list(reversed(data["facts"]))
            self.write(new_name, data)
            (self.dir / old_name).unlink()
        rc, stderr = self.run_audit()
        self.assertEqual(rc, 0, stderr)
        second = self.out.read_bytes()
        self.assertEqual(first, second)


class PerformanceShapeTests(CopyAuditTestBase):
    """三道题答案极常见、其余近乎唯一时，不得逐对枚举全部答卷。"""

    def test_common_answers_do_not_force_pairwise_enumeration(self):
        n = 2000
        plant_src, plant_dst = 100, 1500
        for i in range(n):
            answers: dict[str, Any] = {"q1": "yes", "q2": 1, "q3": "A"}
            for k in range(4, 9):
                answers[f"q{k}"] = f"u-{i}-{k}"
            if i == plant_dst:  # 植入一对：整份照录 sub-00100 的作答
                for k in range(4, 9):
                    answers[f"q{k}"] = f"u-{plant_src}-{k}"
            self.write(f"sub-{i:05d}.facts.json",
                       answers_payload(f"sub-{i:05d}", answers))

        stats = run_copy_audit(str(self.dir), tuple(Q8), str(self.out))
        self.assertEqual(stats.submission_count, n)
        # 工作量证据：2,000 份共 1,999,000 个可能配对，只有植入的 1 对
        # 进入逐对核验；常见答案覆盖全轮也没有触发逐对枚举。
        self.assertEqual(stats.candidate_pair_count, 1)
        self.assertEqual(stats.clue_count, 1)
        self.assertEqual(stats.cluster_count, 1)
        listing = self.read_listing()
        cluster = listing["clusters"][0]
        self.assertEqual(cluster["members"], ["sub-00100", "sub-01500"])
        clue = cluster["clues"][0]
        self.assertEqual(clue["same_questions"], Q8)
        self.assertEqual(clue["different_questions"], [])


if __name__ == "__main__":
    unittest.main()
