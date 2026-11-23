#!/usr/bin/env python3
"""面板库结果查询助手（只读）。

用法：
  python -I examples/panel/query_panel.py <panel.db> [--family HH-1] [--member M_ALICE]

直接打印 panel_member（逐人双波次转移明细）与 panel_summary（转移格人数）。
也可以直接用 sqlite3 标准库执行 SQL：

  import sqlite3
  conn = sqlite3.connect("panel.db")
  conn.execute("SELECT * FROM panel_member").fetchall()
  conn.execute("SELECT * FROM panel_summary").fetchall()
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


MEMBER_COLUMNS = (
    "family_id",
    "member_id",
    "first_state_kind",
    "first_state_value",
    "second_state_kind",
    "second_state_value",
    "first_revision",
    "first_submission_id",
    "first_version",
    "first_static_path",
    "first_instance_path",
    "second_revision",
    "second_submission_id",
    "second_version",
    "second_static_path",
    "second_instance_path",
)

SUMMARY_COLUMNS = (
    "first_state_kind",
    "first_state_value",
    "second_state_kind",
    "second_state_value",
    "person_count",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="查询面板库两张结果表")
    parser.add_argument("db", help="init-panel 建立的 SQLite 库")
    parser.add_argument("--family", help="按家庭 ID 过滤明细")
    parser.add_argument("--member", help="按稳定成员 ID 过滤明细")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        where: list[str] = []
        params: list[str] = []
        if args.family:
            where.append("family_id = ?")
            params.append(args.family)
        if args.member:
            where.append("member_id = ?")
            params.append(args.member)
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        print("== panel_member：双波次转移明细（家庭 + 稳定成员唯一） ==")
        member_sql = (
            f"SELECT {', '.join(MEMBER_COLUMNS)} FROM panel_member{clause} "
            "ORDER BY family_id, member_id"
        )
        for row in conn.execute(member_sql, params):
            print(dict(row))

        print("\n== panel_summary：转移格人数（不保留零人数格） ==")
        for row in conn.execute(
            f"SELECT {', '.join(SUMMARY_COLUMNS)} FROM panel_summary "
            "ORDER BY first_state_kind, first_state_value, "
            "second_state_kind, second_state_value"
        ):
            print(dict(row))

        detail_total = conn.execute(
            f"SELECT COUNT(*) FROM panel_member{clause}", params
        ).fetchone()[0]
        print(
            f"\n明细人数: {detail_total}；"
            f"全库汇总人数: "
            f"{conn.execute('SELECT COALESCE(SUM(person_count), 0) FROM panel_summary').fetchone()[0]}"
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
