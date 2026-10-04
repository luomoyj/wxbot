# wxbot

在微信里使用 Codex：聊天、切换项目、查看进度，以及修改文件和运行任务。

**目前仅支持 Codex。** 自动回复和项目操作依赖本机 Codex App Server，使用前须安装并登录 Codex。可在 Codex 桌面端或 CLI中管理，也可通过 PowerShell启动后台服务，无需一直打开 Codex窗口。

## 环境要求

- Windows 10／11，以下命令在项目根目录的 PowerShell中执行。
- Python 3.11+。
- Node.js 16+（含 npm，用于安装 Codex CLI）。
- 已安装并登录的 Codex，以及可用的模型。

## 快速开始

尚未安装 Codex CLI时，按 [官方安装说明](https://developers.openai.com/codex/cli) 安装并登录：

```powershell
npm install --global @openai/codex
codex login
```

安装项目依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

首次使用无需配置模型，直接运行：

```powershell
.\.venv\Scripts\wxbot.exe setup
```

扫码确认后自动启动后台回复。首次发送有效文本的微信用户会成为唯一白名单，随后即可聊天或要求 Codex操作项目。

默认沿用 Codex设置，已有会话保留自身模型和推理等级。需要单独指定时，可参考 `model-config.example.json` 创建本机 `data/model_config.json`，设置 `model`（模型）和 `reasoning_effort`（推理等级），重启服务生效。

## 常用操作

在 Codex中打开本项目，可使用：

| Skill | 用途 |
| --- | --- |
| `$wxstart`／`$wxstop` | 启动／停止后台 |
| `$wxstatus`／`$wxrestart` | 查看状态／重启 |
| `$wxclear` | 清空当前会话上下文 |
| `$wxcompact` | 压缩当前会话上下文 |
| `$wxreset` | 预览全部 AI状态重置，确认后执行 |

也可直接要求 Codex“启动自动回复”或“查看运行状态”。PowerShell中使用对应命令：

```powershell
.\.venv\Scripts\wxbot.exe start
.\.venv\Scripts\wxbot.exe status
.\.venv\Scripts\wxbot.exe stop
.\.venv\Scripts\wxbot.exe restart
```

微信里发送 `/help` 查看命令，或直接说“修改 README并运行测试”等自然语言要求。

| 微信命令 | 用途 |
| --- | --- |
| `/projects` | 列出项目 |
| `/project 项目名` | 切换项目 |
| `/project add "D:\projects\example-app"` | 登记已有项目目录 |
| `/progress`／`/next` | 查看项目进度／下一步 |
| `/tasks`／`/stop` | 查看任务／取消任务 |
| `/sessions`／`/resume 2` | 列出／切换 Codex会话 |
| `/new`／`/reset` | 重新开始当前会话 |

微信端 `/stop` 只取消任务；停止后台使用 `$wxstop` 或 CLI的 `stop`。进度查询需要目标项目在本地维护 `ROADMAP.md`。

## 使用须知

- 电脑需保持开机、联网；微信会话失效时运行 `.\.venv\Scripts\wxbot.exe login` 重新扫码。
- 仅唯一白名单用户可自动回复和操作主机；可操作范围为当前 Windows账号有权访问的文件与命令。
- 支持文字、单张图片和 UTF-8纯文本附件；现有音频可作为文件附件发送。格式与大小限制见技术设计。
- 执行任务保存本地 checkpoint，恢复前须预览并确认；取消任务不会撤销已写入的文件。
- 登录、模型配置和会话数据保存在本机 `data/`，不要提交或公开该目录。

## 更多说明

[微信命令清单](docs/WECHAT_COMMAND_PROPOSAL.md) · [技术设计与能力边界](docs/TECHNICAL_DESIGN.md) · [参与贡献与测试](CONTRIBUTING.md) · [提交前检查清单](docs/PRE_COMMIT_CHECKLIST.md)

## 许可证

[MIT](LICENSE)
