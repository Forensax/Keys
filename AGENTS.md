# Repository Guidelines

## 项目结构与模块组织

本仓库是一个 FastAPI + Jinja2 + Vanilla JavaScript 的中转站管理工具。核心后端在 `app/`：`main.py` 定义路由，`models.py` 和 `db.py` 管理 SQLite 数据模型与连接，`security.py` 处理登录和加密，`scheduler.py`、`analytics.py`、`proxy_support.py`、`openai_compat.py` 分别承载定时任务、统计、代理和 OpenAI 兼容逻辑。页面模板放在 `app/templates/`，前端脚本与样式放在 `app/static/`。测试在 `tests/`，部署辅助脚本在 `scripts/`。`data/` 保存运行数据库，属于本地状态。

## 构建、测试与开发命令

- `pip install -r requirements.txt`：安装运行和测试依赖。
- `uvicorn app.main:app --reload --host 127.0.0.1 --port 18000`：本地启动开发服务。
- `pytest`：运行完整测试套件。
- `pytest tests/test_scheduling_statistics.py`：只运行定时任务和统计相关测试。
- `docker compose build --pull && docker compose up -d`：构建并启动 Docker 服务。
- `.\scripts\synology-redeploy.ps1 -NasHost keys-nas -RemotePath /volume1/docker/Keys -UseSudo`：在群晖目标目录重建服务。

## 编码风格与命名约定

Python 使用 4 空格缩进、类型标注和 `snake_case` 命名，优先沿用现有函数与模块边界。模板保持 Jinja2 简洁表达，复杂逻辑放回后端。前端功能集中在 `app/static/app.js`，样式集中在 `app/static/styles.css`。避免无关重构、格式 churn 和跨模块大改；新增行为应贴近已有路由、表单和测试模式。

## 测试指南

测试框架为 pytest 和 pytest-asyncio，配置见 `pytest.ini`。测试文件命名为 `tests/test_*.py`，用例函数以 `test_` 开头。涉及路由、数据库迁移、统计聚合、调度、安全、代理或 OpenAI 请求兼容的改动必须补测试。修改前端脚本时，至少运行 `node --check app/static/app.js`；修改 Python 行为时运行相关 pytest，风险较高时跑完整 `pytest`。

## 提交与 Pull Request 指南

提交信息保持简短、说明行为，沿用现有风格，例如 `chore: persist statistics filters`、`feat: add scheduled tests and statistics dashboard`、`Fix home table alignment`。PR 应说明变更目的、主要实现、测试结果和部署影响；涉及 UI 的改动附截图或说明可见变化；涉及数据、配置或 Docker 的改动需明确兼容性和回滚注意点。

## 安全与配置提示

不要提交 `.env`、`data/keys.db`、API Key、SSH 密码或导出的敏感备份。本地配置参考 `.env.example`。同步或部署时保护运行数据库和环境文件；确认 Docker、群晖脚本或镜像源调整不会清除卷或覆盖生产数据。
