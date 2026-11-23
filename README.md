# 离线问卷事实裁决管线

面向现场离线采集场景：采集器在题被隐藏时仍可能保存旧答案，改版问卷又可能复用
题号。本管线把**问卷定义**与**提交快照**转成逐题的**问题事实表**，明确区分：

| 研究者的问题 | 事实表中的表达 |
| --- | --- |
| 问了没答 | `visibility=true` + `answer_status=displayed_unanswered` |
| 根本没问 | `visibility=false` + `not_asked` / `not_asked_cached`（缓存保留但不算作答） |
| 当时无法判定是否该问 | `visibility=unknown` + `undetermined` / `undetermined_cached` |
| 问了但缓存值非法 | `visibility=true` + `illegal_cached_value`（含具体原因） |
| 正常作答 | `visibility=true` + `answered` |

只处理提交快照，不包含任何表单前端或收集系统。仅依赖 Python 标准库，
设计为以隔离模式运行：`python -I run_pipeline.py ...`。

## 目录

```
pipeline/            管线代码（与数据目录分离）
  tristate.py        Kleene 三值逻辑（true/false/unknown）
  models.py          定义、快照、事实行的数据模型
  validate.py        定义静态校验 + 快照结构校验
  engine.py          门控求值、实例遍历、事实裁决
  serialize.py       事实表 JSON 序列化
  batch.py           多定义/多快照批处理与故障隔离
  panel.py           双波次面板转移库（SQLite，panel-apply 的核心）
  cli.py             命令行实现
tests/               unittest 用例
examples/            示例定义与快照（不可信数据，独立目录）
  panel/             面板库映射、可复跑端到端演示与只读查询助手
run_pipeline.py      -I 友好的命令行入口
run_tests.py         -I 友好的测试入口
```

## 运行

```bash
# 测试
python -I run_tests.py

# 批处理：每份提交输出 <submission_id>.facts.json，外加 batch_report.json
python -I run_pipeline.py batch \
  --definitions examples/definitions/*.json \
  --snapshots examples/snapshots/*.json \
  --out-dir examples/out

# 单份
python -I run_pipeline.py one --definition <def.json> --snapshot <snap.json>
```

## 双波次面板转移库（panel-apply）

面向「同一个人首访 → 回访如何改变回答」的面板分析。**一个 SQLite 库负责一项
指标**，直接消费 `one`/`batch` 产出的事实 JSON，不重新裁决任何题目门控。

### 建库映射（一经固定）

```json
{
  "indicator": "livelihood_choice",
  "versions": [
    {"version": "screening-v3",
     "target_static_path": "grp_members.q_live",
     "codes": [{"code": 1, "category": "farming"},
               {"code": "1", "category": "string_one_demo"}]},
    {"version": "screening-v4",
     "target_static_path": "grp_members.q_live",
     "codes": [{"code": 1, "category": "paid_labor"}]}
  ]
}
```

- 每个版本指定**一个根级重复组内目标选择题**的静态路径；其他题一律不入仓。
- 代码按解析后的 JSON 类型识别：整数 `1` 与字符串 `"1"` 是两个不同代码
  （布尔 `true` 也不等于 `1`）；类别必须是非空字符串。
- 映射在建库时写入库内并保存指纹；之后 `panel-apply` 带入任何不同映射直接
  报 `MAPPING_CONFLICT`，不做写入。

### 批更新格式

每个更新文件是独立事务批次，内含一个非空更新数组；同批不得出现两个操作
针对同一家庭的同一波次。

```json
[
  {"family_id": "HH-1", "wave": "first", "revision": 1,
   "operation": "replace",
   "facts": { "<one/batch 产出的完整事实 JSON>": null },
   "crosswalk": [
     {"instance_path": "grp_members#m1", "member_id": "M_ALICE"}
   ]},
  {"family_id": "HH-1", "wave": "second", "revision": 3,
   "operation": "withdraw"}
]
```

- 成员身份**只来自对照**：把目标题行的 `instance_path` 关联到调查组发放的
  稳定成员 ID，绝不从路径推导成员；目标题事实行与对照必须一一对应，允许
  两者同时为空（表示该波该题无成员）。
