# Keys

一个面向个人使用的 OpenAI 兼容中转站管理工具。它可以把不同中转站的 Base URL、API Key、模型列表、连通性测试记录和网络代理保存到本地 SQLite 数据库中，其中 API Key 与代理认证信息会加密存储。

## 功能

- 单用户初始化和密码登录。
- API Key 使用应用密码派生出的密钥进行加密，不以明文保存到数据库。
- 中转站管理：名称、Base URL、API Key、备注、启用状态和默认客户端模式。
- 支持可选分组：新增或编辑时可选择已有分组或输入新分组，主页按分组首次创建顺序展示。
- 支持软归档：归档后从主页隐藏，可在独立归档页查看、恢复或永久删除。
- 从 `GET {base_url}/models` 拉取模型列表。
- 支持手动添加和删除模型；手动模型始终排在接口刷新模型前面，刷新时不会被删除。
- 支持标准 OpenAI、Codex 和 Claude Code 三种客户端兼容测试模式。
- 模型列表不可用时，可以直接输入模型名称执行连通性测试。
- 连通性测试的模型、客户端模式和网络路径会自动保存。
- 首页支持每行单独“测试”和“测试全部”；批量测试最多并发测试 5 个已启用中转站，并逐行更新最近结果。
- 支持多个定时任务，可按全部已启用站点、分组或指定站点运行，并支持 15 分钟至 7 天的间隔或每天固定时间。
- 提供测试统计仪表盘，支持真实历史的时间、站点和来源筛选，以及成功率、延迟趋势、站点排行和失败原因分析。
- 支持 HTTP、HTTPS、SOCKS5 和 SOCKS5H 网络代理，可为每个中转站设置默认代理。
- 连通性测试可以临时选择默认代理、直连或其他已启用代理，并在历史中记录实际网络路径。
- 支持 JSON 导入/导出；导出明文 API Key 和代理认证信息时需要再次输入密码确认。
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

项目提供 `Dockerfile` 和 `docker-compose.yml`，应用镜像使用 LinkOS 公共镜像站的 `docker.linkos.org/library/python:3.11-slim` 作为基础镜像。Docker 构建默认通过阿里云 PyPI 镜像安装 Python 依赖，以缩短国内网络环境下的构建时间。

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

如需切换 PyPI 镜像，在 `.env` 中修改：

```dotenv
DOCKER_PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
```

例如切回 PyPI 官方源：

```dotenv
DOCKER_PIP_INDEX_URL=https://pypi.org/simple/
```

也可以只为单次构建临时指定：

```powershell
$env:DOCKER_PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple/"
docker compose build --pull
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

### 群晖快速重建

如果 `Z:\Keys` 是映射到群晖的 Docker 项目目录，可以用 SSH 一条命令代替 Container Manager 里的“清除项目 / 移除未使用镜像 / 重新构建项目”。先在 DSM 控制面板开启 SSH，并确认项目在群晖里的真实 Linux 路径，例如 `/volume1/docker/Keys`。

```powershell
cd F:\Project\Keys
.\scripts\synology-redeploy.ps1 -NasHost 192.168.1.10 -NasUser admin -RemotePath /volume1/docker/Keys -UseSudo
```

如果你选择把 SSH 密码保存为本地文件，可以放在默认路径：

```powershell
C:\Users\<你的用户名>\.ssh\keys-nas.password
```

脚本检测到该文件后会自动使用密码登录 SSH；如果同时传入 `-UseSudo`，也会用同一个密码响应群晖上的 `sudo`。也可以通过 `-PasswordFile <路径>` 指定其他密码文件。

脚本在群晖上执行的流程等价于：

```sh
docker compose down --remove-orphans
docker compose build --pull --no-cache
docker compose up -d --force-recreate --remove-orphans
docker image prune -f
docker compose ps
```

默认不会删除卷，也不会执行 `docker system prune -a`，所以不会清除 `data/keys.db`。如果你的群晖账号已经有 Docker 权限，可以去掉 `-UseSudo`；如果想保留构建缓存来加速，可加 `-UseBuildCache`。

脚本默认只清理悬空镜像。若确认要清理所有未被容器使用的镜像，可加 `-PruneAllUnusedImages`，对应 `docker image prune -af`。

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
- `APP_TIMEZONE`：定时任务和统计页面的展示时区，默认是 `Asia/Shanghai`。
- `HOST`、`PORT`：用于文档说明的本地运行默认值。
- `DOCKER_BIND_ADDRESS`：Docker Compose 发布端口时绑定的宿主机地址，默认是 `0.0.0.0`。
- `DOCKER_PIP_INDEX_URL`：Docker 构建时使用的 PyPI 镜像，默认是阿里云镜像；只影响镜像构建，不影响应用运行。
- `COOKIE_SECURE`：通过 HTTPS 访问时设为 `true`。
- `REQUEST_TIMEOUT_SECONDS`：访问中转站接口时的请求超时时间。
- `PROXY_TEST_URL`：代理出口测试的固定目标，默认是 `https://api.ipify.org?format=json`。该值只能由部署环境配置，不能从网页提交。

## 网络代理

导航中的“网络代理”页面用于维护 HTTP、HTTPS、SOCKS5 和 SOCKS5H 代理。代理用户名和密码与 API Key 一样，使用当前应用密码派生的 Fernet 密钥加密后保存；列表页只显示代理地址和是否包含认证信息。

