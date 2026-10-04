# 参与贡献

感谢你改进 wxbot。提交变更前，请先阅读 [README](README.md)、[技术设计](docs/TECHNICAL_DESIGN.md) 和 [项目路线图](ROADMAP.md)，确认变更符合当前协议边界和开发阶段。

## 开发环境

项目要求 Python 3.11 或更高版本。Windows PowerShell中的推荐初始化方式：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

运行全部自动化测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

其他平台可使用对应虚拟环境中的 Python执行相同的 `unittest`命令。

## 变更要求

- 先说明要解决的问题、影响范围和验证方式，避免夹带无关重构或格式化。
- 协议字段、请求流程或安全边界发生变化时，先同步更新 `docs/TECHNICAL_DESIGN.md`。
- 新增或修复协议行为时，优先增加纯函数测试或本地 mock server测试，不让自动化测试依赖真实微信服务。
- 真实登录、用户 ID、消息正文、二维码、`bot_token`、`context_token`和 Codex认证数据不得进入代码、测试、Issue、Pull Request或日志。
- 测试数据必须使用一眼可辨的虚构值，例如 `test-token`、`wxid_test_user`或 `https://example.invalid`。
- 不提交 `data/`、`tmp/`、`.venv/`、日志、构建产物或编辑器配置。

## 提交 Pull Request

1. 从最新代码创建独立分支。
2. 保持每个提交目标单一，提交信息清楚说明改了什么。
3. 运行全部自动化测试，并在 Pull Request中写明测试数量和结果。
4. 说明是否仍需维护者进行真实微信人工验收。
5. 涉及安全问题时不要创建公开 Issue或 Pull Request，请按 [安全政策](SECURITY.md) 私密报告。

提交贡献即表示你同意按本项目的 [MIT License](LICENSE) 发布该贡献。