- `replace` 必须同时带 `facts` 与 `crosswalk`；`withdraw` 两者都不得带。
- 波次取值 `first` / `second`；修订号必须是正整数。
- 内容比较基于解析后的 JSON（忽略空白与对象键顺序），与文件名无关。

### 修订水位与事务语义

- 每个家庭每波独立以**最高已接受修订**为准；撤回后水位仍保留，更低修订
  永不改变状态、不能让撤回内容复活。
- 同修订同内容重送为幂等（不重复计算）；同修订异内容报 `REVISION_CONFLICT`。
- 批内任一输入无效或出现同修订冲突，整批回滚，不留下任何改动。
- 常规入库只重建**本批涉及家庭**的成员明细与受影响汇总格，绝不扫描其他
  家庭；水位、成员转移、人数在同一 SQLite 事务中生效。

### 状态种类（类别与原因分开标识）

明细两端各有 `state_kind` + `state_value`：

| 情况 | state_kind | state_value |
| --- | --- | --- |
| 展示且有效作答，代码命中映射 | `category` | 映射的统一类别 |
| 已采信作答但代码无映射（不读缓存兜底） | `reason` | `unmapped` |
| 确定未展示（`visibility=false`） | `reason` | `not_presented` |
| 展示与否未定（`visibility=unknown`） | `reason` | `undetermined` |
| 展示但无有效作答（未答/非法缓存） | `reason` | `presented_unanswered` |
| 成员只在另一波出现（不代表该成员不存在） | `no_observation` | `no_observation` |

只有 `visibility=true` 且 `answer_status=answered` 的行才读取 `answer_value`
换算类别；其余行里可能残留的缓存值一律不读。

### 命令与两张结果表

```bash
# 建库（映射固定）
python -I run_pipeline.py init-panel --db panel.db \
    --mapping examples/panel/mapping.json

# 入仓（可一次给多个批次文件，每个文件独立事务）
python -I run_pipeline.py panel-apply --db panel.db \
    --mapping examples/panel/mapping.json \
    --updates batch-1.json batch-2.json --report-out report.json

# 只读查询助手（或直接用 sqlite3 标准库执行 SQL）
python -I examples/panel/query_panel.py panel.db --family HH-1
```

- **`panel_member`**：以「家庭 ID + 稳定成员 ID」唯一，覆盖两波成员并集；
  每行给出两端状态，有事实的一端可回指提交 ID、版本、静态路径、实例路径。
- **`panel_summary`**：按两端 `(state_kind, state_value)` 转移格给出人数，
  每个明细恰好贡献一人，不保留零人数格。

```sql
-- 逐人核查
SELECT * FROM panel_member WHERE family_id = 'HH-1';
-- 直接汇总
SELECT first_state_kind, first_state_value,
       second_state_kind, second_state_value, person_count
FROM panel_summary;
```

可复跑的端到端演示（位置重用、同代码跨版本不同类别、成员退出/加入、
撤回防复活、冲突整批回滚）：

```bash
python -I examples/panel/run_panel_demo.py
```

## 定义契约

- `version`：版本号；同一版本内题的身份是**静态路径**（如 `grp_a.q2`）。
- `nodes`：前序题序；`type=group` 为重复组，最多嵌套两层；题类型
  `choice/text/number/bool`，选择题的 `options` 是该题在该版本的合法值集合。
- 门控 `gate`（题与组均可带）仅支持：
  - `{"op":"eq","ref":"q1","value":1}` 等值；
  - `{"op":"answered","ref":"q2"}` 是否真正答过；
  - `{"op":"and"|"or","args":[...]}` 合取/析取；
  - `{"op":"not","arg":{...}}` 否定；
  - `{"op":"exists","group":"g","ref":"g.q","value":"yes"}`
    询问**前面的直接子组**是否有一个实例答了指定值。

### 定义级校验（全有或全无）

