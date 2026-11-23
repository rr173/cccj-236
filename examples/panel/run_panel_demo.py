#!/usr/bin/env python3
"""可复跑的 panel-apply 端到端演示（以隔离模式运行：python -I）。

场景严格对应交付要求：

  1. 同一个本地成员位置 grp_members#m1 在首访/回访指向**不同的稳定成员**，
     且两个问卷版本用同一代码 1 表示**不同类别**；
  2. 随后替换首访一波：原有成员退出、另一成员加入（本地行号被重用），
     验证转移人数与逐人明细一起变化；
  3. 撤回后旧修订不能复活（低修订重送被水位挡住）；
  4. 一批中混入同修订冲突时，整批回滚、先前结果保持不变。

脚本在独立临时目录中生成全部输入并重建 SQLite 库，可反复运行。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.cli import main as cli_main  # noqa: E402
from pipeline.panel import (  # noqa: E402
    KIND_CATEGORY,
    KIND_NO_OBSERVATION,
    KIND_REASON,
    REASON_NOT_PRESENTED,
    REASON_PRESENTED_UNANSWERED,
    REASON_UNDERTERMINED,
    REASON_UNMAPPED,
    WAVE_FIRST,
    WAVE_SECOND,
)

STATIC_PATH = "grp_members.q_live"


# ---------------------------------------------------------------------------
# 事实 JSON 构造辅助（结构与 one/batch 产出的 facts JSON 完全一致）
# ---------------------------------------------------------------------------


def fact_row(instance_path: str, *, version: str, submission_id: str,
             visibility: str = "true", answer_status: str = "answered",
             answer_value: object = None) -> dict[str, object]:
    return {
        "version": version,
        "submission_id": submission_id,
        "static_path": STATIC_PATH,
        "instance_path": instance_path,
        "visibility": visibility,
        "answer_status": answer_status,
        "answer_value": answer_value,
        "reason": None,
        "list_state": None,
        "evidence": [],
    }


def facts(version: str, submission_id: str,
          rows: list[dict[str, object]]) -> dict[str, object]:
    for row in rows:  # 行级标识与顶层保持一致。
        row["version"] = version
        row["submission_id"] = submission_id
    return {
        "submission_id": submission_id,
        "version": version,
        "row_count": len(rows),
        "facts": rows,
    }


def crosswalk(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [
        {"instance_path": instance_path, "member_id": member_id}
        for instance_path, member_id in pairs
    ]


def replace(family_id: str, wave: str, revision: int,
            facts_payload: dict[str, object],
            crosswalk_payload: list[dict[str, str]]) -> dict[str, object]:
    return {
        "family_id": family_id,
        "wave": wave,
        "revision": revision,
        "operation": "replace",
        "facts": facts_payload,
        "crosswalk": crosswalk_payload,
    }


def withdraw(family_id: str, wave: str, revision: int) -> dict[str, object]:
    return {
        "family_id": family_id,
        "wave": wave,
        "revision": revision,
        "operation": "withdraw",
    }


def write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 查询助手（等价于直接执行 SQL；本机无 sqlite3 CLI 时用标准库展示）
# ---------------------------------------------------------------------------


MEMBER_SQL = """
SELECT family_id, member_id,
       first_state_kind, first_state_value,
       second_state_kind, second_state_value,
       first_submission_id, first_instance_path,
       second_submission_id, second_instance_path
FROM panel_member
ORDER BY family_id, member_id
"""

SUMMARY_SQL = """
SELECT first_state_kind, first_state_value,
       second_state_kind, second_state_value, person_count
