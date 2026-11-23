"""命令行入口（离线管线，不涉及任何表单前端/收集系统）。

用法：
  python -I -m pipeline.cli batch \
      --definitions defs/*.json --snapshots snaps/*.json \
      --out-dir out/
  python -I -m pipeline.cli one --definition def.json --snapshot snap.json
  python -I -m pipeline.cli init-panel --db panel.db --mapping mapping.json
  python -I -m pipeline.cli panel-apply --db panel.db --mapping mapping.json \
      --updates batch1.json batch2.json --report-out report.json
  python -I -m pipeline.cli copy-audit --facts-dir out/ \
      --questions q1 q2 q3 q4 q5 --out copy-audit.json

输出：每份提交一个 <submission_id>.facts.json，外加 batch_report.json；
面板库内含 panel_member（双波次转移明细）与 panel_summary（转移格人数）；
copy-audit 输出疑似复填清单（只发现线索，不改写任何答卷）。
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
from pipeline.copyaudit import CopyAuditError, run_copy_audit
from pipeline.engine import evaluate_snapshot
from pipeline.errors import DefinitionRejected, SnapshotRejected
from pipeline.panel import (
    BatchRejected,
    PanelError,
    apply_updates,
    initialize_panel,
    load_mapping,
    load_updates_file,
    open_panel,
)
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


def _cmd_init_panel(args: argparse.Namespace) -> int:
    try:
        mapping = load_mapping(args.mapping)
        initialize_panel(args.db, mapping, overwrite=args.overwrite)
    except PanelError as exc:
        print(f"建库失败: {exc}", file=sys.stderr)
        return 4
    print(
        f"面板库已建立: {args.db}（指标 {mapping.indicator!r}，"
        f"版本 {sorted(mapping.versions)}；映射已固定）"
    )
    return 0


def _cmd_panel_apply(args: argparse.Namespace) -> int:
    try:
        mapping = load_mapping(args.mapping)
        conn = open_panel(args.db, mapping)
    except PanelError as exc:
        print(f"打开面板库失败: {exc}", file=sys.stderr)
        return 4

    # 先读取并全量校验所有批次（不触碰数据库）：任一批次文件无法解析或
    # 校验失败，整体退出、库内无任何改动。
    parsed_batches: list[tuple[str, Any]] = []
    try:
        for updates_path in args.updates:
            parsed_batches.append((updates_path, load_updates_file(updates_path)))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"批次文件无法读取/解析，未做任何入库: {exc}", file=sys.stderr)
        conn.close()
        return 5

    reports = []
    try:
        # 每个更新文件是独立一批：独立事务，一批失败回滚该批，不连累已提交的
        # 先前批次（结构问题已在上面的预校验阶段整体拦截）。
        for batch_index, (updates_path, raw_updates) in enumerate(parsed_batches):
            try:
                report = apply_updates(
                    conn, mapping, raw_updates, batch_index=batch_index
                )
            except (BatchRejected, PanelError) as exc:
                errors = getattr(exc, "errors", [str(exc)])
                for err in errors:
                    print(
                        f"批次 {updates_path} 被整批拒绝，该批无改动: {err}",
                        file=sys.stderr,
                    )
                return 5
            reports.append(report.to_dict())
            print(
                f"批次 {updates_path}: 受理 {len(report.applied)} 条；"
                f"幂等 {len(report.idempotent)} 条；"
                f"低修订忽略 {len(report.superseded)} 条；"
                f"重建家庭 {report.rebuilt_families}"
            )
    finally:
        conn.close()

    if args.report_out:
        with open(args.report_out, "w", encoding="utf-8") as fh:
            json.dump(reports, fh, ensure_ascii=False, indent=2)
        print(f"批处理报告: {args.report_out}")
    return 0


def _cmd_copy_audit(args: argparse.Namespace) -> int:
    try:
        stats = run_copy_audit(args.facts_dir, args.questions, args.out)
    except CopyAuditError as exc:
        print(f"复填核查失败: {exc}", file=sys.stderr)
        return 6
    except OSError as exc:
        # 清单写不出去：已有清单保持不变（原子写入）。
        print(f"复填核查失败: 无法写出清单: {exc}", file=sys.stderr)
        return 6
    print(
        f"已核查 {stats.submission_count} 份提交；"
        f"进入逐对核验 {stats.candidate_pair_count} 对；"
        f"直接线索 {stats.clue_count} 条；簇 {stats.cluster_count} 个。"
        f"清单: {args.out}"
    )
    return 0


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

    init_panel = sub.add_parser(
        "init-panel", help="按版本映射建立双波次面板 SQLite 库（一个库一项指标）"
    )
    init_panel.add_argument("--db", required=True, help="SQLite 库文件路径")
    init_panel.add_argument("--mapping", required=True, help="建库映射 JSON")
    init_panel.add_argument(
        "--overwrite", action="store_true", help="库已存在时覆盖（默认报错）"
    )
    init_panel.set_defaults(func=_cmd_init_panel)

    panel_apply = sub.add_parser(
        "panel-apply",
        help="把波次事实与成员对照按修订入仓，维护转移明细与汇总",
    )
    panel_apply.add_argument("--db", required=True, help="init-panel 建立的 SQLite 库")
    panel_apply.add_argument("--mapping", required=True, help="必须与建库映射一致")
    panel_apply.add_argument(
        "--updates", nargs="+", required=True,
        help="一批或多批更新 JSON；每个文件是独立事务批次",
    )
    panel_apply.add_argument("--report-out", help="可选：批处理报告输出路径")
    panel_apply.set_defaults(func=_cmd_panel_apply)

    copy_audit = sub.add_parser(
        "copy-audit",
        help="同一轮事实表的疑似复填线索核查（只发现线索，不改写答卷）",
    )
    copy_audit.add_argument(
        "--facts-dir", required=True,
        help="一轮 *.facts.json 所在目录（只读目录一级，不读 batch_report.json）",
    )
    copy_audit.add_argument(
        "--questions", nargs="+", required=True,
        help="同一问卷版本下 4~8 个不同的根级题路径",
    )
    copy_audit.add_argument(
        "--out", required=True,
        help="清单 JSON 输出路径（原子写入；本轮失败时已有清单不变）",
    )
    copy_audit.set_defaults(func=_cmd_copy_audit)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
