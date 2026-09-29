# 合作成果报告封账

本项目维护合作成果报告封账的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖报告编制组、中外签署人、审计人员，并明确输入摘要、双方会签、封账版本、分块导出等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/sealing_backend/`：封账后端（领域服务、SQLite 持久层、HTTP 接口）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动封账后端 HTTP 服务。
- `tests/`：契约完整性回归测试与后端行为测试。

## 后端设计

状态机：报告版本 `试算 → 会签 → 封账 → 导出`；更正单 `试算 → 会签 → 更正`。

- **输入摘要**：试算稿创建时即固定数据版本、规则版本与数据本体，写入 SHA-256 输入摘要，迟到数据无法混入已生成的稿子。
- **双方会签**：中方、外方签署人各签一次，签署摘要绑定当前内容摘要；被更新版本取代的稿子不能继续会签。
- **封账版本**：双方签署齐全后在单个写事务内封账，按报告分配唯一正式序号；重复或并发封账只会得到同一个正式版本，绝不产生第二个。
- **更正**：封账后内容冻结，迟到数据只能进入下一版本或更正单；更正单须经独立于签署人和申请人的批准人批准（重开审批），同样走双方会签后生效。
- **分块导出**：导出件定长分块并逐块附 SHA-256 摘要，清单含总摘要；导出件生成后冻结，中断续传、重复下载、重复导出结果逐字节一致。
- **幂等与审计**：写接口支持 `Idempotency-Key` 重放；全部状态迁移写入审计轨迹（`GET /audit-log`）。

主要接口（写操作需 `X-Actor` 头）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/reports` | 创建报告 |
| POST | `/reports/{id}/versions` | 生成试算稿（含输入摘要） |
| POST | `/versions/{id}/signatures` | 双方会签 |
| POST | `/versions/{id}/seal` | 封账（幂等） |
| POST | `/versions/{id}/corrections` | 申请更正单（独立批准） |
| POST | `/corrections/{id}/signatures`、`/corrections/{id}/seal` | 更正单会签与生效 |
| POST | `/versions/{id}/export` | 生成分块导出件 |
| GET | `/versions/{id}/export/manifest`、`/versions/{id}/export/chunks/{n}` | 清单与分块下载（续传） |
| GET | `/audit-log` | 审计轨迹 |

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

启动服务：`python3 tools/run_server.py --db var/sealing.db --port 8000`
