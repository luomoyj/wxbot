# 提交前检查清单

本清单用于每次本地 Git提交前确认变更范围、验证结果和运行状态。所有命令均从 wxbot项目根目录执行。

## 1. 确认变更范围

```powershell
git status --short
git diff --name-status
git diff --stat
```

- 每个文件都能对应到本轮任务或已经确认保留的历史修改。
- 不顺带提交无关重构、格式化或临时调试内容。
- 工作区存在多轮修改时，先列出本次准备提交的文件；不要使用 `git add .`扩大范围。

## 2. 检查敏感文件和运行数据

```powershell
git ls-files data tmp ".env*" "*.log"
git status --short --ignored
```

- 第一条命令应无输出；`data/`、`tmp/`、`.env*`和日志不得被 Git跟踪。
- 忽略项可以存在于本机，但不得通过强制添加进入提交。
- 审查待提交差异，确认不包含真实 `bot_token`、`context_token`、用户 ID、消息正文、二维码内容、密钥或密码。
- 发现疑似凭证时立即停止，不提交、不粘贴到对话或日志；先撤出暂存区并处理泄露范围。

## 3. 检查项目进度

打开 [ROADMAP.md](../ROADMAP.md)，确认：

- “当前状态”中的主任务、状态、验收条件、插入任务、恢复点、阻塞和正式下一步均与实际一致。
- 只有实现并完成要求验证的事项标记为“已完成”。
- 仍需真实微信、桌面端或其他外部确认的事项保持“待验收”。
- 插队任务没有覆盖原主任务或遗失恢复点。

## 4. 同步文档与版本

```powershell
.\.venv\Scripts\wxbot.exe --version
git diff --check
```

- 用户可见命令、配置或行为变化已经同步到 `README.md`或对应技术文档。
- 当前版本与 [CHANGELOG.md](../CHANGELOG.md)中的最新版本一致。
- 用户可见能力变化已写入变更记录；内部实现细节不必逐条罗列。
- `git diff --check`无空白错误。
- README和文档中的本地相对链接均能指向真实文件。

## 5. 运行验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

- 完整测试退出码为0，并以 `OK`结束。
- 记录实际测试数量和一次总耗时。
- 测试失败时不得提交为已完成；先修复根因或在 `ROADMAP.md`记录明确阻塞。
- 需要真实微信验收的改动，必须另外等待用户确认，自动化测试不能替代人工验收。

## 6. 检查后台运行状态

```powershell
.\.venv\Scripts\wxbot.exe status
```

- 预期运行时，应显示“自动回复运行正常”，并且只有一个有效 PID。
- 预期停止时，应明确显示“自动回复未运行”。
- 会话失效、轮询重试或健康状态异常时，先按 README的故障排查处理。
- 改动只有重启后才生效时，在交付说明中明确是否已经重启。

## 7. 精确暂存与提交

只暂存已经确认的路径：

```powershell
git add <file1> <file2>
git diff --cached --name-status
git diff --cached --stat
git diff --cached --check
```

- 再次检查暂存文件和统计是否符合本轮范围。
- 提交信息使用中文具体描述，前缀使用 `feat:`、`fix:`、`docs:`、`refactor:`或 `chore:`。
- 提交后运行 `git status --short`，确认剩余文件是刻意未提交的修改。
- 推送属于远端变更，只有用户明确要求时才执行；提交成功不等于已经推送。

## 8. 最终汇报

汇报只包含：

- 实际修改的文件及用户可见结果。
- 验证通过或失败、测试数量和一次总耗时。
- 当前版本与后台运行状态。
- 本地提交和远端推送状态。

不要粘贴底层命令输出、完整测试日志、消息正文或任何凭证。
