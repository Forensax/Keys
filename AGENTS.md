# Repository Guidelines

## 项目结构与模块组织

本仓库是个人使用的 FastAPI + Jinja2 + Vanilla JS + SQLite 中转站管理工具。核心后端在 `app/`：`main.py` 定义路由，`models.py`、`db.py` 管理数据模型与连接，`security.py` 处理登录和加密，`scheduler.py`、`analytics.py`、`proxy_support.py`、`openai_compat.py`、`notifications.py` 分别负责后台任务、统计、代理、OpenAI 兼容测试和 Telegram 通知。模板在 `app/templates/`，前端脚本和样式在 `app/static/`，测试在 `tests/`，部署脚本在 `scripts/`。`data/` 保存本地运行数据库，不作为普通代码改动处理。

## 与用户协作方式

回复和文档优先使用中文，说明要直接、可执行。用户希望 Agent 像当前 Codex 一样：先读代码和测试，理解现有模式，再小步修改、验证、提交、推送、同步和重建 Docker。

若用户说“继续完成任务”或给出 `PLEASE IMPLEMENT THIS PLAN`，应视为授权实施该计划。每次修改前检查 `git status`；工作区可能有用户或前序 Agent 的未提交改动，禁止回滚或覆盖无关改动。只暂存本次任务相关文件。

## 构建、测试与开发命令

- `pip install -r requirements.txt`：安装运行和测试依赖。
- `uvicorn app.main:app --reload --host 127.0.0.1 --port 18000`：本地开发启动。
- `pytest`：运行完整测试套件。
- `pytest tests/test_app.py`、`pytest tests/test_scheduling_statistics.py`、`pytest tests/test_openai_compat.py`：按改动范围运行重点测试。
- `node --check app/static/app.js`：修改前端脚本后做语法检查。
- `git diff --check`：提交前检查空白和换行问题。
- `docker compose build --pull && docker compose up -d`：本地 Docker 构建并启动。
- `.\scripts\synology-redeploy.ps1 -NasHost keys-nas -RemotePath /volume1/docker/Keys -UseSudo`：通过 SSH 在群晖上重建 Docker Compose 服务。

## 编码风格与实现约束

Python 使用 4 空格缩进、类型标注和 `snake_case` 命名，优先复用现有 helper、模型和路由结构。Jinja2 模板只保留展示逻辑，复杂计算放后端。前端交互集中在 `app/static/app.js`，样式集中在 `app/static/styles.css`。避免无关重构、批量格式化和跨模块大改；新增能力要贴近现有表单、自动保存、统计筛选、测试历史等交互模式。

页面可见时间应统一走 `format_local_datetime()`，部署默认 `APP_TIMEZONE=Asia/Shanghai`。数据库和 JSON 备份时间继续保持 ISO/UTC 兼容格式，不为了展示需求改存储格式。

## 测试指南

测试框架为 pytest 和 pytest-asyncio，配置见 `pytest.ini`。测试文件命名为 `tests/test_*.py`，用例函数以 `test_` 开头。涉及路由、数据库迁移、统计聚合、调度、安全、代理、OpenAI 兼容协议、自动保存或页面文案的改动必须补测试。优先运行相关测试；风险较高、共享逻辑变化或部署前有时间时运行完整 `pytest`。

## 提交、推送与默认部署流程

用户已指定：后续所有修改完成后默认执行提交、推送、同步到 `Z:\Keys`、重建 Docker。推荐流程：

1. `git status --short` 确认变更范围，只暂存本任务文件。
2. 运行相关测试和 `git diff --check`，在最终回复中说明结果。
3. 用简短提交信息提交，例如 `chore: update agent guidelines`、`feat: add OpenAI responses profile`、`Fix home table alignment`。
4. `git push origin master`。
5. 同步 Git 跟踪文件到 `Z:\Keys`，保护 `Z:\Keys\.env` 和 `Z:\Keys\data\keys.db`，不要覆盖本地运行配置和数据库。
6. 运行群晖重建脚本：`.\scripts\synology-redeploy.ps1 -NasHost keys-nas -RemotePath /volume1/docker/Keys -UseSudo`。
7. 检查容器状态和 `http://10.10.10.10:18000/health`。

如果只是文档小改，仍按用户的默认偏好执行上述链路，除非用户明确说不用部署。

## SSH、同步与敏感信息

NAS 连接使用 SSH 配置别名 `keys-nas` 或主机 `10.10.10.10`，用户为 `aiden`。密码登录由脚本支持，默认密码文件为 `C:\Users\<用户名>\.ssh\keys-nas.password`，也可用 `-PasswordFile <路径>` 指定。不要把 SSH 密码、API Key、`.env`、`data/keys.db` 或包含明文密钥的导出文件提交到 Git，也不要写进文档正文。同步或部署时必须保留生产数据卷，禁止执行会清空数据库的命令。

## Pull Request 与交付说明

提交信息保持简短、说明行为，沿用现有祈使式或类型前缀风格。PR 或最终交付应说明变更目的、主要文件、测试结果、部署结果和剩余风险；涉及 UI 的改动说明可见变化，涉及数据或 Docker 的改动说明兼容性。最终回复不要让用户手动复制文件；用户和 Agent 在同一工作区，直接说明已完成什么、验证了什么即可。
