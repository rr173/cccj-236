"""事实行与证据的 JSON 序列化。"""

from __future__ import annotations

from typing import Any

from .engine import FactRow
from .models import Evidence


def evidence_to_dict(evidence: Evidence) -> dict[str, Any]:
    return {
        "kind": evidence.kind,
        "detail": evidence.detail,
        "static_path": evidence.static_path,
        "instance_path": evidence.instance_path,
        "value": evidence.value,
        "status": evidence.status,
    }


def fact_row_to_dict(row: FactRow) -> dict[str, Any]:
    return {
        "version": row.version,
        "submission_id": row.submission_id,
        "static_path": row.static_path,
        "instance_path": row.instance_path,
        "visibility": row.visibility.value,
        "answer_status": row.answer_status,
        "answer_value": row.answer_value,
        "reason": row.reason,
        "list_state": (
            None
            if row.list_closed is None
            else {
                "closed": row.list_closed,
                "instance_count": row.list_instance_count,
            }
        ),
        "evidence": [evidence_to_dict(e) for e in row.evidence],
    }


def facts_to_dict(submission_id: str, version: str, rows: list[FactRow]) -> dict[str, Any]:
    return {
        "submission_id": submission_id,
        "version": version,
        "row_count": len(rows),
        "facts": [fact_row_to_dict(r) for r in rows],
    }
