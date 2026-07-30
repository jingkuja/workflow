# 新媒体内容制作工作流后台

当前仓库已完成 T0 技术验证、T1 工程底座、T2 选题与演播稿闭环，以及 T4
架构收口、看板和运维加固，提供：

- 老板、员工两个独立的 Streamable HTTP MCP 入口。
- 固定 Token 鉴权和不同工具白名单。
- `.docx`、`.pdf`、`.md`、`.txt` 文件字段探针。
- 最大约 100 MB Base64 视频分块解码与落盘探针。
- 随机文件 ID 和可点击下载路由。
- 企业微信群机器人 `userid` @探针，默认禁止真实发送。
- Docker Compose、Nginx、健康检查、单元测试和联调脚本。
- PostgreSQL 16、Alembic 迁移和 `.env` 人员同步（数据库只保存 Token 哈希）。
- 首版业务模型、追加式审计、幂等请求指纹和 SQLAlchemy 乐观锁。
- 支持租约、超时重领和递增重试的 PostgreSQL Worker。
- 企业微信事务发件箱及事件去重、重试和 DEAD 状态。
- `storage_provider + storage_key` 附件模型、本地原子落盘和磁盘阈值保护。
- 数据库备份、隔离恢复脚本和 Docker 日志轮转。
- WorkBuddy 大模型结构化抽取任意排版 Word，后台信任 MCP 结果、保存原文件，并按“文件 + 任务内容”去重入库。
- 工作日生效规则、任务编号和按周工作量最小值随机分配。
- 导入任务删除、改派、优先级以及老板/员工资源权限隔离。
- `.docx`、`.pdf`、`.md`、`.txt` 演播稿多版本提交、驳回、重提和通过。
- 追加式分配记录、审计日志和通知发件箱事件。
- MCP → 内部工作流 API 双层鉴权；MCP 容器不持有业务 PostgreSQL 凭据。
- 统一 `request_id`、稳定错误码、修复建议和状态相关 `next_actions`。
- T2 概览、目标差额、项目分页筛选、审计时间线、待审核中心和人员负载。
- 员工阻塞幂等上报，以及失败后台任务、通知和阻塞事项查询。
- `WAITING_FOR_FILMING` 作为当前正常终态，不暴露冻结的 T3 操作。

## 本地启动

```bash
cp .env.example .env
make compose-up
docker compose ps
```

首次启动顺序为 PostgreSQL 健康检查 → `db-migrate` → API/MCP/Worker → Nginx。
只有 Nginx 的 8080 端口暴露给宿主机。
Nginx 使用 Docker DNS 动态解析 MCP/API 上游，应用容器重建后不会沿用旧 IP。

默认地址：

- 老板 MCP：`http://localhost:8080/mcp/boss`
- 员工 MCP：`http://localhost:8080/mcp/employee`
- 反向代理健康检查：`http://localhost:8080/health`
- 下载路由：`http://localhost:8080/files/{opaque_file_id}`

`.env.example` 中的 Token 仅用于本机验证，部署前必须替换。
部署到真实子域名时设置 `PUBLIC_BASE_URL=https://你的域名`，系统会自动把该
域名加入 MCP Host/Origin 白名单；仅当前置网关改写了 `Host` 时，才需要把改写
后的值追加到 `MCP_ALLOWED_HOSTS`。服务始终保留 MCP SDK 的 DNS 重绑定保护。

WorkBuddy 应配置完整入口 `/mcp/boss` 或 `/mcp/employee`，不要使用 `/mcp`。
本地文件统一使用两步工具链：

1. 明确调用 `upload_file`。它的 `file_base64` 参数带有
   `contentEncoding: base64`、`format: byte`，由 MCP Host 读取本地文件并填充，
   不是模型生成的文本。
2. 使用返回的 `file_key` 调用 `import_structured_topics`、
   `import_topic_document` 或 `submit_script_file`。业务工具不再接收 Base64。

`file_key` 绑定上传人和公司，默认 24 小时有效；过期后重新调用 `upload_file`。
老板上传普通 `.docx` 时应优先调用 `import_structured_topics`：WorkBuddy 负责理解
文档并生成结构化选题，后台直接保存任务与原文件，并负责去重、分配和审计。
`import_topic_document` 仅保留给旧的固定标题模板。

## 开发与测试

```bash
make install
make lint
make test
```

T1 基础能力冒烟：

```bash
docker compose exec -T workflow-api python -m workflow.scripts.t1_smoke
```

T2 真实样例端到端冒烟：

```bash
set -a
source .env
set +a
.venv/bin/python -m workflow.scripts.t2_smoke --concurrency-check
```

该命令通过真实老板/员工 MCP 入口验证：样例解析 10 条、并发导入只创建一个
批次、哈希重复识别、员工本人权限、幂等提交，以及“提交 → 驳回 → 重提 →
通过”闭环。详见 `docs/T2-选题与演播稿闭环验收记录.md`。

T4 看板、分页、运维查询和 `request_id` 契约冒烟：

```bash
set -a
source .env
set +a
.venv/bin/python -m workflow.scripts.t4_smoke
```

T4 部署与故障处理见 `docs/T4-部署与运维手册.md`；T4 自动化证据和当前 20 项
映射见 `docs/T4-T2收口与加固验收记录.md`。

数据库备份和隔离恢复：

```bash
scripts/backup_db.sh backups
scripts/restore_db_test.sh backups/你的备份.dump workflow_restore_test
```

启动 Compose 后执行 MCP 冒烟：

```bash
set -a
source .env
set +a
.venv/bin/python -m workflow.scripts.t0_smoke
```

附带 Word 文件探针：

```bash
.venv/bin/python -m workflow.scripts.t0_smoke \
  --document "docs/AI行业选题文档上传样例.docx"
```

生成接近 100 MB 的本地 MP4 测试文件：

```bash
scripts/generate_t0_video.sh t0-artifacts/t0-100mb.mp4
```

然后执行大文件探针：

```bash
.venv/bin/python -m workflow.scripts.t0_smoke \
  --video t0-artifacts/t0-100mb.mp4
```

Base64 只出现在独立的 `upload_file` 调用中，不再和选题、任务、备注等业务字段
共享一个工具请求。MCP SDK 和 JSON 解析层仍可能先把完整 Base64 字符串载入
内存，因此仍需使用真实 WorkBuddy 请求验证单次大文件上传的内存峰值。

## 企业微信验证

在服务器 `.env` 配置：

```dotenv
MCP_BOSS_WECOM_USERID=老板userid
MCP_EMPLOYEES_JSON='[{"name":"员工姓名","token":"随机Token","wecom_userid":"员工userid","active":true}]'
WECOM_GROUP_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...
T0_ALLOW_WECOM_SEND=true
```

然后通过老板 MCP 调用 `t0_probe_wecom_mention`。默认值为 `false`，防止本地测试误发群通知。

详细步骤和结果登记见 `docs/T0-技术验证与联调记录.md`。