非法引用、门控环、越界作用域（引用兄弟/后代实例的题、exists 子组不是直接子组、
ref 不属于该子组）、前向引用、重复 id、选择题 options 非法、嵌套超过两层、
未知门控操作符——任一存在即聚合全部错误并**拒绝整份定义**，绝不产出部分事实。

## 快照契约

- 每份快照固定 `version`（不匹配则只拒绝该快照）；
- 重复组实例带稳定 `instance_id`，两层嵌套；
- 组清单 `{"instances":[...], "closed": true|false}`：
  - 省略 `closed` 视为 `true`；完全省略某组清单视为**已封闭空清单**；
  - `closed=false` 表示采集器不保证清单完整。
- 未列出的实例不产生任何事实行（不造假行）。

## 裁决语义（三值逻辑）

1. **作用域**：门控只能读到同实例或祖先实例中较早题的值；同名题不跨实例取值。
2. **等值 `eq`**：缺答（无缓存、显式 null、非法缓存、题被隐藏/未定）参与比较
   一律为 `unknown`，绝不把缺答判成假。
3. **是否答过 `answered`**：题确定不展示 → `false`；展示但无有效回答 → `false`；
   展示与否未定 → `unknown`；展示且有合法值 → `true`。
4. **合取/析取/否定**：Kleene 强三值逻辑（含未知短路表）。
5. **exists**：
   - 列出的实例中有确定命中 → `true`；
   - 无命中且清单已封闭 → `false`；
   - 无命中且清单未封闭 → `unknown`（不能把“存在”判假）；
   - 父组确定隐藏时，未封闭清单不得把结果变成未知 → 直接 `false`。
6. **父组可见性传播**：子实例可见性 = 父实例可见性 ∧ 本组门控；父组未定时，
   只对清单中实际列出的实例输出明细，且保留清单的封闭状态（`list_state`）。
7. **非法缓存值**：不参与任何门控；若该题确实展示，事实行标
   `illegal_cached_value` 并给出该版本的合法值集合作为原因。
8. **版本化合法值**：选择题合法性由“题 + 版本”共同决定，同缓存值可在一个版本
   有效、另一版本非法；事实行带 `version`，跨版本不串答案。

## 事实行字段

```json
{
  "version": "screening-v3",
  "submission_id": "sub-001",
  "static_path": "grp_contact.q_detail",
  "instance_path": "grp_contact#c1",
  "visibility": "true | false | unknown",
  "answer_status": "answered | displayed_unanswered | illegal_cached_value | not_asked | not_asked_cached | undetermined | undetermined_cached",
  "answer_value": "原始缓存值（可能为 null，不代表采信）",
  "reason": "未采信/未答的具体原因",
  "list_state": {"closed": false, "instance_count": 1},
  "evidence": [
    {"kind": "ref|exists|exists_match|cached", "detail": "...",
     "static_path": "...", "instance_path": "...", "value": "...",
     "status": "used|missing|explicit_null|illegal|hidden_unused|undetermined_unused|excluded|..."}
  ]
}
```

`evidence` 只记录**实际参与该行裁决**的引用（含祖先组门控证据）；
被排除的缓存值会以 `illegal` / `hidden_unused` / `excluded` 等状态显式留痕。

## 故障隔离

- 坏定义：进 `batch_report.definition_rejections`，不产出任何基于它的事实；
- 坏快照：进 `batch_report.snapshot_rejections`，同批其他独立答卷不受影响；
- 快照引用了无可用（被拒绝/未提供）定义版本：仅该快照报错。

## 验收场景对照

- 同一缓存答案在两条门控路径下分别有效/无效：
  `tests/test_acceptance.py::test_same_cached_value_valid_and_invalid_under_two_gate_paths`
- 未封闭清单与空组的差异：
  `tests/test_acceptance.py::test_open_vs_closed_empty_group_difference`
  及 `tests/test_engine.py::ExistsTests`
- 同名题跨版本、跨实例不串答案：
  `tests/test_acceptance.py::test_same_named_question_across_versions_does_not_share_answer`
  `tests/test_engine.py::CrossInstanceTests`
