# gb-5310 验收测试

对应 `BUSINESS.md` 的验收用例，全部需要独立的 MySQL 8.0 schema
（测试账号需具备 `CREATE TEMPORARY TABLES` 权限）：

| 用例 | 覆盖口径 |
|---|---|
| `test_01_sample_scenario.py` | 固定样例：位点、牌面投影、逐批对账、逐条处置与会话级临时表 |
| `test_02_business_rules.py` | 跨分区事件号重用、信封去重、矛盾隔离、缺号续放、历史未核实、录入校验行号 |
| `test_03_atomicity_rollback.py` | 位点 SQL 后提交前注入故障，同批回滚，重试后一起提交，记录不丢失 |
| `test_04_idempotency_digest.py` | 重复执行报告与 SHA-256 校验码一致，临时表不跨会话泄漏 |
| `test_05_api_workflow.py` | 登录鉴权、角色权限、队列执行、复核签署与证据导出（另需 Redis） |

按 `BUSINESS.md` 的命令从仓库根目录启动（`projects/gb-5310/backend` 为指向
`backend/` 的符号链接，使文档中的 `PYTHONPATH` 原样可用）：

```sh
PYTHONPATH=projects/gb-5310/backend \
GB5310_TEST_MYSQL_URL='mysql+pymysql://USER:PASSWORD@HOST:3306/SCHEMA?charset=utf8mb4' \
python -m unittest discover -s test-artifacts/gb-5310/tests -v
```

端到端用例默认连接 `redis://127.0.0.1:16379/0`，可用
`GB5310_TEST_REDIS_URL` 覆盖；Redis 不可用时该用例自动跳过。
该用例在独立子进程内启动 uvicorn 被测服务，并以子进程复用
`claim/finish/solve` 驱动持久队列；队列消费只以执行终态为准，
因此即使同时存在常驻 worker 抢锁，验收结果也保持确定。

回滚用例（`test_03`）通过 `domain.replay(..., failpoint=...)` 在批次事务
「位点 SQL 已执行、COMMIT 未发出」处注入异常，直接在同一 MySQL 会话核对
投影、位点和日志均回滚到上一批，再重试批次并验证一起提交、记录不丢失。

