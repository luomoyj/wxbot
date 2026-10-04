# wxbot

基于微信 iLink Bot 协议的本地消息收发客户端。

> [!WARNING]
> 本项目是非官方协议兼容实现，不是微信公众平台官方 API，也不隶属于腾讯或微信。协议可能随时变化；请仅在你有权控制的账号和设备上使用，并自行承担账号、数据和系统操作风险。

wxbot面向个人可信环境：它在本机保存微信会话和 Codex Thread，通过唯一白名单用户接收消息，并可让持久 Codex项目 Thread操作当前 Windows账号有权访问的文件和命令。开始使用前请先阅读 [安全政策](SECURITY.md) 和本文中的权限边界。

当前版本：

```powershell
.\.venv\Scripts\wxbot.exe --version
```

使用某项能力前，可先运行该命令并对照 [变更记录](CHANGELOG.md)，确认本机代码版本已经包含该能力。

## 全新电脑安装

以下步骤适用于 Windows 10／11，命令均在 PowerShell 中执行。

### 1. 安装基础环境

需要提前安装：

- Python 3.11 或更高版本。
- Git。
- Node.js 与 npm，用于安装 Codex CLI。
- 可使用 Codex 的 ChatGPT 账号或 OpenAI API Key。

安装后关闭并重新打开 PowerShell，确认命令可用：

```powershell
python --version
git --version
node --version
npm --version
```

### 2. 安装并登录 Codex CLI