FROM panel_summary
ORDER BY first_state_value, second_state_value
"""


def show_results(db_path: Path, title: str) -> None:
    print(f"\n===== {title} =====")
    conn = sqlite3.connect(str(db_path))
    print("-- panel_member（逐人转移明细）--")
    for row in conn.execute(MEMBER_SQL):
        print(" ", row)
    print("-- panel_summary（转移格人数，无零人数格）--")
    for row in conn.execute(SUMMARY_SQL):
        print(" ", row)
    total_detail = conn.execute("SELECT COUNT(*) FROM panel_member").fetchone()[0]
    total_summary = conn.execute(
        "SELECT COALESCE(SUM(person_count), 0) FROM panel_summary"
    ).fetchone()[0]
    print(f"-- 明细人数 {total_detail} == 汇总人数 {total_summary} --")
    conn.close()


def run_cli(*argv: str) -> int:
    print("\n$ python -I run_pipeline.py " + " ".join(argv))
    return cli_main(list(argv))


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="panel-demo-"))
    print(f"演示工作目录（独立、可删除）: {workdir}")
    db_path = workdir / "panel.db"
    mapping_path = ROOT / "examples" / "panel" / "mapping.json"

    # 0) 建库：映射从此固定。
    rc = run_cli("init-panel", "--db", str(db_path),
                 "--mapping", str(mapping_path))
    assert rc == 0

    # 1) 第一批：两波各一条替换。
    #    同一本地位置 grp_members#m1：首访是 M_ALICE（v3 代码 1 -> farming），
    #    回访是 M_BOB（v4 代码 1 -> paid_labor）。另含各类原因状态成员。
    batch1 = [
        replace(
            "HH-1", WAVE_FIRST, 1,
            facts(
                "screening-v3", "sub-first-1",
                [
                    fact_row("grp_members#m1", version="screening-v3",
                             submission_id="sub-first-1", answer_value=1),
                    fact_row("grp_members#m2", version="screening-v3",
                             submission_id="sub-first-1", answer_value=2),
                    fact_row("grp_members#m3", version="screening-v3",
                             submission_id="sub-first-1", answer_value=99),
                    fact_row("grp_members#m4", version="screening-v3",
                             submission_id="sub-first-1",
                             visibility="false",
                             answer_status="not_asked_cached",
                             answer_value=1),
                    fact_row("grp_members#m5", version="screening-v3",
                             submission_id="sub-first-1",
                             visibility="unknown",
                             answer_status="undetermined_cached",
                             answer_value=2),
                    fact_row("grp_members#m6", version="screening-v3",
                             submission_id="sub-first-1",
                             answer_status="illegal_cached_value",
                             answer_value="fax"),
                ],
            ),
            crosswalk(
                ("grp_members#m1", "M_ALICE"),
                ("grp_members#m2", "M_CAROL"),
                ("grp_members#m3", "M_DAVE"),
                ("grp_members#m4", "M_ELLA"),
                ("grp_members#m5", "M_FINN"),
                ("grp_members#m6", "M_GINA"),
            ),
        ),
        replace(
            "HH-1", WAVE_SECOND, 1,
            facts(
                "screening-v4", "sub-second-1",
                [
                    # 同一本地位置 m1，回访对应的是另一个人 M_BOB。
                    fact_row("grp_members#m1", version="screening-v4",
                             submission_id="sub-second-1", answer_value=1),
                    fact_row("grp_members#m2", version="screening-v4",
                             submission_id="sub-second-1", answer_value=3),
                ],
            ),
            crosswalk(
                ("grp_members#m1", "M_BOB"),
                ("grp_members#m2", "M_CAROL"),
            ),
        ),
    ]
    batch1_path = workdir / "batch-1.json"
    write_json(batch1_path, batch1)
    rc = run_cli("panel-apply", "--db", str(db_path),
                 "--mapping", str(mapping_path),
                 "--updates", str(batch1_path))
    assert rc == 0
    show_results(db_path, "场景 1：位置重用 + 同代码跨版本不同类别 + 原因状态")

    # 断言关键语义。
    conn = sqlite3.connect(str(db_path))
    alice = conn.execute(
        "SELECT first_state_kind, first_state_value, second_state_kind "
        "FROM panel_member WHERE member_id='M_ALICE'"
    ).fetchone()
    bob = conn.execute(
        "SELECT second_state_kind, second_state_value, first_state_kind "
        "FROM panel_member WHERE member_id='M_BOB'"
    ).fetchone()
    assert alice == (KIND_CATEGORY, "farming", KIND_NO_OBSERVATION), alice
    assert bob == (KIND_CATEGORY, "paid_labor", KIND_NO_OBSERVATION), bob
    reason_expect = {
        "M_DAVE": REASON_UNMAPPED,
        "M_ELLA": REASON_NOT_PRESENTED,
        "M_FINN": REASON_UNDERTERMINED,
        "M_GINA": REASON_PRESENTED_UNANSWERED,
    }
    for member_id, reason in reason_expect.items():
        kind, value = conn.execute(
            "SELECT first_state_kind, first_state_value "
            "FROM panel_member WHERE member_id=?",
            (member_id,),
        ).fetchone()
        assert (kind, value) == (KIND_REASON, reason), (member_id, kind, value)
    conn.close()

    # 2) 替换首访（修订 2）：M_CAROL 退出、M_HUGO 加入，本地位置 m2 被重用。
    batch2 = [
        replace(
            "HH-1", WAVE_FIRST, 2,
            facts(
                "screening-v3", "sub-first-2",
                [
                    fact_row("grp_members#m1", version="screening-v3",
                             submission_id="sub-first-2", answer_value=1),
                    fact_row("grp_members#m2", version="screening-v3",
                             submission_id="sub-first-2", answer_value=2),
                ],
            ),
            crosswalk(
                ("grp_members#m1", "M_ALICE"),
                ("grp_members#m2", "M_HUGO"),
            ),
        )
    ]
    batch2_path = workdir / "batch-2.json"
    write_json(batch2_path, batch2)
    rc = run_cli("panel-apply", "--db", str(db_path),
                 "--mapping", str(mapping_path),
                 "--updates", str(batch2_path))
    assert rc == 0
    show_results(db_path, "场景 2：替换首访，CAROL 退出、HUGO 加入")
    conn = sqlite3.connect(str(db_path))
    members = {
        row[0]: row[1:]
        for row in conn.execute(
            "SELECT member_id, first_state_kind, second_state_kind "
            "FROM panel_member WHERE family_id='HH-1' ORDER BY member_id"
        )
    }
    assert "M_CAROL" in members and "M_HUGO" in members
    # CAROL 首访端变无观测（她仍在回访）；HUGO 回访端无观测。
    assert members["M_CAROL"] == (KIND_NO_OBSERVATION, KIND_CATEGORY), members["M_CAROL"]
    assert members["M_HUGO"] == (KIND_CATEGORY, KIND_NO_OBSERVATION), members["M_HUGO"]
    detail_total = conn.execute(
        "SELECT COUNT(*) FROM panel_member"
    ).fetchone()[0]
    summary_total = conn.execute(
        "SELECT COALESCE(SUM(person_count), 0) FROM panel_summary"
    ).fetchone()[0]
    assert detail_total == summary_total
    conn.close()

    # 3) 撤回首访（修订 3），再用旧修订 1/2 重送：均不能复活。
    batch3 = [withdraw("HH-1", WAVE_FIRST, 3)]
    batch3_path = workdir / "batch-3.json"
    write_json(batch3_path, batch3)
    rc = run_cli("panel-apply", "--db", str(db_path),
                 "--mapping", str(mapping_path),
                 "--updates", str(batch3_path))
    assert rc == 0

    stale_path = workdir / "batch-stale.json"
    write_json(stale_path, [batch1[0]])  # 修订 1 的旧替换
    rc = run_cli("panel-apply", "--db", str(db_path),
                 "--mapping", str(mapping_path),
                 "--updates", str(stale_path))
    assert rc == 0  # 低修订被忽略，不是错误
    conn = sqlite3.connect(str(db_path))
    watermark = conn.execute(
        "SELECT revision, status FROM panel_watermark "
        "WHERE family_id='HH-1' AND wave='first'"
    ).fetchone()
    first_wave_rows = conn.execute(
        "SELECT COUNT(*) FROM panel_wave_state "
        "WHERE family_id='HH-1' AND wave='first'"
    ).fetchone()[0]
    assert watermark == (3, "withdrawn"), watermark
    assert first_wave_rows == 0
    # 明细中首访端应全部为无观测。
    bad = conn.execute(
        "SELECT COUNT(*) FROM panel_member WHERE first_state_kind <> ?",
        (KIND_NO_OBSERVATION,),
    ).fetchone()[0]
    assert bad == 0
    conn.close()
    show_results(db_path, "场景 3：撤回后旧修订重送不能复活（水位保留）")

    # 4) 同批混入同修订冲突：整批回滚，先前结果不变。
    snapshot_before = Path(tempfile.mkdtemp()) / "snapshot.db"
    sqlite3.connect(str(snapshot_before)).close()
    import shutil
    shutil.copy2(db_path, snapshot_before)

    good_update = replace(
        "HH-1", WAVE_FIRST, 4,
        facts(
            "screening-v3", "sub-first-4",
            [fact_row("grp_members#m1", version="screening-v3",
                      submission_id="sub-first-4", answer_value=1)],
        ),
        crosswalk(("grp_members#m1", "M_ALICE")),
    )
    conflicting = replace(
        "HH-1", WAVE_SECOND, 1,  # 回访修订 1 已接受过不同内容
        facts(
            "screening-v4", "sub-second-conflict",
            [fact_row("grp_members#m1", version="screening-v4",
                      submission_id="sub-second-conflict", answer_value=3)],
        ),
        crosswalk(("grp_members#m1", "M_BOB")),
    )
    conflict_path = workdir / "batch-conflict.json"
    write_json(conflict_path, [good_update, conflicting])
    rc = run_cli("panel-apply", "--db", str(db_path),
                 "--mapping", str(mapping_path),
                 "--updates", str(conflict_path))
    assert rc == 5  # 整批拒绝
    with sqlite3.connect(str(db_path)) as after, \
            sqlite3.connect(str(snapshot_before)) as before:
        for table in ("panel_watermark", "panel_wave_state",
                      "panel_member", "panel_summary"):
            self_rows = after.execute(
                f"SELECT * FROM {table} ORDER BY 1, 2, 3, 4"
            ).fetchall()
            before_rows = before.execute(
                f"SELECT * FROM {table} ORDER BY 1, 2, 3, 4"
            ).fetchall()
            assert self_rows == before_rows, f"{table} 在冲突批次后发生变化"
    show_results(db_path, "场景 4：冲突批次整批回滚，先前结果保持不变")

    print("\n全部演示断言通过。")
    print(f"数据库保留在: {db_path}")
    print("\n直接查询两张结果表（示例）：")
    print("  python3 -I examples/panel/query_panel.py " + str(db_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
