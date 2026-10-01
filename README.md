# 高校分类定位论证

把院校使命、学科结构、人才培养、社会服务四类证据组织成**有生效期**的分类建议；
规则换版、专家回避、材料补交、复议重算发生时，已签结论仍指向签署当时的原始
输入与参与人，复议人员可逐指标还原计算依据。并发送审、重复提交与进程重启
不会制造两份决定。

零三方依赖：Python 3.11+ 标准库 + SQLite（WAL）。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/classification_service/`：服务端实现。
  - `canonical.py`：规范 JSON 与内容哈希（跨进程复算的基础）。
  - `db.py`：SQLite 结构、连接管理与状态机迁移表。
  - `store.py`：数据访问、每线程连接与 `BEGIN IMMEDIATE` 写事务。
  - `rules.py`：版本化规则包与确定性评分引擎（纯数据规则 + 固定算子）。
  - `workflow.py`：用例编排（申请、证据修订、回避、签署、复议）。
  - `httpapi.py`：线程化 HTTP/JSON 接口与幂等键处理。
  - `seed.py`：两版示例规则（2024.1 旧版 / 2026.1 现行版）与专家。
  - `__main__.py`：启动入口。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约与服务端回归测试（含并发、重启、HTTP 端到端）。

## 启动

```bash
# 写入示例规则版本与专家后启动（默认 127.0.0.1:8080）
PYTHONPATH=src python3 -m classification_service --db classification.db --seed
```

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

## 工作流（对应契约五状态：草拟/受理/评审/签署/复议）

```
草拟 ──受理──► 受理 ──进入评审──► 评审 ──签署──► 签署 ──发起复议──► 复议 ──重签──► 签署
```

1. **建档（草拟）**：高校代码唯一；使命陈述同时落为 `mission` 证据第 1 版。
2. **证据补交**：四类槽位 `mission/disciplines/talent/service` 的每次提交都
   **追加为新版本**（`(案卷,槽位,seq)` 与内容哈希双唯一），不覆盖旧版。
3. **受理**：四槽位齐备方可受理。
4. **进入评审**：此时把当前激活规则版本**冻结**到案卷，此后换版不影响本案。
5. **专家回避**：有在力回避关系的专家不能分派；评审期间新增回避会拦截签署。
6. **签署（签署）**：至少两名实际提交意见且无回避的专家；把评分明细、各槽位
   证据版本指针（seq+hash）、规则版本/哈希、签署官员与专家名单固化为不可变
   快照；签署后证据通道封闭，须先复议才能补交。
7. **复议与重签**：复议冻结被复议决定的规则版本与证据快照重算并逐项比对；
   复议态可补交新材料，重签产生**新版本决定行（只追加）**——旧决定与参与人
   原样保留，在途复议自动记为 adjusted/upheld。

## 关键不变量如何落地

| 契约不变量 | 落地方式 |
| --- | --- |
| 分类规则版本 | 规则包只追加、内容哈希唯一；案卷在进入评审时冻结版本；决定与复议都存 `rule_version + rule_hash`；换版不删旧包，旧版结论随时可用旧版逐位复算。 |
| 专家回避 | `recusals` 在力关系分派前强校验；签署前对实际提交意见的专家二次校验；参与人快照只含无回避专家。 |
| 签署快照 | `decisions` 行不可变，内嵌规范 JSON 评分明细、证据指针（seq+hash）与参与人；读回时校验证据/规则哈希，底层行被改动即报 tampered。 |
| 复议追溯 | `appeals` 冻结规则版本与快照指针；`GET /api/appeals/{id}` 返回原评分、重算评分与逐指标差异；重签只追加新版本决定。 |

## 并发、重复提交与重启

- **单决定保证**：所有写走 `BEGIN IMMEDIATE` 立即写锁；`applications` 自然键、
  `(application_id, decision_seq)`、意见与证据内容哈希均有唯一约束兜底。
- **幂等键**：写请求可带 `Idempotency-Key`；查重与响应落库跟业务在**同一事务**
  原子提交，并发重复请求只有一个真正执行，其余重放首份响应；同键不同体返回
  409。键持久化在库内，**进程重启后仍然有效**。
- **崩溃恢复**：WAL + 事务原子性，崩溃只可能回滚整个事务，不会出现“业务已
  提交而幂等键丢失”。

## HTTP 接口摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/rules` | 发布版本化规则包（首个自动激活） |
| POST | `/api/rules/activate` | 激活指定版本（换版） |
| GET  | `/api/rules` | 规则版本列表 |
| POST | `/api/experts` | 登记专家 |
| POST | `/api/applications` | 建档（支持 `Idempotency-Key`） |
| GET  | `/api/applications` | 案卷列表 |
| GET  | `/api/applications/{id}` | 案卷详情（含证据指针与最新决定） |
| POST | `/api/applications/{id}/evidence` | 补交/追加证据版本 |
| GET  | `/api/applications/{id}/history` | 状态事件、证据、回避、意见流水 |
| POST | `/api/applications/{id}/accept` | 受理 |
| POST | `/api/applications/{id}/review` | 进入评审（冻结规则版本） |
| POST | `/api/applications/{id}/recusals` | 登记回避 |
| POST | `/api/applications/{id}/assignments` | 分派专家（校验回避） |
| GET  | `/api/applications/{id}/assignments` | 分派列表 |
| POST | `/api/assignments/opinions` | 专家提交意见（重复内容拒绝） |
| POST | `/api/applications/{id}/sign` | 签署；body 可带 `rule_version`（复议重签默认现行版） |
| GET  | `/api/applications/{id}/decision` | 最新签署决定（含快照与参与人） |
| GET  | `/api/applications/{id}/decisions` | 全部历史版本决定 |
| GET  | `/api/applications/{id}/evaluation?rule_version=` | 只读试算（对比新旧规则） |
| POST | `/api/applications/{id}/appeals` | 发起复议 |
| GET  | `/api/appeals/{id}` | 冻结规则+原始证据重算，返回逐指标差异 |
| POST | `/api/appeals/{id}/resolve` | 了结复议（upheld/adjusted） |

## 示例规则与“应用型误伤”

种子含两版规则：`2024.1` 为科研导向旧版（研究型阈值低、应用型门槛高、指标少），
对同一所应用型院校会得出 `mixed`；`2026.1` 增加实践课程、双师型教师、技术转化
等指标并重校阈值，结论纠正为 `applied`。同一证据可经
`GET /api/applications/{id}/evaluation?rule_version=2024.1` 直接对比。