按照 [OpenAI Codex CLI 官方文档](https://developers.openai.com/codex/cli) 安装：

```powershell
npm install --global @openai/codex
codex --version
codex login
```

`codex login` 会打开浏览器，默认可使用 ChatGPT 账号完成登录。登录后确认状态：

```powershell
codex login status
```

wxbot 启动的 Codex App Server会复用当前 Windows账号下的 Codex CLI登录状态，无需复制登录文件或在项目中配置 OpenAI密钥。

### 3. 克隆项目

克隆开源仓库：

```powershell
git clone https://github.com/luomoyj/wxbot.git
Set-Location wxbot
```

### 4. 创建项目虚拟环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

依赖仅安装到项目的 `.venv`，不会修改全局 Python包。

### 5. 完成首次设置

```powershell
.\.venv\Scripts\wxbot.exe setup
```

该命令会检查 Codex CLI；本机尚无微信登录时展示二维码，扫码确认后保存登录凭证并启动后台自动回复。凭证保存在被 Git忽略的 `data/session.json`，不要复制、提交或公开 `data/`中的文件。

### 6. 管理后台自动回复

```powershell
.\.venv\Scripts\wxbot.exe start
.\.venv\Scripts\wxbot.exe status
```

后台停止和重启命令：

```powershell
.\.venv\Scripts\wxbot.exe stop
.\.venv\Scripts\wxbot.exe restart
```

### 7. 完成首次验收

1. 从微信向机器人发送一条文本消息。
2. 首位发送有效文本的用户会自动成为唯一白名单。
3. 确认本机收到该消息，且微信只收到一条 Codex回复。
4. 在微信发送“当前会话信息”，确认模型、Thread和工作目录可以正常读取。

如需先检查本地代码，可运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 使用

首次使用：

```powershell
.\.venv\Scripts\wxbot.exe setup
```

微信会话失效后，`status`会提示在本机重新登录。此时执行 `login`并扫码成功，wxbot会自动恢复后台自动回复；白名单、Codex Thread、当前项目和任务状态不会被清空。

单独重新登录：

```powershell
.\.venv\Scripts\wxbot.exe login
```

前台运行基础消息收发，仅用于调试：

```powershell
.\.venv\Scripts\wxbot.exe run
```

App Server冷启动可能需要约 20 秒；后台启动和重启会等待最多 30 秒的健康信号，再报告成功或失败。

查看全部公开命令和分组命令帮助：

```powershell
.\.venv\Scripts\wxbot.exe --help
.\.venv\Scripts\wxbot.exe context --help
.\.venv\Scripts\wxbot.exe project --help
```

公开命令按使用目标分组：

| 命令 | 用途 |
| --- | --- |
| `setup` | 首次检查、扫码并启动 |
| `login` | 单独登录或重新扫码 |
| `start／stop／status／restart` | 管理后台自动回复 |
| `run` | 前台运行基础消息收发，供调试使用 |
| `context clear` | 清空当前 Codex Thread和降级历史 |
| `context compact` | 保留对话语义并压缩当前 Thread |
| `reset-ai-state` | 预览完整 AI状态重置范围；追加 `--confirm`才执行 |
| `project status [项目名]` | 读取当前或指定项目的正式进度 |
| `doctor` | 检查 Codex CLI和 App Server兼容性 |

在 Codex 桌面端打开本项目后，也可以输入：

- `$wxstart`：后台启动自动回复。
- `$wxstop`：停止后台自动回复。
- `$wxstatus`：查看运行状态。
- `$wxclear`：清空当前 Codex Thread和降级历史，保留其他会话、白名单和去重状态。
- `$wxcompact`：压缩当前活动会话的 Codex上下文，保留对话内容和本地状态。
- `$wxrestart`：停止并重新启动后台自动回复。
- `$wxreset`：预览完整 AI 状态重置范围；再次明确确认后才执行。

这些项目 Skill仅在本项目中生效，也可以直接用自然语言要求 Codex启动、停止或检查 wxbot。

## 常见故障排查

排查时先在项目根目录运行：

```powershell
.\.venv\Scripts\wxbot.exe status
```

不要直接删除 `data/`中的状态文件，也不要用 `taskkill`猜测并结束进程。

### Codex快捷命令没有显示

**现象**：在 Codex桌面端或 CLI中输入 `/`，没有看到 `wxstart`、`wxstatus`等命令。

**原因**：这些入口是项目级 Skill，不是 Codex内置的任意 `/命令`。

**处理**：

1. 确认 Codex当前打开的工作目录是 wxbot项目根目录。
2. 使用 `$wxstart`、`$wxstatus`或 `$wxrestart`调用对应 Skill；也可以通过 `/skills`查找项目 Skill。
3. 新增或修改 `.agents/skills/`后，重启一次 Codex桌面端或重新进入 CLI会话。
4. 如果仍未发现 Skill，直接运行等价的 `start`、`status`或 `restart`命令。

**验证**：执行 `$wxstatus`或 `wxbot status`，应返回“自动回复运行正常”或明确的未运行原因。

### 微信尚未登录

**现象**：启动时提示“尚未登录，请先运行 login”。

**处理**：

```powershell
.\.venv\Scripts\wxbot.exe login
.\.venv\Scripts\wxbot.exe start
```

**验证**：

```powershell
.\.venv\Scripts\wxbot.exe status
```

状态应为“自动回复运行正常”。单独执行 `login`只保存会话，因此普通首次登录后仍需执行一次 `start`；使用 `setup`则会自动完成这两步。

### 微信会话失效，出现 `-14`

**现象**：后台停止轮询，`status`提示微信会话失效或需要重新扫码。

**处理**：

```powershell
.\.venv\Scripts\wxbot.exe login
```

重新扫码成功后，wxbot会自动恢复此前因 `-14`停止的后台自动回复，不会清空白名单、Codex Thread、当前项目和任务状态。

**验证**：运行 `wxbot status`确认状态正常，再从微信发送一条文本，确认只收到一条回复。

### Codex CLI尚未登录

**现象**：后台启动失败并提示“Codex未登录”或要求运行 `codex login`。

**诊断与处理**：

```powershell
codex login status
codex login
.\.venv\Scripts\wxbot.exe restart
```

如果 `codex`命令本身不存在，先按“全新电脑安装”中的步骤安装 Codex CLI。wxbot复用当前 Windows账号的 Codex登录状态，不要把认证信息写入项目。

**验证**：`codex login status`应显示已登录，`wxbot status`应显示自动回复运行正常。

### wxbot进程已经存在

**现象**：重复启动没有产生新进程，或提示另一个 wxbot进程正在运行、进程存在但健康状态异常。

**处理**：

1. 先运行 `wxbot status`。如果状态正常，无需再次启动。
2. 如果健康状态异常，使用受控重启：

```powershell
.\.venv\Scripts\wxbot.exe restart
```

3. 如果重启提示停止失败，按顺序执行：

```powershell
.\.venv\Scripts\wxbot.exe stop
.\.venv\Scripts\wxbot.exe status
.\.venv\Scripts\wxbot.exe start
```

**验证**：最终 `wxbot status`应只显示一个正常运行的 PID；重复执行 `wxbot start`不会再创建第二个轮询进程。

首次发来有效文本消息的用户会成为唯一自动回复白名单。白名单保存在本机 `data/auto_reply.json`；其他用户只接收，不自动回复。自动回复通过一个长期运行的本地 Codex App Server完成：普通聊天和每个项目分别使用持久 Thread，明确执行请求由原 Thread使用主机级权限直接操作真实文件、项目和命令。失败时不会发送无依据的兜底内容，而会保存并返回脱敏后的失败阶段与原因。

配对白名单后，可从微信发送单张 JPEG、PNG或 WebP图片，也可紧接着发送说明文字。wxbot按 iLink媒体字段下载并解密图片；纯图片等待1.5秒后单独交给 Codex，同一用户和 Bot会话在等待窗口内发来的文字会与图片合并为一次 App Server `localImage`输入，只回复一次。Turn结束后立即清理本机临时文件。图片不参与首次白名单配对；多图、超过20 MiB或无法确认格式的图片当前不回复。该能力完成自动化验证后仍需按 [技术设计文档](docs/TECHNICAL_DESIGN.md) 的第20节执行一次真实微信验收。

唯一白名单用户也可以发送单个纯文本附件，当前支持 `.txt`、`.md`、`.log`、`.csv`、`.tsv`、`.json`、`.jsonl`、`.yaml`、`.yml`、`.toml`、`.ini`、`.diff`和 `.patch`，单文件不超过2 MiB且必须是严格 UTF-8文本。wxbot下载解密后把未改写的微信文字和带不可信数据边界的附件正文作为两个独立 App Server输入项，不增加读取确认；只有微信原文能够授权执行，附件内的命令或确认语句不会提供权限。当前附件 Turn使用只读沙箱，适合阅读、总结和分析；基于附件直接修改项目需等后续把附件生命周期接入主机任务 checkpoint。Turn进入终态后立即清理临时文件，终态入站记录不保留 CDN参数、AES key或正文。二进制、压缩包、Office、PDF、脚本、多个文件和混合媒体暂不支持。

唯一白名单用户当前微信原文明说“把 `notes.txt`发给我”等发送请求时，wxbot可直接发送当前项目内唯一确定的同范围普通文本文件，不再重复确认。没有写文件名时，只在最近任务 checkpoint中恰好存在一个允许的新增／修改文本文件时自动确定；否则要求明确文件名。发送前会重新核对项目范围、文件类型、2 MiB上限、UTF-8内容、敏感字段和 SHA-256摘要；文件变化、范围外路径、敏感内容、符号链接或多个候选均不会发送。CDN上传或 `sendmessage`结果不确定时不自动重试，避免重复文件。

现有音频文件也可以按普通文件附件发送，支持 `.mp3`、`.wav`、`.ogg`、`.m4a`和 `.silk`，单文件不超过20 MiB。必须在当前微信消息中明确写出当前项目内的文件名；wxbot不会从 checkpoint模糊选择音频。该能力只负责上传现有文件，不生成 TTS、不转码，也不会显示为微信原生语音气泡。

普通聊天和各项目的 Thread ID保存在本机 `data/thread_sessions.json`，wxbot或 App Server重启后会恢复原 Thread。成功对话仍分别保存在 `data/auto_reply.json`，采用最多20轮且约6000字符的混合上限，只在原 Thread明确不存在或损坏时用于降级恢复；最新一轮始终保留。可在微信中发送“清空上下文”“清空对话上下文”“忘掉之前的对话”“重新开始聊天”或“清除聊天记忆”清空当前活动会话，也可在 Codex 中使用 `wxclear` Skill。清理后保留其他项目上下文、白名单、消息去重、登录状态和当前项目。
需要保留内容但降低当前 Thread上下文体积时，可在 Codex中使用 `wxcompact`，或在微信中发送“压缩上下文”“压缩对话上下文”。该操作调用 App Server原生 Thread压缩，等待当前后台进程返回结果，不清空对话、不重建 Thread，也不影响其他项目历史、白名单、消息去重和降级历史；后台未运行时需先启动自动回复。

`data/auto_reply.json` 是被 Git 忽略的本机明文状态文件，保存唯一白名单用户 ID、最多1000条消息去重标识、当前项目和会话模式，以及普通聊天和各项目的降级历史；不保存 `bot_token` 或 `context_token`。历史按每个活动会话最多20轮且约6000字符裁剪，其他状态目前不按天自动过期。不要提交、上传或随意复制该文件。

只清理当前聊天记忆时，优先在微信中使用上述自然语言、在 Codex中使用 `wxclear`，或运行 `wxbot context clear`。完整重置时，在微信发送“清空全部AI状态”查看范围，再于5分钟内发送“确认清空全部AI状态”；也可运行 `wxbot reset-ai-state`预览，再明确运行 `wxbot reset-ai-state --confirm`。完整重置会清空白名单、消息去重、项目选择、全部降级历史和 Codex Thread映射，但保留微信登录、checkpoint、任务记录、模型配置、响应指标和项目文件。没有预览或确认超时均不会执行。完整字段和边界见 [技术设计文档](docs/TECHNICAL_DESIGN.md)。

微信端使用自然语言操作电脑，例如“有哪些项目”“切换到 wxbot”“修改 README并运行测试”“安装当前项目依赖”“提交代码”“删除 test.txt”“为什么刚才的任务失败”。常用项目列表仍用于快速切换，但不再构成访问边界；唯一白名单用户可以提供绝对路径，让 Codex访问当前 Windows账号有权访问的其他磁盘、目录、项目和进程。普通修改、项目级依赖、普通 Git提交、当前分支普通推送，以及少量明确点名普通文件的删除或移动会直接执行，不再生成二次确认任务；写任务仍自动保存 checkpoint。“提交”或“提交代码”默认包含推送当前分支，“仅本地提交”除外。批量或递归删除目录、清空数据、修改 `.env`或凭证、CI/CD、数据库迁移或批量数据删除、全局依赖、系统配置、生产部署、公开发布、删除远端资源和 Git改写历史仍需针对具体动作二次确认。

忘记用法时，在微信发送 `/help`，直接列出当前全部微信工作台指令和简短用途，不调用模型。旧中文帮助入口仍可用。

微信固定命令采用 Hermes风格命名，完整用法见 [微信命令清单](docs/WECHAT_COMMAND_PROPOSAL.md) 。常用入口为 `/projects`列项目、`/project wxbot`切项目、`/progress`查进度、`/next`查下一步、`/tasks`查队列、`/stop`取消任务、`/sessions`列会话、`/resume 2`绑定会话，以及 `/send "my notes.md"`发送允许文件。未知命令或错误参数直接返回用法，不交给模型猜测。

`/projects`合并 wxbot上级目录的一级 Git项目与手动登记项目。发送 `/project add "D:\projects\example-app"`可登记嵌套目录、其他盘符或非 Git项目；目录必须已经存在，登记不会修改项目文件或自动切换。以目录名作为项目名，随后发送 `/project example-app`切换；名称含空格时加引号。登记存于本机 `data/projects.json`，重启后保留；重复路径不会重复添加，同名不同路径会被拒绝。

`/new`和 `/reset`只重新开始当前会话；清空全部 AI状态须先发 `/reset-ai-state`查看范围，再发 `/reset-ai-state confirm`确认。`/rollback latest`预览恢复最近任务，`/rollback confirm`只恢复本次预览的目标并再次检查冲突。`/stop`不停止后台服务。开发、测试和提交仍使用自然语言，微信原文保持不变。

项目执行任务会在修改前后保存本机 checkpoint。可在微信中直接说“查看刚才的修改”“查看 README.md的修改”或“查看原始 diff”；“撤销刚才的修改”只显示恢复范围，再次回复“确认恢复刚才的修改”才会执行。恢复会逐文件比较任务前、任务后和当前内容：安全文件正常恢复，非重叠文本修改只撤销本任务内容，冲突文件单独跳过，不会覆盖后续修改。

微信中的“下一步”“当前进展”“项目当前任务”“还有什么没完成”和“有没有阻塞”由程序每次直接读取当前项目 `ROADMAP.md`，不使用 Codex Thread中的旧进度。也可在本机运行 `wxbot project status`查看同一份正式状态。

微信项目主 Thread会以“微信 项目名”持久化为 Codex用户任务；wxbot重启后继续使用同一 Thread，并可在相同本机账号的 Codex CLI和桌面端任务列表中查找。

切换到项目会话后，除即时控制指令外，咨询、闲聊和执行要求都会把未改写的微信原文直接写入该项目的长期 Thread，不再创建临时意图分类 Thread；权限、开发和汇报规则统一从 `AGENTS.md`与 Thread基础指令读取。

在微信中查询可用会话时，每项会分别显示会话标题和一行经过隐私过滤的最近用户提问；取不到最近对话时回退到会话摘要，没有正式名称或安全提问时使用明确占位文字，避免把内部恢复提示、系统提示或结构化 JSON当成会话名称。

Codex桌面端附带图片或文件的消息会先移除附件文件名、本机路径和图片标签，再展示真实问题；只有图片而没有文字时显示“发送了一张图片”。

微信中可直接说“有哪些会话”“搜索会话 wxbot”“查看第 2 个会话”“切换到第 2 个会话”或“当前会话信息”。列表和搜索只显示当前项目工作目录中的 Codex会话；切换成功后更新当前项目的长期 Thread映射，不删除旧 Thread，也不影响其他项目。为避免序号过期，wxbot重启后需要重新列出会话再切换。

自动回复模型由本机 `data/model_config.json`明确指定。首次配置可复制 `model-config.example.json`，只允许填写 `model`和`reasoning_effort`；修改后运行 `wxbot restart`生效，Codex中也可使用 `$wxrestart`。当前示例使用 `gpt-5.6-sol`和`low`。每次 Codex Turn只在本机 `data/turn_metrics.json`记录随机统计编号、请求类型、执行阶段、输入长度区间、模型、推理等级、结果类型、模型耗时、总耗时和时间戳，不记录消息正文、用户 ID或 Thread ID，最多保留最近100条。模型耗时包含该 Turn内部可能发生的工具执行；当前不记录无法可靠取得的工具调用次数和单个工具耗时。

微信轮询收到消息后立即打印并交给入站调度器；同一主机 Thread的 AI消息保持串行，任务查询和取消走即时控制通道，不会等待前一条 Codex调用完成。执行任务由持久队列和独立 Worker调度，但实际修改仍在原长期 Thread和真实文件系统中完成。微信中可直接询问“当前任务怎么样了”“为什么失败”“有哪些运行中的任务”，或发送“取消刚才的任务”。取消和服务重启不会回滚已经写入的真实文件；排队任务会继续执行，已中断任务不会自动重放，未成功发送的结果会在白名单用户下一条消息到达时补发。

主机级权限意味着微信白名单账号可以触达当前 Windows账号可访问的数据和程序。微信账号、电脑会话或模型上下文被劫持时可能造成真实文件或系统损失；请只在个人可信环境中运行，并定期检查 Git差异和本地备份。

入站文本、图片或纯文本附件协议消息会先保存到本机 `data/inbox.json`，再推进微信拉取游标。服务重启后，尚未开始的消息继续处理；已经开始但在进程退出时未能确认发送结果的消息不会自动重发，避免产生重复回复。活动任务会临时保存处理所需的用户 ID、`context_token`、正文或媒体下载字段；任务进入已完成或结果不确定状态后立即删除整个消息对象，只保留去重键、状态和时间。终态记录保留 7 天且最多 1000 条。该文件已被 Git忽略，不应复制、提交或公开。

切换项目后，所有非控制类自然语言都进入该项目的独立 Thread，不依赖“项目、进展、这个、继续”等关键词判断。切换到其他项目不会清空旧项目上下文，切回后可继续原话题。

运行中：

- 直接输入文字：回复最近收到消息的会话。
- `/reply 编号 内容`：回复指定会话。
- `/list`：列出本次运行中收到的会话。
- `/help`：显示命令帮助。
- `/quit`：退出。

登录凭证保存在本机 `data/session.json`，该目录已被 Git 忽略。不要复制、提交或公开该文件。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

该命令会自动发现并运行 `tests/` 目录下的全部测试。

运行全部自动化测试可验证微信消息收发、持久 Thread、主机级任务执行、checkpoint恢复及安全边界是否正常。

主机级直接执行验收

协议范围、风险和真实微信验收步骤见 [技术设计文档](docs/TECHNICAL_DESIGN.md)。登录、`getupdates`和`sendmessage`与腾讯当前参考实现的版本化逐字段对照见 [iLink协议核对记录](docs/ILINK_PROTOCOL_AUDIT.md)。

项目整体结构以及与 Hermes Agent、OpenClaw 的定位差异见 [Agent 架构对比](docs/AGENT_ARCHITECTURE_COMPARISON.md)。

准备提交代码前，按 [提交前检查清单](docs/PRE_COMMIT_CHECKLIST.md)逐项确认变更范围、敏感文件、验证、文档和运行状态。

项目当前进度和后续任务见 [项目路线图](ROADMAP.md)。

## 参与贡献

开发环境、测试要求、敏感数据边界和 Pull Request流程见 [贡献指南](CONTRIBUTING.md)。提交安全漏洞前请先阅读 [安全政策](SECURITY.md)，不要在公开 Issue中披露真实凭证或利用细节。

## 许可证

本项目采用 [MIT License](LICENSE)。许可证只覆盖本仓库中的代码和文档，不代表腾讯、微信或其他第三方对其商标、服务或协议授予任何权利。
