#!/usr/bin/env python3
"""可复跑的 reconcile 端到端演示（以隔离模式运行：python -I）。

发出的答卷里：

  * ``grp_visit`` 是封闭组，只有一位命中成员 ``v1``；
  * ``grp_household`` 下两位父成员 ``hh_a`` / ``hh_b`` 各有一个**同短码**
    的子成员 ``m1``（身份 = 所属组 + 完整祖先实例链 + 稳定 ID）。

两台设备的回传：

  * 设备 A（``left.json``）把 ``grp_visit`` 标为未封闭但保留成员，同时改了
    ``hh_a`` 下 ``m1`` 的年龄；
  * 设备 B（``right.json``）删掉 ``grp_visit`` 的命中成员但**不改**封闭标记，
    并删掉父成员 ``hh_b``。

合并结果：

  * ``grp_visit`` 成为「未封闭的空组」——封闭状态来自 A、成员删除来自 B，
    两者独立合并；
  * 依赖「组内是否存在命中」的 ``q_followup`` 变为**展示未定**
    （``unknown`` / ``undetermined``），不沿用任一回传的判定；
  * ``hh_a`` 下的 ``m1`` 保留 A 的修改；``hh_b``（连同其下同名 ``m1``）
    按 B 的删除一并移除，``hh_a`` 的修改不受影响。

脚本在独立临时目录里复制输入并重建输出，可反复运行。随后演示同一份
基准下「改被删父成员下面同名子成员」的冲突——此时拒绝合并并保留上一份
交付（原子写入，逐字节不变）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.cli import main as cli_main  # noqa: E402

EXAMPLE_DIR = ROOT / "examples" / "reconcile"
PERSON_GROUP = "grp_household.grp_person"


def run_cli(*argv: str) -> int:
    print("\n$ python -I run_pipeline.py " + " ".join(argv))
    return cli_main(list(argv))


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def household(root: dict, instance_id: str) -> dict:
    for instance in root["groups"]["grp_household"]["instances"]:
        if instance["instance_id"] == instance_id:
            return instance
    raise AssertionError(f"缺少父成员 grp_household#{instance_id}")


def person(household_instance: dict) -> dict:
    instances = household_instance["groups"][PERSON_GROUP]["instances"]
    assert [p["instance_id"] for p in instances] == ["m1"], instances
    return instances[0]


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="reconcile-demo-"))
    print(f"演示工作目录（独立、可删除）: {workdir}")
    definition = EXAMPLE_DIR / "definition.json"
    paths = {
        name: workdir / f"{name}.json"
        for name in ("base", "left", "right")
    }
    for name, path in paths.items():
        write(path, load(EXAMPLE_DIR / f"{name}.json"))
    out = workdir / "merged.json"

    rc = run_cli(
        "reconcile", "--definition", str(definition),
        "--base", str(paths["base"]), "--left", str(paths["left"]),
        "--right", str(paths["right"]), "--out", str(out),
    )
    assert rc == 0, rc

    delivered = load(out)
    snapshot = delivered["snapshot"]
    facts = {
        (row["static_path"], row["instance_path"]): row
        for row in delivered["facts"]["facts"]
    }

    # 1) 未封闭空组：closed 来自 A，成员删除来自 B。
    visit = snapshot["root"]["groups"]["grp_visit"]
    print("\n-- 合并后的 grp_visit --")
    print(json.dumps(visit, ensure_ascii=False))
    assert visit == {"instances": [], "closed": False}, visit
    assert ("grp_visit.q_hit", "grp_visit#v1") not in facts

    # 2) 依赖「是否存在命中」的题展示未定，而不是沿用任一回传的判定。
    followup = facts[("q_followup", "")]
    print("\n-- q_followup（依赖 grp_visit 的 exists）--")
    print(json.dumps(
        {k: followup[k] for k in ("visibility", "answer_status", "reason")},
        ensure_ascii=False,
    ))
    assert followup["visibility"] == "unknown", followup["visibility"]
    assert followup["answer_status"] == "undetermined", followup["answer_status"]

    # 3) 两层重复组：同名子成员短码只在各自父成员下有意义。
    households = snapshot["root"]["groups"]["grp_household"]["instances"]
    print("\n-- 合并后的 grp_household --")
    print(json.dumps(households, ensure_ascii=False, indent=2))
    assert [h["instance_id"] for h in households] == ["hh_a"], households
    assert person(households[0])["answers"][f"{PERSON_GROUP}.q_age"] == 31

    previous = out.read_bytes()

    # 4) 若改的是被删父成员 hh_b 下面的同名子成员：拒绝，保留上一份交付。
    #    B（right.json）仍然删除 hh_b；把 A 改为修改 hh_b 下的 m1 而不是保留。
    conflict_left = load(EXAMPLE_DIR / "left.json")
    person(household(conflict_left["root"], "hh_b"))["answers"][
        f"{PERSON_GROUP}.q_age"
    ] = 41
    write(paths["left"], conflict_left)
    write(paths["right"], load(EXAMPLE_DIR / "right.json"))

    rc = run_cli(
        "reconcile", "--definition", str(definition),
        "--base", str(paths["base"]), "--left", str(paths["left"]),
        "--right", str(paths["right"]), "--out", str(out),
    )
    assert rc == 8, rc
    assert out.read_bytes() == previous, "冲突时不得替换已有输出"
    print("\n冲突被拒绝，上一份交付保持不变（逐字节相同）。")
    print(f"\n可复跑：python -I {Path(__file__).name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
