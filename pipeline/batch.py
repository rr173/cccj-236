"""批处理运行器：多定义、多快照隔离处理。

一份坏定义被整体拒绝；一份坏快照只拒绝该快照，其他独立答卷照常产出。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .engine import evaluate_snapshot
from .errors import DefinitionRejected, SnapshotRejected
from .models import Definition
from .serialize import facts_to_dict
from .validate import load_definition, load_snapshot_file


@dataclass
class BatchReport:
    definitions: dict[str, str] = field(default_factory=dict)  # version -> 定义文件
    definition_errors: dict[str, list[str]] = field(default_factory=dict)
    processed: list[str] = field(default_factory=list)
    snapshot_errors: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "definitions_loaded": self.definitions,
            "definition_rejections": self.definition_errors,
            "processed_submissions": self.processed,
            "snapshot_rejections": self.snapshot_errors,
        }


def load_definitions(paths: list[str | Path]) -> tuple[dict[str, Definition], BatchReport]:
    definitions: dict[str, Definition] = {}
    report = BatchReport()
    for path in paths:
        try:
            definition = load_definition(path)
        except DefinitionRejected as exc:
            report.definition_errors[str(path)] = [str(e) for e in exc.errors]
            continue
        if definition.version in definitions:
            report.definition_errors[str(path)] = [
                f"VERSION_DUP: 版本 {definition.version!r} 已由其他定义文件提供"
            ]
            continue
        definitions[definition.version] = definition
        report.definitions[definition.version] = str(path)
    return definitions, report


def run_batch(
    definition_paths: list[str | Path],
    snapshot_paths: list[str | Path],
) -> tuple[list[dict[str, Any]], BatchReport]:
    """返回（每份快照一个事实表 dict，报告）。"""
    definitions, report = load_definitions(definition_paths)
    outputs: list[dict[str, Any]] = []

    for snap_path in snapshot_paths:
        # 先只读出版本，以选择对应定义；读取失败按快照错误隔离。
        try:
            with open(snap_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            version = raw.get("version") if isinstance(raw, dict) else None
        except (OSError, json.JSONDecodeError) as exc:
            report.snapshot_errors[str(snap_path)] = [f"UNREADABLE: {exc}"]
            continue

        definition = definitions.get(version) if isinstance(version, str) else None
        if definition is None:
            report.snapshot_errors[str(snap_path)] = [
                f"VERSION_UNAVAILABLE: 没有通过校验的定义版本 {version!r}"
            ]
            continue
        try:
            snapshot = load_snapshot_file(snap_path, definition)
            rows = evaluate_snapshot(definition, snapshot)
        except SnapshotRejected as exc:
            report.snapshot_errors[str(snap_path)] = [str(e) for e in exc.errors]
            continue
        report.processed.append(snapshot.submission_id)
        outputs.append(facts_to_dict(snapshot.submission_id, definition.version, rows))

    return outputs, report
