"""panel-apply 命令行集成测试：走真实文件、真实 SQLite 库。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pipeline.cli import main as cli_main
from tests.test_panel import (
    crosswalk,
    fact_row,
    facts_payload,
    make_mapping,
    update,
)


class PanelCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "panel.db"
        self.mapping_path = self.dir / "mapping.json"
        self.mapping_path.write_text(
            json.dumps(make_mapping(), ensure_ascii=False), encoding="utf-8"
        )

    def cli(self, *argv: str) -> int:
        return cli_main(list(argv))

    def write_batch(self, name: str, payload: Any) -> Path:
        path = self.dir / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def test_full_cli_flow_init_apply_query_and_conflict_exit_code(self):
        # 1) 建库。
        self.assertEqual(
            self.cli("init-panel", "--db", str(self.db),
                     "--mapping", str(self.mapping_path)),
            0,
        )
        # 重复建库默认报错。
        self.assertEqual(
            self.cli("init-panel", "--db", str(self.db),
                     "--mapping", str(self.mapping_path)),
            4,
        )

        # 2) 入仓两波。
        batch = [
            update(
                "F1", "first", 1,
                facts=facts_payload([fact_row("g#m1", answer_value=1)]),
                members=crosswalk(("g#m1", "M_ALICE")),
            ),
            update(
                "F1", "second", 1,
                facts=facts_payload(
                    [fact_row("g#m1", version="v4", answer_value=1)],
                    version="v4",
                ),
                members=crosswalk(("g#m1", "M_ALICE")),
            ),
        ]
        batch_path = self.write_batch("b1.json", batch)
        report_path = self.dir / "report.json"
        self.assertEqual(
            self.cli(
                "panel-apply", "--db", str(self.db),
                "--mapping", str(self.mapping_path),
                "--updates", str(batch_path),
                "--report-out", str(report_path),
            ),
            0,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report[0]["applied"][0]["family_id"], "F1")

        conn = sqlite3.connect(str(self.db))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM panel_member").fetchone()[0], 1
        )
        self.assertEqual(
            conn.execute(
                "SELECT COALESCE(SUM(person_count), 0) FROM panel_summary"
            ).fetchone()[0],
            1,
        )
        conn.close()

        # 3) 冲突批次返回 5，且库内容不变。
        conflict = self.write_batch(
            "b2.json",
            [
                update(
                    "F1", "first", 1,
                    facts=facts_payload([fact_row("g#m1", answer_value=2)]),
                    members=crosswalk(("g#m1", "M_ALICE")),
                )
            ],
        )
        self.assertEqual(
            self.cli(
                "panel-apply", "--db", str(self.db),
                "--mapping", str(self.mapping_path),
                "--updates", str(conflict),
            ),
            5,
        )
        conn = sqlite3.connect(str(self.db))
        self.assertEqual(
            conn.execute(
                "SELECT first_state_value FROM panel_member"
            ).fetchone()[0],
            "v3_one",
        )
        conn.close()

    def test_unknown_db_returns_error_code(self):
        self.assertEqual(
            self.cli(
                "panel-apply", "--db", str(self.dir / "missing.db"),
                "--mapping", str(self.mapping_path),
                "--updates", str(self.write_batch("b.json", [])),
            ),
            4,
        )


if __name__ == "__main__":
    unittest.main()
