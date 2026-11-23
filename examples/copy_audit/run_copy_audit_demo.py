#!/usr/bin/env python3
"""可复跑的 copy-audit 端到端演示（以隔离模式运行：python -I）。

第一部分（小样本）：三份答卷只有相邻两对成立直接线索；随后移除一份答卷
上的一项有效作答，簇与线索同时变化；并能分辨「两处真实分歧」与「一侧
缺项」。

第二部分（两万份）：八个核查题中三道答案极常见、其余回答近乎唯一，植入
一对满足条件的答卷。copy-audit 按「题子集 × 类型化取值」分桶，只有极少数
候选对进入逐对核验，绝不因常见答案覆盖全轮而逐对枚举全部答卷。

全部输入在独立临时目录中生成（事实表由现有引擎真实裁决产出），可反复
运行；不留下任何需要提交的数据文件。
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.cli import main as cli_main  # noqa: E402
from pipeline.engine import evaluate_snapshot  # noqa: E402
from pipeline.serialize import facts_to_dict  # noqa: E402
from pipeline.validate import load_snapshot, validate_definition  # noqa: E402

Q5 = [f"q{i}" for i in range(1, 6)]
Q8 = [f"q{i}" for i in range(1, 9)]


def run_cli(*argv: str) -> tuple[int, str]:
    print("\n$ python -I run_pipeline.py " + " ".join(argv))
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = cli_main(list(argv))
    line = stdout.getvalue().strip()
    if line:
        print(line)
    return rc, line


def write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def show_listing(path: Path, title: str) -> dict:
    listing = json.loads(path.read_text(encoding="utf-8"))
    print(f"\n===== {title} =====")
    print(json.dumps(listing, ensure_ascii=False, indent=2))
    return listing


# ---------------------------------------------------------------------------
# 第一部分：三份答卷的小样本（真实 batch 产出事实表）
# ---------------------------------------------------------------------------


def demo_small_sample(workdir: Path) -> None:
    print("\n########## 第一部分：小样本（相邻两对、移除作答、分歧与缺项）##########")
    facts_dir = workdir / "small" / "facts"
    facts_dir.mkdir(parents=True)
    def_dir = workdir / "small" / "defs"
    def_dir.mkdir()
    snap_dir = workdir / "small" / "snapshots"
    snap_dir.mkdir()

    definition = {
        "version": "copy-demo-v1",
        "nodes": [
            {"id": q, "type": "choice", "options": [1, 2]} for q in Q5
        ],
    }
    def_path = def_dir / "copy-demo-v1.json"
    write_json(def_path, definition)

    # sub-a = (1,1,1,1,1)；sub-b = (2,1,1,1,1)；sub-c = (2,2,1,1,1)。
    # a-b 差 1 题、b-c 差 1 题 -> 直接线索；a-c 差 2 题（两处真实分歧）-> 不是。
    def snapshot(sub_id: str, answers: dict[str, int]) -> Path:
        path = snap_dir / f"{sub_id}.json"
        write_json(
            path,
            {
                "submission_id": sub_id,
                "version": "copy-demo-v1",
                "root": {"answers": answers},
            },
        )
        return path

    def run_round(tag: str, omit_b_q1: bool) -> dict:
        for old in facts_dir.glob("*.facts.json"):
            old.unlink()
        for old in snap_dir.glob("*.json"):
            old.unlink()
        b_answers = {"q1": 2, **{q: 1 for q in Q5[1:]}}
        if omit_b_q1:
            del b_answers["q1"]  # 移除一项有效作答（题照问、没答 -> 缺项）
        snaps = [
            snapshot("sub-a", {q: 1 for q in Q5}),
            snapshot("sub-b", b_answers),
            snapshot("sub-c", {"q1": 2, "q2": 2, **{q: 1 for q in Q5[2:]}}),
        ]
        rc, _ = run_cli(
            "batch",
            "--definitions", str(def_path),
            "--snapshots", *(str(p) for p in snaps),
            "--out-dir", str(facts_dir),
        )
        assert rc == 0
        out = workdir / "small" / f"listing-{tag}.json"
        rc, _ = run_cli(
            "copy-audit", "--facts-dir", str(facts_dir),
            "--questions", *Q5, "--out", str(out),
        )
        assert rc == 0
        return show_listing(out, f"小样本清单（{tag}）")

    base = run_round("base", omit_b_q1=False)
    assert base["cluster_count"] == 1 and base["clue_count"] == 2
    cluster = base["clusters"][0]
    assert cluster["members"] == ["sub-a", "sub-b", "sub-c"], cluster
    pairs = {tuple(c["pair"]) for c in cluster["clues"]}
    # 只有相邻两对成立；间接相连的 sub-a/sub-c 不是直接线索。
    assert pairs == {("sub-a", "sub-b"), ("sub-b", "sub-c")}, pairs
    print(">> sub-a 与 sub-c 有两处真实分歧（q1、q2），只沿 sub-b 间接相连，")
    print("   归入同一簇但不列为直接线索。")

    changed = run_round("removed", omit_b_q1=True)
    # 移除 sub-b 的 q1 作答后：簇与线索同时变化。
    assert changed["cluster_count"] == 1 and changed["clue_count"] == 1
    cluster = changed["clusters"][0]
    assert cluster["members"] == ["sub-a", "sub-b"], cluster
    clue = cluster["clues"][0]
    assert clue["pair"] == ["sub-a", "sub-b"], clue
    # 一侧缺项不计为相同或不同：a-b 仍 4 题相同、0 题不同。
    assert clue["same_questions"] == ["q2", "q3", "q4", "q5"], clue
    assert clue["different_questions"] == [], clue
    print(">> 移除 sub-b 的 q1 作答后：sub-c 变孤立答卷退出清单，b-c 线索消失；")
    print("   而 sub-b 缺 q1 不算分歧，a-b 仍是直接线索（缺项 ≠ 真实分歧）。")


# ---------------------------------------------------------------------------
# 第二部分：两万份答卷（三道题答案极常见、其余近乎唯一，植入一对）
# ---------------------------------------------------------------------------


def demo_scale(workdir: Path) -> None:
    print("\n########## 第二部分：两万份答卷的性能形状 ##########")
    total = 20000
    plant_src, plant_dst = 1234, 19876
    version = "copy-scale-v1"
    facts_dir = workdir / "scale" / "facts"
    facts_dir.mkdir(parents=True)

    definition = validate_definition(
        {
            "version": version,
            "nodes": [
                {"id": "q1", "type": "choice", "options": ["yes", "no"]},
                {"id": "q2", "type": "choice", "options": [1, 2]},
                {"id": "q3", "type": "choice", "options": ["A", "B"]},
                *[{"id": f"q{k}", "type": "text"} for k in range(4, 9)],
            ],
        }
    )

    print(f"生成 {total} 份快照并由现有引擎逐份裁决（事实表写入临时目录）...")
    started = time.perf_counter()
    for i in range(total):
        sid = f"sub-{i:05d}"
        answers: dict[str, object] = {"q1": "yes", "q2": 1, "q3": "A"}
        for k in range(4, 9):
            answers[f"q{k}"] = f"u-{i}-{k}"  # 近乎唯一
        if i == plant_dst:  # 植入：整份照录 sub-001234 的作答
            for k in range(4, 9):
                answers[f"q{k}"] = f"u-{plant_src}-{k}"
        snapshot = load_snapshot(
            {
                "submission_id": sid,
                "version": version,
                "root": {"answers": answers},
            },
            definition,
        )
        rows = evaluate_snapshot(definition, snapshot)
        write_json(
            facts_dir / f"{sid}.facts.json",
            facts_to_dict(sid, version, rows),
        )
    print(f"生成耗时 {time.perf_counter() - started:.1f} 秒。")

    out = workdir / "scale" / "listing.json"
    started = time.perf_counter()
    rc, line = run_cli(
        "copy-audit", "--facts-dir", str(facts_dir),
        "--questions", *Q8, "--out", str(out),
    )
    elapsed = time.perf_counter() - started
    assert rc == 0
    print(f"copy-audit 耗时 {elapsed:.1f} 秒。")

    listing = json.loads(out.read_text(encoding="utf-8"))
    assert listing["submission_count"] == total
    assert listing["cluster_count"] == 1 and listing["clue_count"] == 1
    cluster = listing["clusters"][0]
    assert cluster["members"] == ["sub-01234", "sub-19876"], cluster
    clue = cluster["clues"][0]
    assert clue["same_questions"] == Q8 and clue["different_questions"] == []

    possible = math.comb(total, 2)
    print(f">> 植入的一对被找到：{cluster['members']}（8 题全同、0 题不同）。")
    print(f">> 工作量证据：全部可能配对 {possible:,} 中，只有 1 对进入逐对核验")
    print("   （三道极常见答案没有导致逐对枚举；其余答案近乎唯一，分桶即隔离）。")

    # 清单确定性：同样的输入再跑一遍，字节级一致。
    out2 = workdir / "scale" / "listing-again.json"
    rc, _ = run_cli(
        "copy-audit", "--facts-dir", str(facts_dir),
        "--questions", *reversed(Q8), "--out", str(out2),
    )
    assert rc == 0
    assert out.read_bytes() == out2.read_bytes()
    print(">> 改变题目参数顺序重跑：清单字节级一致。")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="copy-audit-demo-"))
    print(f"演示工作目录（独立、可删除）: {workdir}")
    demo_small_sample(workdir)
    demo_scale(workdir)
    print("\n全部演示断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