中转站未设置默认代理时使用直连。刷新模型始终使用中转站的默认网络路径；详情页执行连通性测试时，可以临时选择“使用默认”“直连”或其他已启用代理，该选择不会修改中转站配置。测试历史中的“网络”列保存当次使用的代理名称快照，因此代理改名或删除后历史记录仍然可读。

代理被禁用、删除、配置无效或凭据无法解密时，请求会在连接目标接口前失败，不会自动回退直连。所有直连和代理请求均设置 `trust_env=False`，不会隐式读取服务器的 `HTTP_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY` 环境变量。

代理列表中的“测试”按钮通过该代理访问 `PROXY_TEST_URL`，只保留最近一次出口 IP、延迟和错误。普通网页用户不能指定测试地址，以避免将该功能变成任意 URL 请求入口。

JSON 默认备份包含分组及其创建顺序、代理名称、地址、启用状态和认证存在标记，但不包含用户名或密码。勾选“包含明文 API Key 和代理认证信息”并再次确认应用密码后，备份才会包含可用于完整恢复的敏感信息。请把此类备份视为明文密钥文件妥善保管。

## 定时任务

导航中的“定时任务”页面用于创建、编辑、启停和立即运行后台连通性测试。任务可以覆盖全部未归档且启用的中转站、一个分组，或一组指定站点；指定站点在运行时若已禁用、归档或删除，会记录为跳过。所有任务共享最多 5 个并发请求，同一任务不会重叠执行。应用停机期间错过的多次执行会在恢复后合并为一次，然后继续计算下一个未来时间。

后台任务首次使用前需要再次输入应用密码授权。系统用现有 encryption salt 和密码派生 vault key，再用稳定的 `SESSION_SECRET` 经 HKDF-SHA256 派生出的独立包装密钥加密保存；数据库不会保存明文密码、API Key 或 vault key。`SESSION_SECRET` 必须显式配置并保持不变，修改后调度器会锁定且不会发送外部请求，需要重新授权。“锁定后台密钥”会删除包装密钥、停用全部任务并清空下次执行时间。

定时任务生成的测试历史和任务运行记录保留 180 天，每天自动清理一次；手动测试历史不会被此策略删除。本版本只支持单个 Uvicorn 进程，不能使用多个 worker，也不提供分布式锁或外部任务队列。

## 测试统计

“统计”页面默认读取真实 `connectivity_tests` 历史，支持最近 24 小时、7 天、30 天、90 天或全部历史，并可按站点和手动/定时来源筛选。页面展示测试总数、成功率、失败数、平均与 P95 延迟、被测试站点数，以及成功/失败趋势、前 5 个高频站点延迟趋势、站点排行、失败原因 Top 8 和来源分布。24 小时范围按小时聚合，其余范围按天聚合。

页面图表使用项目内的 HTML、CSS、SVG 和 Vanilla JavaScript，不依赖 CDN；每个图形标记都提供键盘焦点和数值提示，同时保留站点表现表格供精确读取。

JSON 备份版本 6 会导出定时任务定义和目标映射，但不会导出后台包装密钥、任务运行历史或完整测试历史。导入时按分组名称以及站点名称 + Base URL 重新映射目标，所有导入任务都会保持停用，必须重新授权并由用户手动启用。旧版本 JSON 备份仍可继续导入。

## 兼容范围

第一版只面向标准 OpenAI 兼容接口：

- 鉴权方式：`Authorization: Bearer <api_key>`
- 模型列表：`GET {base_url}/models`，请求会携带中转站默认客户端模式对应的 Header。
- 标准 OpenAI：`POST {base_url}/chat/completions`
- Codex：`POST {base_url}/responses`
- Claude Code：`POST {base_url}/messages`

每个中转站可以保存一个默认客户端模式。详情页选择的测试模式会独立保存供后续测试使用，但不会修改中转站默认客户端模式。测试历史会记录每次实际使用的模式。JSON 备份中的 `client_profile` 可取 `openai_chat`、`codex` 或 `claude_code`；旧备份没有该字段时按标准 OpenAI 导入。

详情页会独立保存最近选择的测试模型、客户端模式和网络路径，这些设置用于下一次详情页测试、首页单行“测试”和首页“测试全部”。首页模型列表点击后仍会复制模型名称，同时把该模型保存为首页测试模型。首页单行“测试”会直接更新当前行的最近测试结果；“测试全部”仅处理未归档且启用的中转站。缺少模型或代理不可用时不会发送外部请求，但会记录一条失败结果；禁用中转站的首页单行测试会被跳过且不写入测试历史。

Codex 模式会按当前 Codex Desktop 的 Responses 协议发送流式请求，包括结构化消息、`prompt_cache_key`、session/thread/turn/window 关联信息和客户端元数据。测试请求不启用工具，只要求模型简短返回 `pong`；系统会从 SSE 响应中提取实际文本并保存到测试结果。若错误中仍出现 `detected: Go-http-client/1.1`，说明中间层没有把 Codex 客户端 Header 转发给最终上游，需要由该中转站管理员启用请求头透传。

这些内置模式用于模拟对应客户端的 HTTP 请求特征，但无法绕过 TLS 指纹、设备证明、动态签名或服务端账号策略。Azure OpenAI 的 deployment/api-version 路径和任意自定义 Header 等能力暂不包含在当前范围内。

## 测试

```powershell
pytest
```
