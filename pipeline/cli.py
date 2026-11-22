"""命令行入口（离线管线，不涉及任何表单前端/收集系统）。

用法：
  python -I -m pipeline.cli batch \
      --definitions defs/*.json --snapshots snaps/*.json \
      --out-dir out/
  python -I -m pipeline.cli one --definition def.json --snapshot snap.json

输出：每份提交一个 <submission_id>.facts.json，外加 batch_report.json。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 兼容 python -I：隔离模式不自动把仓库根放入 sys.path。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.batch import run_batch
from pipeline.engine import evaluate_snapshot
from pipeline.errors import DefinitionRejected, SnapshotRejected
from pipeline.serialize import facts_to_dict
from pipeline.validate import load_definition, load_snapshot_file


def _cmd_one(args: argparse.Namespace) -> int:
    try:
        definition = load_definition(args.definition)
    except DefinitionRejected as exc:
        for err in exc.errors:
            print(f"定义被拒绝: {err}", file=sys.stderr)
        return 2
    try:
        snapshot = load_snapshot_file(args.snapshot, definition)
    except SnapshotRejected as exc:
        for err in exc.errors:
            print(f"快照被拒绝: {err}", file=sys.stderr)
        return 3
    rows = evaluate_snapshot(definition, snapshot)
    payload = facts_to_dict(snapshot.submission_id, definition.version, rows)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_batch(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs, report = run_batch(args.definitions, args.snapshots)
    for payload in outputs:
        out_path = out_dir / f"{payload['submission_id']}.facts.json"
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    with open(out_dir / "batch_report.json", "w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2)

    print(
        f"已处理 {len(outputs)} 份提交；"
        f"拒绝定义 {len(report.definition_errors)} 份；"
        f"拒绝快照 {len(report.snapshot_errors)} 份。报告: {out_dir / 'batch_report.json'}"
    )
    return 0 if not report.definition_errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="离线问卷事实裁决管线")
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("one", help="单定义 + 单快照，事实表打印到 stdout")
    one.add_argument("--definition", required=True)
    one.add_argument("--snapshot", required=True)
    one.set_defaults(func=_cmd_one)

    batch = sub.add_parser("batch", help="多定义多快照批处理")
    batch.add_argument("--definitions", nargs="+", required=True)
    batch.add_argument("--snapshots", nargs="+", required=True)
    batch.add_argument("--out-dir", required=True)
    batch.set_defaults(func=_cmd_batch)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
