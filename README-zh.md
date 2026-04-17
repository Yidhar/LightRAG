# RagEngine

基于图谱的 RAG 平台 —— 每个工作区下独立知识库，用户之间数据隔离，Web UI
完成文档导入、检索、图谱查看。

底层基于 LightRAG 框架。本 README 只讲 **本地部署**。框架细节见
[docs/](docs/)。

---

## 环境依赖

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- [Bun](https://bun.sh/) 1.1+（构建 WebUI 用）
- 一个 LLM + embedding 服务（OpenAI / Ollama / Azure / …）

---

## 快速开始

```bash
# 1. 安装 Python 依赖
uv sync --extra api

# 2. 构建 WebUI
cd lightrag_webui
bun install --frozen-lockfile
bun run build
cd ..

# 3. 一键初始化 —— 生成 .env、TOKEN_SECRET、auth 数据库
uv run python scripts/bootstrap_deployment.py

# 4. 配置你的 LLM 提供商
#    打开 .env，填入 LLM_BINDING / LLM_MODEL / LLM_BINDING_API_KEY
#    以及 EMBEDDING_BINDING / EMBEDDING_MODEL / EMBEDDING_DIM

# 5. 启动服务
uv run lightrag-server
```

浏览器打开 [http://localhost:9621](http://localhost:9621)。

第一次访问登录页会显示 **"首次使用：创建管理员账号"** 横幅 —— 填入用户名
和密码（至少 8 位），提交后你就是第一个管理员。

之后其他用户通过登录页的 **注册** tab 自助注册即可；每次注册会自动创建一
个属于该用户的私人工作区。

---

## `bootstrap_deployment.py` 做了什么

幂等，重复跑安全。

1. `.env` 不存在时从 `env.example` 复制
2. 生成 48 字节 `TOKEN_SECRET` 写入 `.env`（已设置的非占位值保留不变）
3. 确保以下平台开关已开启：
   - `USE_DB_AUTH=true`
   - `DB_URL=sqlite+aiosqlite:///./lightrag_auth.db`
   - `ENABLE_KB_ISOLATION=true`
   - `LIGHTRAG_ALLOW_SELF_REGISTRATION=true`
4. 建立 sqlite 数据库（users / workspaces / memberships / refresh_tokens /
   audit_log）

加 `--skip-db` 可以跳过建库，等服务器首次启动时再建（用于目标数据库还连不
上的场景）。

---

## 关闭自助注册

企业私有部署里每个账号都必须由管理员创建时，编辑 `.env`：

```
LIGHTRAG_ALLOW_SELF_REGISTRATION=false
```

重启后注册 tab 消失，管理员通过 WebUI 的 **Members** 页面添加用户。

---

## 生产环境注意

- **`TOKEN_SECRET`** —— 生产前务必旋转脚本生成的随机值。`.env` 千万别进
  git（已经在 `.gitignore` 里）
- **数据库** —— 本地开发默认 sqlite。多 worker 部署请把 `DB_URL` 换成
  Postgres（`postgresql+asyncpg://user:pass@host/db`），schema 一致
- **HTTPS** —— 在 `.env` 里设置 `SSL=true`、`SSL_CERTFILE`、`SSL_KEYFILE`
- **多进程** —— 用 `lightrag-gunicorn` 启动 gunicorn 多 async worker

---

## Docker

```bash
docker compose up -d
```

Compose 文件在仓库根目录（`docker-compose.yml` / `docker-compose-full.yml`）。
用 `scripts/setup/setup.sh` 可以交互式生成定制 compose profile。

---

## 开发模式

```bash
# 后端热重载
uv run uvicorn lightrag.api.lightrag_server:app --reload

# 前端开发服务器（Vite 监听 :5173，代理到 :9621）
cd lightrag_webui && bun run dev
```

Lint / 测试：

```bash
ruff check .
uv run pytest tests                    # 离线测试
uv run pytest tests --run-integration  # 需要外部服务

cd lightrag_webui
bun run lint
bun test
```

---

## 清空本地状态

```bash
# 清空所有平台表：身份、工作区、成员关系、审计日志
uv run python scripts/reset_platform_db.py --confirm

# 再次跑 bootstrap 重建 schema
uv run python scripts/bootstrap_deployment.py
```

文档存储默认在 `./rag_storage/`，删掉该目录即可清空已导入的文档、图谱和
向量数据。
