# 合作成果报告封账

本项目维护合作成果报告封账的领域约定、角色边界与样例数据，并提供一套完整后端：
从确定的数据版本与规则版本生成带输入摘要的试算稿，经中外双方会签后封账固定内容；
迟到数据进入下一版本或更正单，重开须经独立批准；封账版本支持分块摘要核对导出，
断点续传、重复下载与并发封账都不能改变已签结果或产生两个正式版本。

## 领域约定

- 角色：报告编制组、中外签署人（中方 / 外方）、审计人员（独立批准人）。
- 状态：试算 → 会签 → 已封账 →（经独立批准产生继任版本）已更正。
- 导出是已封账报告上的只读活动，不改变状态。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/seal_ledger/`：封账后端。
  - `canonical.py`：规范 JSON 与 SHA-256 摘要（确定性基石）。
  - `rules.py`：版本化的确定性规则引擎（数据版本 × 规则版本 → 报告正文）。
  - `models.py`：状态机与双方会签约束。
  - `store.py`：SQLite 存储，正式版本号、继任链、会签的数据库级唯一约束。
  - `service.py`：核心服务（试算、会签、封账、独立批准重开、分块导出）。
  - `api.py` / `__main__.py`：零依赖标准库 HTTP 接口与启动入口。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归与后端全流程测试（含并发封账）。

## 关键保证

1. **迟到数据不改已签结果**：数据与规则按内容摘要（SHA-256）定址登记，
   试算稿创建时即固定"输入摘要 + 正文字节"；次日补到的数据是新版本，
   只能进入下一份试算稿或更正单。
2. **双方会签才能封账**：中方、外方均签署后方可封账；封账在单个
   `BEGIN IMMEDIATE` 事务内重算复核正文并分配正式版本号，并发封账由数据库
   锁与部分唯一索引裁决，至多一个成功，正式版本号全局唯一且连续。
3. **重开须独立批准**：申请人不得批准自己的请求，批准人不得是该报告签署人；
   批准后产生一份继任试算稿，原报告在继任稿封账前始终保持已封账，封账后转为
   "已更正"但正文与分块仍可核对下载。每份封账报告至多一个继任版本。
4. **分块导出可核对**：封账清单包含正文整体摘要与每个 64 KiB 分块的摘要；
   下载按客户端幂等留痕，支持中断续传（查询已下分块）与 ETag 去重（304），
   拼回字节的摘要必须等于封账正文摘要——外方下载件与本地存档一致。

## 运行

```bash
# 启动服务（默认 127.0.0.1:8080，SQLite 文件 seal_ledger.sqlite3）
PYTHONPATH=src python3 -m seal_ledger --db seal_ledger.sqlite3 --port 8080
```

主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/data-versions` | 登记数据版本（内容定址，幂等） |
| POST | `/rule-versions` | 登记规则版本 |
| POST | `/reports` | 生成试算稿（支持 `Idempotency-Key`） |
| POST | `/reports/{id}/submit` | 提交会签 |
| POST | `/reports/{id}/signatures` | 中方/外方签署 |
| POST | `/reports/{id}/seal` | 双方齐签后封账 |
| POST | `/reports/{id}/reopen-requests` | 申请重开（登记独立批准人） |
| POST | `/reopen-approvals/{id}/decision` | 独立批准/驳回 |
| POST | `/corrections` | 凭批准创建继任试算稿 |
| GET | `/reports/{id}/manifest` | 封账清单与分块摘要 |
| GET | `/reports/{id}/chunks/{n}?client_key=...` | 下载分块（支持 `If-None-Match`） |
| GET | `/reports/{id}/download-status?client_key=...` | 续传进度 |
| GET | `/reports/{id}/events` | 审计事件流 |

## 验证

测试命令：`PYTHONPATH=src python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
