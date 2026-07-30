# 隔离的 Docker 数据服务

这套 Compose 只创建 Docker 内部的数据服务，不连接或修改 NanoClaw 当前使用的本地数据库。

当前状态：M1-M5 已完成；M6 技术门禁通过，但因诊断输出中的隔离凭据需要轮换，操作安全验收暂未签署。原有五服务的验收证据见 `M1_ACCEPTANCE.md` 至 `M6_ACCEPTANCE.md`；新增 Elasticsearch 需在具备 Docker 权限的主机上另行完成实机验收。

## 隔离保证

- 不挂载仓库中的 SQLite、MySQL、知识库或会话数据目录；
- 不挂载或执行 `agent/business/migrations/` 中的迁移；
- 不修改根目录 `.env.example`、`.env`、`config.py` 或应用后端选择；
- 所有数据只写入 Compose 管理的 named volumes；
- MySQL 使用宿主端口 `3307`、PostgreSQL 使用 `5433`，避免接触常见本地端口 `3306`/`5432`；
- Elasticsearch 使用宿主端口 `9201`，只保存可从权威知识仓库重建的关键词索引；
- 所有对外端口仅绑定 `127.0.0.1`；etcd、MinIO 和 Milvus 管理端口不发布到宿主机；
- 普通停止命令不删除 volumes。不要运行 `docker compose down -v`，除非明确决定删除 Docker 数据。

首次使用时复制 Docker 专用环境模板并替换示例密码。当前 M1 已在 Git 忽略的 `deploy/docker/.env` 中生成随机密码，不要提交、打印或覆盖该文件：

```powershell
Copy-Item deploy\docker\.env.example deploy\docker\.env
```

只启动 MySQL：

```powershell
docker compose --env-file deploy\docker\.env `
  -f deploy\docker\compose.yaml --profile business up -d
```

只启动 PostgreSQL/pgvector：

```powershell
docker compose --env-file deploy\docker\.env `
  -f deploy\docker\compose.yaml --profile vector-local up -d
```

M2 Schema 只通过显式命令应用到 Docker PostgreSQL，不会影响本地数据库：

```powershell
$env:RAG_VECTOR_BACKEND='pgvector'
$env:RAG_VECTOR_DIMENSIONS='64'
# 将 Docker 专用连接变量映射到 RAG_PGVECTOR_* 后再执行
\.venv\Scripts\python.exe -m trade_rag.pgvector_migration --check
\.venv\Scripts\python.exe -m trade_rag.pgvector_migration --apply
```

只启动 Milvus、etcd 和 MinIO：

```powershell
docker compose --env-file deploy\docker\.env `
  -f deploy\docker\compose.yaml --profile vector-milvus up -d
```

M3 已启用 Milvus 鉴权。先运行 `generate_secrets.ps1` 补齐被忽略的 Docker `.env`，再使用 root 管理 token 显式执行 `python -m trade_rag.milvus_admin` 的鉴权初始化、代次创建、alias 切换或回滚。应用只使用受限 token，默认后端仍为 `memory`。

M4 应用运行时通过 `RAG_VECTOR_BACKEND=memory|sqlite|pgvector|milvus` 选择后端。当前本机 `.env` 使用 SQLite 持久索引；未加载项目 `.env` 时的代码兜底仍为 `memory`。Web提供 `/healthz`、`/readyz`、受保护的 `/api/rag/metrics` 和 `/metrics/rag`；后端故障不会回退到另一数据源。告警规则示例见 `rag-alert-rules.example.yaml`。

只启动 Elasticsearch 关键词服务：

```powershell
.\deploy\docker\generate_secrets.ps1
docker compose --env-file deploy\docker\.env `
  -f deploy\docker\compose.yaml --profile keyword-elasticsearch up -d
```

本机 `.env` 默认使用 `RAG_KEYWORD_BACKEND=sqlite` 的 FTS5 BM25，不连接 Elasticsearch。需要让宿主机上的 NanoClaw 显式使用 Docker Elasticsearch 时，将后端改为 `elasticsearch`，安全映射 Docker 密码，然后检查并重建派生索引：

```powershell
$dockerSettings = Get-Content deploy\docker\.env -Encoding utf8 | ConvertFrom-StringData
$env:RAG_KEYWORD_BACKEND = 'elasticsearch'
$env:RAG_ELASTICSEARCH_URL = 'http://127.0.0.1:9201'
$env:RAG_ELASTICSEARCH_USERNAME = 'elastic'
$env:RAG_ELASTICSEARCH_PASSWORD = $dockerSettings.DOCKER_ELASTIC_PASSWORD
$env:RAG_ELASTICSEARCH_INDEX = 'trade_knowledge_child_v1'

.\.venv\Scripts\python.exe -m trade_rag.elasticsearch_reindex --check
.\.venv\Scripts\python.exe -m trade_rag.elasticsearch_reindex
.\.venv\Scripts\python.exe -m trade_rag.elasticsearch_reindex --apply
```

不带 `--apply` 的重建命令只输出待索引统计，不写 ES。`--apply` 幂等 upsert 当前已发布文档，并清理 manifest 中已撤销或不再有效的文档版本。ES 查询失败时混合检索可降级为 semantic-only，但不会切换到内存关键词索引；ES 写入失败会使知识索引操作失败。

M5 使用脱敏 fixture 演练 MySQL/pgvector/Milvus 的备份、恢复、停写切换和回滚。运行方式、安全门禁和保留策略见 `M5_RUNBOOK.md`。临时 `migration` profile 只增加固定 digest 的 Milvus Backup 服务并绑定回环端口 `18080`；它不改变默认应用后端。

M6 使用 `python -m trade_rag.m6_release full` 执行 10,000 向量容量、混合入口并发、可逆故障和安全治理门禁。运行方式、硬门槛与恢复步骤见 `M6_RUNBOOK.md`。005 迁移和 MySQL 会话/协调后端只在显式 M6 配置中启用；普通应用仍使用本地会话存储和 memory 向量后端。

启动全部隔离服务：

```powershell
docker compose --env-file deploy\docker\.env `
  -f deploy\docker\compose.yaml --profile all up -d
```

查看状态与停止服务（保留 Docker volumes）：

```powershell
docker compose --env-file deploy\docker\.env `
  -f deploy\docker\compose.yaml ps

docker compose --env-file deploy\docker\.env `
  -f deploy\docker\compose.yaml stop
```

当前 Compose 不创建业务表、不导入应用数据，也没有把 NanoClaw 切换到这些服务。pgvector 镜像包含扩展程序，但本轮不会对现有本地 PostgreSQL 执行 `CREATE EXTENSION`；后续若需要测试，应只在这个 Docker PostgreSQL 的独立数据库中操作。
