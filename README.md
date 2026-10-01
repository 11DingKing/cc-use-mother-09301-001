# 高校分类定位论证

把院校使命、学科结构、人才培养、社会服务四类证据组织成**带生效期的分类建议**的服务端。
解决“应用型学校被研究型指标误伤”的核心手段是：版本化的确定性规则、可重放的评估、
不可变的签署快照，以及“一案一决定”的并发与重启保证。

## 需求与实现的对应

| 业务要求 | 落地方式 |
| --- | --- |
| 有生效期的分类建议 | 决定含 `valid_from` / `valid_to`（默认 5 年），随签署快照一并持久化 |
| 规则换版不追溯旧案 | 规则集版本化，建案时绑定版本；签署快照内嵌规则全文与指纹，换版只影响新案 |
| 专家回避 | 同单位自动拦截（专家 `org` 与院校比对）＋冲突单位登记；回避后不能出意见，未回避评审人不足两人不能起评/签署 |
| 材料补交 | 证据追加式版本化（v1、v2…），旧版本永不覆盖；补交后针对旧版本的意见不再满足签署条件，须重新评审 |
| 已签结论指向原始输入与参与人 | 签署快照冻结规则全文、当前证据、**完整证据链历史**、全部评审意见与历任参与人 |
| 复议可还原计算依据 | 规则引擎为纯函数（Decimal 定点数、规范化 JSON），复议时对快照重放并逐项比对，给出指纹是否一致 |
| 证据不足不硬贴标签 | 最高分与次高分差距低于 `min_margin` 时结论为 `indeterminate`，签署被退回补正 |
| 并发送审不产生两份决定 | 全部写事务 `BEGIN IMMEDIATE`；幂等键唯一约束 + 事务内复查，并发同键返回同一结果 |
| 重复提交 | 每个写接口强制 `idempotency_key`，重试安全 |
| 进程重启 | SQLite（WAL）持久化；重启后决定、快照、事件流完整可查，重放结果不变 |

## 技术栈

Python 3.11 标准库：`http.server`（多线程 HTTP）＋ `sqlite3`（WAL）。零三方依赖。

```
src/classification/
├── core.py              # 规范化 JSON / SHA-256 / Decimal 友好的标识与时间
├── config.py            # 环境变量配置
├── storage.py           # SQLite schema、每线程连接、BEGIN IMMEDIATE 事务
├── service.py           # 案件工作流、签署快照、回避、复议编排
├── services/
│   ├── rules.py         # 确定性规则引擎（evaluate / replay）
│   └── seeds.py         # 内置规则集 2024.1（旧）/ 2026.1（现行）
├── http_app.py          # 路由与 JSON 序列化
└── __main__.py          # python3 -m classification
```

## 运行

```bash
PYTHONPATH=src python3 -m classification --port 8080 --db data/classification.db
# 或安装后：classification-server
```

## API（所有 POST 均需 `idempotency_key`，重试时复用同一键）

```
POST /api/cases                       建案（可指定 ruleset_version）
POST /api/cases/{id}/accept           受理
POST /api/experts                     登记专家（org；可选 conflicts 机构列表）
POST /api/cases/{id}/experts          指派评审人（同单位/冲突单位 → 403）
POST /api/cases/{id}/experts/{eid}/recuse   专家回避
POST /api/cases/{id}/evidence         提交/补交证据 → 追加版本
POST /api/cases/{id}/review/start     进入评审（需证据 + ≥2 名未回避专家）
POST /api/cases/{id}/reviews          专家意见（绑定当前证据版本）
POST /api/cases/{id}/sign             签署：冻结快照、计算建议与生效期
GET  /api/cases/{id}/decision         查看决定与完整快照
POST /api/cases/{id}/reconsiderations 复议申请（同步返回快照重放报告）
POST /api/cases/{id}/reconsiderations/resolve  维持(upheld)/另案(superseded)
GET  /api/cases/{id}/events           追加式事件流
GET  /api/rulesets                    规则集列表；POST 发布新版本
```

证据项形如：

```json
{"dimension": "service", "submitted_by": "高校-王老师",
 "content": "与企业合作开展技术服务、成果转化、横向课题……"}
```

`dimension ∈ {mission, disciplines, talent, service}`。评分按候选类别在各维度的
锚定词覆盖率加权求和；类别分差小于阈值 `min_margin` 时不予定论。

## 验证

```bash
python3 -m unittest discover -s tests -v     # 23 项：引擎确定性/工作流/回避/补交/换版/复议/并发/重启/HTTP
python3 -m compileall -q src tools tests     # 编译检查
python3 tools/check_contract.py domain/contract.json
```

## 领域契约

- `domain/contract.json`：角色（主管部门/高校/评审专家）、状态（草拟/受理/评审/签署/复议）
  与不变量（分类规则版本、专家回避、签署快照、复议追溯）。
- `src/domain_contract/`：契约读取与确定性校验。
