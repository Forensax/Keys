# Keys

一个面向个人使用的 OpenAI 兼容中转站管理工具。它可以把不同中转站的 Base URL、API Key、模型列表和连通性测试记录保存到本地 SQLite 数据库中，其中 API Key 会加密存储。

## 功能

- 单用户初始化和密码登录。
- API Key 使用应用密码派生出的密钥进行加密，不以明文保存到数据库。
- 中转站管理：名称、Base URL、API Key、备注、启用状态和默认客户端模式。
- 支持软归档：归档后从主页隐藏，可在独立归档页查看、恢复或永久删除。
- 从 `GET {base_url}/models` 拉取模型列表。
- 支持标准 OpenAI、Codex 和 Claude Code 三种客户端兼容测试模式。
- 模型列表不可用时，可以直接输入模型名称执行连通性测试。
- 支持 JSON 导入/导出；导出明文 API Key 时需要再次输入密码确认。
- 默认使用本地 SQLite 数据库，适合个人工具场景。

## 环境要求

- Python 3.11 或更高版本。

当前这台机器上的 `python` 可能指向 Microsoft Store 的占位入口。如果命令无法正常运行，请从 [python.org](https://www.python.org/downloads/) 安装 Python，或使用一个明确可用的 Python 可执行文件路径。

## 快速开始

如果你使用 Conda，推荐按下面的方式创建独立环境：

```powershell
cd F:\Project\Keys
conda create -n keys python=3.11 -y
conda activate keys
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload --host 127.0.0.1 --port 18000
```

如果你不使用 Conda，也可以使用 Python 自带的 venv：

```powershell
cd F:\Project\Keys
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload --host 127.0.0.1 --port 18000
```

启动后打开 `http://127.0.0.1:18000`。首次进入会要求创建应用密码，之后就可以添加和管理中转站。

之后再次启动时，如果使用 Conda，只需要：

```powershell
cd F:\Project\Keys
conda activate keys
uvicorn app.main:app --reload --host 127.0.0.1 --port 18000
```

## Docker Compose 部署

项目提供 `Dockerfile` 和 `docker-compose.yml`，应用镜像使用 LinkOS 公共镜像站的 `docker.linkos.org/library/python:3.11-slim` 作为基础镜像。

首次部署时创建环境变量文件：

```powershell
Copy-Item .env.example .env
```

打开 `.env`，务必把 `SESSION_SECRET` 替换为足够长的随机字符串。可以使用下面的命令生成：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

构建并启动容器：

```powershell
docker compose build --pull
docker compose up -d
```

常用管理命令：

```powershell
# 查看运行状态
docker compose ps

# 持续查看应用日志
docker compose logs -f keys

# 停止并删除容器，不会删除 ./data 中的数据库
docker compose down
```

更新代码后重新构建并启动：

```powershell
git pull
docker compose build --pull
docker compose up -d
```

容器内的 SQLite 数据库固定为 `/app/data/keys.db`，并通过 `./data:/app/data` 保存到宿主机，因此重建容器不会删除中转站数据。备份时复制宿主机的 `data/keys.db` 即可。

Compose 默认通过 `0.0.0.0:18000` 向所有网络接口开放服务。只允许本机访问时，在 `.env` 中设置：

```dotenv
DOCKER_BIND_ADDRESS=127.0.0.1
```

如果宿主机已有程序占用 `18000`，请先停止该程序，或者修改 `.env` 中的 `PORT`。公网部署必须配置防火墙和 HTTPS 反向代理。

## 公网部署注意事项

这个工具按个人使用场景设计。如果你要把它暴露到公网，请务必注意：

- 放在 HTTPS 后面，建议使用 Nginx、Caddy、Cloudflare Tunnel 或其他能终止 TLS 的反向代理。
- 在 `.env` 中把 `SESSION_SECRET` 设置为足够长的随机字符串。
- 通过 HTTPS 访问时，将 `COOKIE_SECURE=true`。
- 只有在明确理解网络暴露风险时，才把服务绑定到 `0.0.0.0`。
- 定期备份 `data/keys.db`。

公网或局域网访问时的启动示例：

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 18000
```

## 配置项

应用会自动读取项目根目录下的 `.env` 文件。可用环境变量如下：

- `APP_NAME`：页面显示的应用名称。
- `DATABASE_URL`：SQLAlchemy 数据库地址，默认是 `sqlite:///./data/keys.db`。
- `SESSION_SECRET`：Cookie 签名密钥。开发环境可以省略，但生产或公网部署必须显式设置。
- `HOST`、`PORT`：用于文档说明的本地运行默认值。
- `DOCKER_BIND_ADDRESS`：Docker Compose 发布端口时绑定的宿主机地址，默认是 `0.0.0.0`。
- `COOKIE_SECURE`：通过 HTTPS 访问时设为 `true`。
- `REQUEST_TIMEOUT_SECONDS`：访问中转站接口时的请求超时时间。

## 兼容范围

第一版只面向标准 OpenAI 兼容接口：

- 鉴权方式：`Authorization: Bearer <api_key>`
- 模型列表：`GET {base_url}/models`，请求会携带中转站默认客户端模式对应的 Header。
- 标准 OpenAI：`POST {base_url}/chat/completions`
- Codex：`POST {base_url}/responses`
- Claude Code：`POST {base_url}/messages`

每个中转站可以保存一个默认客户端模式。详情页测试时可以临时切换模式，该选择只影响本次请求，不会修改中转站默认配置。测试历史会记录每次实际使用的模式。JSON 备份中的 `client_profile` 可取 `openai_chat`、`codex` 或 `claude_code`；旧备份没有该字段时按标准 OpenAI 导入。

Codex 模式会发送 `prompt_cache_key` 和对应的 `Session_id`。这两个字段除模拟 Codex CLI 请求外，也用于触发新版 new-api 内置的 Codex CLI 请求头透传规则。若错误中仍出现 `detected: Go-http-client/1.1`，说明中间层没有把客户端 User-Agent 转发给最终上游，需要由该 new-api 实例的管理员启用“Codex CLI 请求头透传”或升级到包含该功能的版本，客户端无法单方面修复服务端丢弃 Header 的行为。

这些内置模式用于模拟对应客户端的 HTTP 请求特征，但无法绕过 TLS 指纹、设备证明、动态签名或服务端账号策略。Azure OpenAI 的 deployment/api-version 路径、任意自定义 Header、定时后台测试等能力暂不包含在当前范围内。

## 测试

```powershell
pytest
```
