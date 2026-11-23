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
  copyaudit.py       同一轮事实表的疑似复填线索核查（copy-audit 的核心）
  reconcile.py       同一提交的基准 + 两份离线回传快照三路合并（reconcile）
  cli.py             命令行实现
tests/               unittest 用例
examples/            示例定义与快照（不可信数据，独立目录）
  panel/             面板库映射、可复跑端到端演示与只读查询助手
  copy_audit/        复填核查的可复跑演示（小样本 + 两万份性能形状）
  reconcile/         离线补录合并的可复跑演示（未封闭空组 + 两层重复组）
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

## 复填线索核查（copy-audit）

县里收齐同一轮问卷后，质检员用它在离线环境找出「可能整份照录或只改了
一个答案」的答卷，再打开原记录逐份核查。**它只发现线索：不合并、不删除、
不改写任何答卷。**

```bash
python -I run_pipeline.py copy-audit \
  --facts-dir examples/out \
  --questions q1 q2 q3 q4 q5 \
  --out copy-audit.json
```

- **输入**：`--facts-dir` 目录**一级**所有 `*.facts.json`（`one`/`batch`
  产出的单份事实表；`batch_report.json` 与其他文件一律不读），以及同一
  问卷版本下 **4~8 个不同的根级题路径**。
- **采信**：只取根实例中所选题里 `visibility=true` 且
  `answer_status=answered` 的非空标量作答；没有采信作答的题视为缺项。
  标量相等沿用引擎规则：`true`、`1`、`1.0`、`"1"` 互不混认；不做模糊文本
  匹配或跨版本换码。未采信行的缓存值不校验、不读取。
- **直接线索**：两份答卷在双方都采信作答的所选题上，同答案题数 ≥ 4 且
  不同答案题数 ≤ 1；一侧或两侧缺项不计为相同或不同（常见答案本身不足
  为证，缺项也不是分歧）。
- **全有或全无**：坏事实文件、行与表头的提交 ID/版本不一致、跨文件版本
  不一致、提交 ID 为空或重复、同一答卷的所选根题重复、已采信行没有合法
  标量——任一出现即指出出错文件并以退出码 6 中止，**不交付部分清单**；
  本轮失败时已有清单保持不变（清单原子写入）。
- **清单**：能沿直接线索互相到达的答卷归为同一簇，孤立答卷不入清单。
  每簇列出成员提交 ID 与簇内**全部**直接线索（两端 ID、相同题路径、
  不同题路径）——间接相连的两份不会写成直接相似，也不只输出把成员连
  起来的那几条。清单不复制答案或原始证据值；内容不随读文件顺序、文件
  名或事实行顺序变化。
- **性能**：直接线索要求至少 4 题同答案，合格的一对必然在某个 4 题子集
  上取值完全一致；按「题子集 × 类型化取值」分桶找出全部候选对再逐对
  核验，不重不漏，绝不因常见答案覆盖全轮而逐对枚举全部答卷。命令结束
  时打印进入逐对核验的答卷对数，作为核查工作量留痕。

可复跑演示（小样本的簇/线索变化 + 两万份性能形状与植入对检出）：

```bash
python -I examples/copy_audit/run_copy_audit_demo.py
```

## 离线补录合并（reconcile）

一台设备补录后没有可靠更新时间，只能把两份**完整回传快照**与当初发出的
快照做一次三路合并，得到一份可直接入仓的答卷。只处理同一提交的这一次
合并，不做版本迁移，也不做在线同步。

```bash
python -I run_pipeline.py reconcile \
  --definition examples/reconcile/definition.json \
  --base examples/reconcile/base.json \
  --left examples/reconcile/left.json \
  --right examples/reconcile/right.json \
  --out merged.json
```

- **共同基准**：三份快照必须是同一个非空提交 ID、引用同一份定义，且各自
  通过快照结构校验；本入口的实例 ID 只允许字母、数字、下划线和连字符。
- **输入严格性**：重复 JSON 键、`NaN`/`Infinity`/溢出浮点、非布尔
  `closed` 一律拒绝。这些检查先于合并发生，错误时非零退出。
- **原始字段**：以 base 为基准做三路合并。只有一边改动（含**去掉字段**）
  就保留那一边；两边相同只算一次；两边改成不同结果报冲突。字段未出现与
  显式 `null` 是不同状态；标量按引擎的严格类型相等比较（`true`、`1`、
  `1.0`、`"1"` 互不混同）。选项非法、题未展示或展示未定都**不影响**
  原始缓存字段的合并——裁决只发生在合并之后。
- **成员身份**：重复组不按数组行号对齐，身份 = 所属组 + 完整祖先实例链 +
  稳定 ID。同一成员两边都保留时合并其内部互不冲突的改动。原有成员一方
  删除、另一方原样保留则删除；保留方改了该成员或其任一后代则冲突（不复活、
  也不丢修改）；两方都删则删除。新成员可由一方新增；两方以同一身份新增
  时只有完整内容等价才接受，否则冲突，不拼成一个新成员。
- **清单状态**：调整对象键顺序或实例数组顺序不算修改；省略清单与封闭的空
  清单等价；省略 `closed` 视为封闭。`closed` 的修改与同一清单里不同成员的
  增删**分别**合并。未封闭只是提示可能还有未记录成员，不表示文件中已移除
  的已知成员仍保留。
- **输出**：成功时只原子写出一个 JSON：`snapshot`（完整合并快照，可直接
  再喂给 `one`）与 `facts`（在合并后快照上用原定义**重新裁决**的完整事实
  表，绝不从两份回传记录拼接事实行）。交换 `--left`/`--right` 或重排输入
  中的成员，不改变成功交付的内容与输出顺序。
- **失败**：输入错误退出码 7；合并冲突退出码 8，指出冲突类型与完整组、
  实例、题目位置。不发布部分答卷，也不替换已有输出。

可复跑演示（未封闭空组让 `exists` 题展示未定 + 两层重复组同名子成员 +
删除/修改冲突保留上一份交付）：

```bash
python -I examples/reconcile/run_reconcile_demo.py
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
