# wxbot 技术设计文档

当前登录、`getupdates`和`sendmessage`字段已经按腾讯 `openclaw-weixin` 2.4.6、提交 `cef0bfc390393f716903e16d50408118047f87e0` 于2026-07-20逐项核对；来源、差异和处理边界见 [iLink协议逐字段核对记录](ILINK_PROTOCOL_AUDIT.md)。

状态：文本、图片输入、连续图片与文字单次回复和主机 Thread能力均已实现并通过真实微信验收
日期：2026-07-24

## 1. 结论

项目采用 **Python 3.11+** 实现本地常驻 CLI：用户扫码登录 iLink Bot，程序通过 `getupdates` 长轮询接收文本消息，并通过 `sendmessage` 回复文本。第二阶段使用本地 Codex App Server自动回复；当前下一阶段只增加入站图片作为 Codex输入，不做图片发送、Web管理台或生产部署。

这样能用最短路径验证项目真正需要的能力：微信端能向 Bot 发消息，项目能收到；项目能针对该会话回复，微信端能收到。

## 2. 能力边界

### 首版包含

- 获取登录二维码并轮询扫码状态。
- 保存登录返回的 `bot_token`、`ilink_bot_id`、`baseurl`。
- 使用 `getupdates` 长轮询接收文本消息。
- 持久化 `get_updates_buf`，避免重启后重复拉取。
- 缓存入站消息的 `context_token`，对原会话发送文本回复。
- 对重复消息做有界去重。
- 识别会话失效并停止轮询，提示重新登录。
- CLI 输出最少必要状态，敏感字段脱敏。

### 首版不包含

- 操控任意个人微信账号、读取微信历史消息或通讯录。
- 群发、主动给陌生会话发消息。
- 图片发送、语音、视频和文件的上传下载；入站图片在第20节单独设计。
- 插件系统、Web UI、数据库和云部署。
- 绕过微信风控、登录限制或协议限制。

### 关键事实

iLink 当前公开实现连接的是微信 iLink Bot 身份。回复不是只靠用户 ID：每条回复还必须带最近入站消息中的 `context_token`。因此首版定位为“收到消息后回复”，不承诺脱离上下文主动发信。

## 3. 资料依据与可信度

设计优先对照腾讯仓库 `Tencent/openclaw-weixin` 的实现和协议说明，再用独立客户端交叉验证：

- [腾讯 openclaw-weixin](https://github.com/Tencent/openclaw-weixin)：作为主要参考，公开了请求头、核心接口及消息模型。
- [wechat-ilink-client](https://github.com/photon-hq/wechat-ilink-client)：用于交叉核对登录、长轮询、上下文令牌和媒体加密流程。
- [weixin-bot protocol-spec](https://github.com/epiral/weixin-bot/blob/main/docs/protocol-spec.md)：用于补充字段级说明和错误处理建议，不作为高于腾讯源码的权威来源。

风险说明：iLink Bot API 并非传统意义上的稳定公开平台 API；字段、风控和可用性可能变化。实现必须集中封装协议层，并保留原始错误码，不能把猜测写成兼容逻辑。

## 4. 技术选型

| 项目 | 选择 | 理由 |
|---|---|---|
| Runtime | Python 3.11+ | 适合本地自动化，并便于后续接入 AI、知识库和语音处理 |
| Language | Python + 类型标注 | 使用 `dataclass` / `TypedDict` 固定协议结构，避免动态字典到处传递 |
| HTTP | `httpx` | 明确支持连接、读取和长轮询超时控制，便于使用 mock transport 测试 |
| CLI | Python 标准输入输出 | 最少实现即可完成扫码与文本收发验证 |
| Storage | 本地 JSON 原子替换 | 状态量小，无需数据库 |
| Test | Python 内置 `unittest` | 避免为首版引入测试框架 |

文本首版运行依赖只引入 `httpx` 和二维码渲染库 `qrcode`。项目不直接依赖第三方 iLink SDK：当前协议面仍由本项目逐字段对照腾讯 TypeScript源码实现，避免把凭证持久化和会话策略交给未经审计的依赖。图片阶段已经按确认新增项目依赖 `cryptography`，仅用于 AES-128-ECB解密和 PKCS#7校验。

## 5. 建议目录

```text
wxbot/
├─ AGENTS.md
├─ pyproject.toml
├─ docs/
│  └─ TECHNICAL_DESIGN.md
├─ src/
│  └─ wxbot/
│     ├─ __init__.py
│     ├─ __main__.py
│     ├─ api/
│     │  ├─ client.py
│     │  └─ models.py
│     ├─ auth/
│     │  └─ qr_login.py
│     ├─ message/
│     │  ├─ poller.py
│     │  ├─ parser.py
│     │  └─ sender.py
│     ├─ storage/
│     │  └─ session_store.py
│     └─ cli.py
├─ tests/
├─ data/              # gitignored
└─ tmp/               # gitignored
```

`data/` 保存有效会话与游标；`tmp/` 只保存可再生成的二维码图片。二维码确认或过期后立即删除对应临时文件。

## 6. 协议设计

### 6.1 登录

1. `POST {defaultBaseUrl}/ilink/bot/get_bot_qrcode?bot_type=3`，body 为 `{"local_token_list": []}`，获取二维码标识和二维码内容。
2. CLI 把二维码 URL 渲染到终端；若终端不可用，再生成 `tmp/login-qr.png`。
3. `GET {defaultBaseUrl}/ilink/bot/get_qrcode_status?qrcode=...` 轮询状态。
4. `wait` / `scaned` 继续等待；`scaned_but_redirect` 切换到服务端返回的 `redirect_host`；`need_verifycode` 时在终端读取手机显示的配对码；`expired` / `verify_code_blocked` 停止并提示重新执行登录；`confirmed` 保存完整凭证。
5. 后续请求使用确认响应返回的 `baseurl`，不能假定永远是默认域名；二维码登录总有效期限制为 5 分钟。

登录确认后至少保存：

```python
@dataclass(frozen=True)
class SessionState:
    bot_token: str
    bot_id: str
    base_url: str
    get_updates_buf: str
    saved_at: str
```

不把 token 放进 `.env`。它是运行时生成的会话凭证，写入被 Git 忽略的 `data/session.json`；日志只显示 token 是否存在，不显示值。

### 6.2 公共业务请求

业务接口使用 JSON POST，并统一生成：

```text
Content-Type: application/json
AuthorizationType: ilink_bot_token
Authorization: Bearer <bot_token>
X-WECHAT-UIN: <base64(decimal(random uint32))>
iLink-App-Id: bot
iLink-App-ClientVersion: 256
```

每个 body 添加：

```json
{
  "base_info": {
    "channel_version": "0.1.0",
    "bot_agent": "wxbot/0.1.0"
  }
}
```

`X-WECHAT-UIN` 每次请求重新生成。`httpx` 会依据 UTF-8 body 设置正确的 `Content-Length`，应用层不手写该值。

### 6.3 接收消息

请求：

```json
{
  "get_updates_buf": "<last cursor or empty string>",
  "base_info": {
    "channel_version": "0.1.0"
  }
}
```

处理顺序：

1. 发起长轮询，请求超时设为服务端 `longpolling_timeout_ms` 加安全余量。
2. 校验 HTTP 状态、JSON 结构和 `ret` / `errcode`。
3. 只处理 `message_type = 1` 的用户消息。
4. 遍历 `item_list`，首版只把 `type = 1` 的 `text_item.text` 交给 CLI。
5. 以稳定消息标识去重；若服务端没有单一消息 ID，则使用关键字段摘要并限定 5 分钟窗口。
6. 保存用户 ID 与其最新 `context_token`。
7. 本批响应处理完成后，原子写入新的 `get_updates_buf`。

游标推进采用“批次处理完成后提交”。这仍不是严格 exactly-once：进程可能在已回复但未落盘时崩溃，因此发送端还要用稳定 `client_id` 和短期去重降低重复回复风险。

### 6.4 发送文本

首版发送只允许两种来源：

- CLI 针对刚收到的会话执行回复。
- 测试代码向 mock server 发送。

请求体：

```json
{
  "msg": {
    "from_user_id": "",
    "to_user_id": "<inbound from_user_id>",
    "client_id": "wxbot:<stable message id>",
    "message_type": 2,
    "message_state": 2,
    "context_token": "<inbound context_token>",
    "item_list": [
      {
        "type": 1,
        "text_item": {
          "text": "<reply>"
        }
      }
    ]
  },
  "base_info": {
    "channel_version": "0.1.0"
  }
}
```

没有可用 `context_token` 时拒绝发送并给出明确提示，不能用空值碰运气。

## 7. 运行流程

```mermaid
sequenceDiagram
    participant O as Operator
    participant B as wxbot CLI
    participant W as WeChat iLink
    participant U as WeChat User
    O->>B: wxbot login
    B->>W: get_bot_qrcode
    B-->>O: Display QR code
    O->>W: Scan and confirm
    B->>W: get_qrcode_status
    W-->>B: token, bot id, baseurl
    B->>B: Persist local session
    loop Long polling
        B->>W: getupdates(cursor)
        U->>W: Send text
        W-->>B: message, context_token, new cursor
        B-->>O: Show sender and text
        O->>B: Reply text
        B->>W: sendmessage(context_token)
        W-->>U: Deliver reply
        B->>B: Persist new cursor
    end
```

## 8. 状态、并发与恢复

- 单账号只允许一个轮询进程；启动时创建本地锁，避免两个进程消费同一游标。
- `session.json` 通过“写临时文件 → 原子重命名”更新，防止中断留下半截 JSON。
- 长轮询等待超时视为一次无消息的成功轮询，保持原游标并立即进入下一轮，不记为协议错误。
- 连接中断、HTTP错误和无效 JSON统一转成不包含响应正文的 `ILinkError`；除会话失效 `-14`外，轮询器按 `2..30`秒指数退避后继续使用原游标请求。
- 入站文本在交给内存 Worker 前必须先写入本机 `data/inbox.json`；写入成功后 Poller 才允许持久化服务端返回的新游标，避免“游标已保存但内存任务尚未执行”导致消息永久丢失。
- 入站任务使用 `queued`、`processing`、`completed` 和 `uncertain` 四种状态。重启时继续执行 `queued`；上次进程中断时已进入 `processing` 的任务转为 `uncertain`，因为无法证明微信回复是否已经发送，禁止自动重发。
- `queued` 和 `processing` 为恢复处理临时保存完整消息；进入 `completed` 或 `uncertain` 后必须立即删除消息对象，只保留去重键、状态、创建时间和更新时间。
- `completed` 与 `uncertain` 终态记录统一保留 7 天且合计不超过 1000 条。读取旧格式时自动完成脱敏迁移；状态查询只能展示各状态数量，不得返回记录键或消息内容。
- `ret = -14` 或 `errcode = -14`：停止当前会话，保留非敏感诊断信息，要求重新扫码。
- 收到无法识别的消息类型：记录类型号，不输出原始敏感 body，不中断轮询。
- 后台进程启动后先写入 `starting`，完成会话、进程锁和 Codex CLI 预检后才写入 `running`；控制端等待该健康信号后才报告启动成功。
- Windows使用 NVM时，全局 npm工具按 Node版本隔离。Codex CLI定位顺序为：当前 PATH中的 `codex.cmd`、`NVM_HOME`下各 Node版本中的 `codex.cmd`、当前 PATH中的其他 `codex`可执行文件。NVM候选按版本从新到旧选择，使 wxbot切换到未安装 Codex CLI的 Node版本后仍能复用本机已有 CLI，并避免误用无权由后台进程启动的 Codex桌面端内置程序。
- `data/wxbot.health.json` 只保存运行标识、状态、启动时间、最近成功轮询时间、脱敏错误类别、连续失败次数和下次重试等待秒数；不保存服务端原始错误文本、用户 ID、消息正文或凭证。
- 临时轮询失败不终止进程，按指数退避重试；终端应显示脱敏原因、连续失败次数和重试时间。下一次轮询成功后清空错误状态和失败计数。
- `wxbot status` 必须区分“正常运行”“临时轮询异常，正在重试”“微信会话失效，需要重新扫码”和“健康信号过期”，不能仅根据 PID 判断运行状态。
- `/wxstatus` 同时核对 PID、Windows 进程创建时间和健康状态；PID 被系统复用时视为原进程已停止，不操作新进程。
- `/wxstop` 先通过 `data/wxbot.control.json` 请求优雅停止；超时后才强制结束已通过身份校验的进程树。
- `context clear`和`context compact`复用该控制通道，并分别通过 `data/wxbot.context-clear.json`和`data/wxbot.compaction.json`返回请求状态与脱敏结果；这些文件不保存对话正文、用户 ID、Thread ID或凭证。
- Windows 强制停止命令返回失败后必须再次核对原 PID和创建时间；若目标进程已在检查与 `taskkill` 之间自行退出，应按停止成功处理，只有同一进程仍存活时才报告停止失败。
- Windows 后台模式由隐藏的 `cmd.exe` 持有虚拟环境 Python 进程树，避免 Python 启动器脱离调用终端后被宿主提前回收；PID 身份和停止操作针对该稳定父进程。
- 自动回复状态更新使用独立文件锁，将读取、修改和原子替换放在同一临界区；损坏或字段异常时拒绝覆盖原文件。
- `data/inbox.json` 的活动记录可能包含用户 ID、`context_token` 和消息正文，只允许保存在本机并由 Git忽略；不得写入普通日志或测试夹具，自动化测试只能使用明显虚构的数据。终态记录不得继续保存这些字段。

## 9. 安全与隐私

- `.gitignore` 必须覆盖 `data/`、`tmp/`、日志和构建产物。
- 默认日志不记录正文；交互界面显示正文是产品功能，但不写磁盘。
- 测试样本只能使用虚构用户 ID、token、正文和 `context_token`。
- 错误对象进入日志前统一脱敏 `Authorization`、token、二维码、用户 ID 和上下文令牌。
- 真实扫码和真实发消息属于人工验收，不纳入自动测试。
- 不提供群发、通讯录遍历、历史消息抓取或风控规避能力。

## 10. 测试与验收

### 自动测试

1. `X-WECHAT-UIN` 解码后必须是 `0..4294967295` 的十进制整数。
2. 每次请求都有完整公共 headers 和 `base_info`。
3. 登录状态机正确处理 `wait`、`scaned`、`scaned_but_redirect`、`need_verifycode`、`confirmed`、`expired` 和 `verify_code_blocked`。
4. `getupdates` 首次游标为空，成功批次后才推进新游标。
5. 重复入站消息不会在去重窗口内重复触发回复。
6. `sendmessage` 的 `to_user_id` 和 `context_token` 来自同一入站会话。
7. 无上下文令牌时发送失败且不请求服务端。
8. `-14` 会停止轮询并进入“需要重新登录”状态。
9. `-14` 状态必须在后台进程退出后继续保存在 `data/wxbot.health.json`，使 `wxbot status` 能明确提示本机重新扫码，而不是退化为“未运行”。
10. 用户在本机重新执行 `wxbot login` 时展示二维码；若登录前状态为 `-14` 会话失效，登录成功后自动重新启动后台自动回复。普通首次登录只保存会话，不自动启动，首次使用可运行 `wxbot setup`连续完成登录和后台启动。
11. 重新登录只替换 `data/session.json` 中的 iLink 登录状态；不得清空白名单、消息去重、Codex Thread映射、当前项目或长任务状态。
9. 日志脱敏测试确保敏感值不出现在输出中。
10. 长轮询超时保持原游标；连接中断、HTTP 5xx和无效 JSON后使用原游标恢复。
11. 消息处理失败时不推进游标且不永久命中内存去重，恢复后同一消息能够再次处理。

### 真实微信验收

只进行一次完整人工链路：

1. 启动 CLI，展示二维码。
2. 手机微信扫码并确认，CLI 显示登录成功但不输出 token。
3. 微信端向 Bot 发送唯一文本，例如 `wxbot-e2e-20260713`。
4. CLI 在一次长轮询周期内显示该消息。
5. CLI 回复 `received: wxbot-e2e-20260713`。
6. 微信端收到回复。
7. 重启 CLI，不重复处理已提交游标对应的旧消息。

成功标准：以上 7 步全部通过；任何一步失败，保存脱敏响应并回到官方源码逐字段对比，不通过反复扫码和猜测排查。

## 11. 开发计划（确认后执行）

1. 建立 Python 最小工程、虚拟环境约定和忽略规则  
   验证：类型检查与空测试通过，敏感目录未被 Git 跟踪。
2. 实现协议类型、公共 HTTP 客户端和本地 mock server  
   验证：headers、body、错误码与 UIN 测试通过。
3. 实现二维码登录和会话原子持久化  
   验证：四种二维码状态与脱敏测试通过。
4. 实现长轮询、游标、去重和会话上下文缓存  
   验证：重放、断线恢复、`-14` 测试通过。
5. 实现 CLI 文本回复  
   验证：mock 环境端到端收发通过。
6. 请求主人执行一次真实扫码验收  
   验证：完成第 10 节真实微信 7 步链路；不部署、不发布。

## 12. 后续阶段

首版真实链路稳定后，再单独评审媒体能力。媒体需要 `getuploadurl`、AES-128-ECB、CDN 上传与不同媒体类型的密钥编码，复杂度和隐私风险明显高于文本收发，不应提前混入首版。

## 13. Codex App Server自动回复

不公开的内部 `_worker`入口启动微信长轮询和一个本地 `codex app-server --listen stdio://` 子进程。App Server复用当前 Windows用户的 Codex登录状态，不监听网络端口。

后台控制器和底层 App Server JSON-RPC请求统一允许冷启动最多等待 30 秒；只有完成初始化并写入轮询健康状态后才报告启动成功，避免外层仍在等待而内层 `thread/start` 已提前超时。

- App Server适配器按“扩展字段宽容、关键字段严格”处理 Codex升级：无请求 ID的未知通知直接忽略，成功响应中的未知扩展字段继续兼容。
- `thread/start`、`thread/resume`和`thread/fork`必须返回非空 `thread.id`，`turn/start`必须返回非空 `turn.id`。
- 手动压缩当前活动会话使用 `thread/compact/start`，请求只携带当前 `threadId`；命令等待 `thread/compacted` 通知或等价的上下文压缩完成项后才报告成功。压缩失败、超时或 App Server退出时不得修改本地对话历史和 Thread映射。
- 压缩完成后，当前 Thread记录“等待压缩后新模型 Turn”状态。该状态下若 `thread/tokenUsage/updated.last.inputTokens` 为零或尚未返回，用户界面显示“压缩后尚未进行新的模型对话”，不显示 `0 Token`；收到后续真实模型 Turn的非零输入用量后清除该状态并恢复数字。这是展示状态，不伪造 App Server未提供的精确上下文占用。
- `thread/list`必须返回数组 `data`，且每条记录必须包含非空字符串 `id`；`thread/read`必须返回带非空 `id`的 `thread`。
- JSON-RPC成功响应的 `result`不是对象，或以上关键字段缺失、类型变化时，统一抛出包含请求方法和字段路径的 Schema兼容错误，便于 Codex升级后快速定位；错误信息不得包含完整响应、用户正文或凭证。
- `wxbot doctor`使用一个固定命名的持久兼容检查 Thread验证 `thread/resume`和`thread/read`，并用临时 Thread验证 `thread/start`、`turn/start`和 `thread/compact/start`；重复执行不会持续创建新的可见兼容检查 Thread。
- 首次收到有效文本消息时，把发送者设为唯一自动回复白名单；之后只对该用户自动回复。
- 普通聊天和每个项目分别使用独立、持久的主机 Thread，切换项目不清空旧项目上下文。
- 普通聊天和项目主 Thread ID写入本机 `data/thread_sessions.json`。App Server重启后优先通过 `thread/resume`恢复原 rollout；只有原 Thread明确不存在或损坏时，才新建 Thread并根据 `data/auto_reply.json` 注入降级历史。
- 降级历史采用“最多20轮＋约6000字符”的混合上限：短对话可以保留超过10轮，长对话按连续的最近轮次截断，并始终保留最新一轮。持久 Thread仍是完整上下文来源；当前没有真实证据证明需要会话摘要，因此不引入摘要和语义重要性判断。
- 模型失败、超时、进程退出、空回复或发送失败时不追加历史，也不发送兜底消息。
- 自动回复使用由入站消息稳定标识派生的 `client_id`，并保存最近处理标识，降低重复发送风险。
- 微信自然语言清理只清空当前活动会话并重建其 Thread，保留其他项目、白名单、去重记录和登录状态。
- 微信发送“压缩上下文”或“压缩对话上下文”时，只压缩当前活动的持久 Thread；不清空对话、不重建 Thread、不影响其他项目历史、白名单、去重记录或降级历史。
- 微信正文、用户 ID、token 和 `context_token` 不写普通日志。

### `data/auto_reply.json` 本地状态与清理

`data/auto_reply.json` 是被 Git 忽略的本机明文状态文件，不是运行日志，也不包含 `bot_token` 或 `context_token`。它保存唯一白名单用户 ID、最近处理过的消息去重标识、当前项目和会话模式，以及普通聊天和各项目的降级对话历史。对话历史包含用户消息和模型回复，因此仍应按敏感本地数据保护，不应提交、上传或随意复制。

该文件当前没有按天自动过期机制，各类数据按以下规则保留：

- 当前活动会话的历史最多保留20轮，并按约6000字符的总量继续裁剪；最新一轮始终保留。
- 消息去重标识最多保留最近1000条。
- 白名单用户、当前项目、会话模式以及未被继续写入的其他项目历史会持续保留，直到用户明确清理。

微信自然语言清理或 Codex中的 `wxclear`只清空当前活动会话的降级历史，并重建该会话的 Codex Thread；它保留其他项目历史、白名单、消息去重、当前项目和微信登录状态。需要只清理聊天记忆时应优先使用该方式。

需要完整重置 AI 状态时，使用 `wxbot reset-ai-state`预览范围，再明确执行 `wxbot reset-ai-state --confirm`。确认后清空白名单、去重标识、当前项目、会话模式、全部降级历史和 `data/thread_sessions.json`中的 Codex Thread映射；下一位发来有效文本消息的用户会重新成为唯一白名单。CLI在自动回复运行时先停止进程，重置完成后再恢复运行；任一步失败都返回非零状态。该命令保留 `data/session.json`中的微信登录凭证、checkpoint、任务记录、模型配置、响应指标和项目文件。

微信中发送“清空全部AI状态”或“重置全部AI状态”只返回同一份范围预览，并建立5分钟有效的确认状态，不立即修改数据。只有同一白名单用户随后发送“确认清空全部AI状态”或“确认重置全部AI状态”才在当前进程中清空持久状态和 Thread缓存；没有预览、确认超时或用户不一致时拒绝执行。该流程独立于 `wxclear`：`wxclear`只清理当前活动会话，完整重置则清理全部 AI会话状态。

普通聊天和主机操作不再使用逐消息 `codex exec`。唯一白名单用户的明确执行请求由当前长期 Thread使用 `danger-full-access`直接修改真实文件、运行命令并读取工具错误；不再 fork临时工作区、生成 unified diff或等待整份补丁审批。

项目请求进入后台 Worker后，立即显示当前项目名称和“正在处理，完成后会自动回复。”，让微信端用户确认当前项目，同时不展示本机绝对路径。同一个长期项目 Thread根据微信原文和项目规则决定只回答、检查或执行；请求涉及执行时真实工作区可能已经发生变化，状态查询必须如实区分等待、执行、完成、失败和取消。

后台任务状态保存在 `data/tasks.json`。App Server回复超时、进程退出或通信中断统一记为失败，wxbot重启时仍在运行的任务记为重启中断，用户取消记为取消；这些状态的自然语言查询使用固定脱敏文案，并统一提示失败或取消前真实文件可能已有部分修改。完成通知发送失败不改变任务执行结果，而是保留 `notification_pending`，由后续微信消息重试发送；“当前任务怎么样了”和“为什么失败”会优先返回仍待通知、失败或取消的最近任务。

执行任务的 `600`秒限制按“持续无 App Server进展”的空闲时间计算，不再按整个 Turn墙钟时间计算。`item/started`、`item/completed`、`turn/diff/updated`、`turn/plan/updated`及其他同时携带当前 `threadId`与 `turnId`的有效通知均刷新活动时间；连续600秒没有任何进展才请求 `turn/interrupt`并标记超时。用户主动取消、App Server退出和通信异常仍立即中断，进展续期不得掩盖真正无响应。

任务状态查询由确定性控制层覆盖“当前任务怎么样了”“还没做完吗”“做完了吗”“怎么还没好”“为什么刚才的任务失败”等常见自然表达，不进入模型。任务运行时立即返回当前阶段；任务结束后返回最近结果或回退到 `ROADMAP.md`正式状态。

自动回复进程写入终端的启动、入站消息、处理结果、轮询错误和其他运行提示统一使用本机时区的 `[HH:mm:ss]` 前缀。时间只用于定位运行事件，不得同时输出消息正文、用户 ID、`context_token`或凭证。

## 14. 本地 CLI 与 Codex 桌面端快捷命令

面向开源用户的正式 CLI 使用已安装的 `wxbot`入口，不要求用户记忆虚拟环境中的 Python绝对路径。公开命令按用户目标命名：

- `wxbot setup`：首次使用入口；检查 Codex CLI，缺少微信登录时启动扫码，随后启动后台自动回复。
- `wxbot login`：单独登录或在微信会话失效后重新扫码。
- `wxbot start`、`stop`、`status`、`restart`：管理后台自动回复；`start`已经运行时不重复启动。
- `wxbot run`：在当前终端前台运行基础消息收发，供调试使用。
- `wxbot context clear`：通过正在运行的后台进程清空当前活动会话的降级历史和持久 Codex Thread映射，使下一轮创建新 Thread；不得只清本地历史后继续复用旧 Thread。
- `wxbot context compact`：通过后台进程压缩当前活动的持久 Codex Thread，保留对话内容和本地状态。
- `wxbot reset-ai-state`：默认只预览完整 AI状态重置范围，只有显式追加 `--confirm`才执行。
- `wxbot project status [项目名]`：读取当前或指定项目的正式 `ROADMAP.md`状态。
- `wxbot doctor`：执行 Codex CLI与 App Server Schema兼容检查。

后台子进程使用不出现在公开帮助中的内部 `_worker`入口。旧的 `daemon-*`、`auto-start`、`clear-history`、`compact-context`、`project-status`和`schema-check`不再作为公开命令；项目尚处于 `0.x`且公开发布前直接迁移，避免长期保留两套名称。`wxbot --help`必须列出每个公开命令及用途，分组命令必须支持 `wxbot context --help`、`wxbot project --help`和叶子命令帮助。

项目在 `.agents/skills/` 提供七个项目级 Skill：

- `/wxstart`：在隐藏后台进程中启动自动回复；已经运行时不重复启动。
- `/wxstop`：停止由 `/wxstart` 启动的后台进程。
- `/wxstatus`：查询后台自动回复是否运行。名称不使用 `/status`，避免与 Codex 内置命令冲突。
- `/wxclear`：清空当前活动会话的降级历史和持久 Codex Thread映射，但保留其他会话、唯一白名单和消息去重记录。
- `/wxcompact`：请求后台自动回复进程压缩当前活动的持久 Codex Thread；压缩完成前等待结果，失败时返回脱敏原因。
- `/wxrestart`：依次停止并重新启动后台自动回复，返回新进程 PID。
- `/wxreset`：先预览完整 AI状态重置范围；只有再次明确确认后才执行受控重置。

Skill 只负责调用确定的本地 CLI 命令，不读取或输出凭证、用户 ID 与消息正文。后台进程的 PID 写入被 Git 忽略的 `data/wxbot.pid.json`；标准输出和错误输出均不落盘。PID 文件只用于管理由 `/wxstart` 启动的进程，过期记录由下次启动覆盖。

Skill 底层分别调用 `wxbot start`、`stop`、`status`、`restart`、`context clear`、`context compact`和`reset-ai-state`。上下文清理和压缩都通过后台进程已有的 App Server会话管理器执行，避免 CLI进程与常驻进程对同一 Thread产生不同判断；后台未运行时返回明确提示，不自行创建第二个轮询进程。后台启动前必须确认本地会话存在；单进程锁继续作为防止重复轮询的最终约束。

## 15. 微信项目工作台

项目列表继续提供 `wxbot` 父目录中的常用 Git项目作为快捷入口，但它不再构成权限边界。长期主机 Thread可以接受绝对路径并访问当前 Windows账号有权访问的其他磁盘、目录、项目和进程。

微信端使用自然语言，不要求输入斜杠命令：

- “帮助”“帮助指令”“指令帮助”“有哪些指令”或“列出所有指令”：由确定性控制层直接返回当前微信工作台的完整指令列表和简短用途，不调用模型。
- “有哪些项目”：列出可用项目。
- “切换到 wxbot 项目”：切换当前项目。
- “修改 README并运行测试”：在真实项目中直接执行并返回结果。
- “为什么刚才的任务失败”：从任务控制层读取脱敏失败原因。
- “取消刚才的任务”：中断当前 Turn，但不回滚已经写入的真实文件。

帮助文本由代码中的单一固定模板维护，覆盖项目、进度、任务、会话、上下文、运行信息、checkpoint、文件发送和普通执行示例。帮助入口属于即时控制，在项目任务正在分析或执行时也能直接响应；新增或删除微信端确定性控制能力时必须同步更新该模板、README和回归测试。

普通问题和执行请求都进入当前项目的持久 App Server Thread，不依赖关键词判断。Thread工作目录指向真实项目并使用主机级权限；切换项目只改变活动 Session，旧项目 Thread与本地历史继续保留，切回后继续原对话。文件现状优先于旧对话。

当保存的 Thread不存在、损坏或尚未创建时，新的可见主 Thread仍必须把本轮微信原文作为唯一用户消息。用于恢复指代的本地降级历史只能附加到该 Thread不可见的 `baseInstructions`，并明确标记为不可信数据：它只能辅助理解指代，不得提供执行授权、扩大任务范围或覆盖工作目录中的最新文件和规则。禁止再把历史 JSON与“本轮用户消息”包装后写入可见用户 Turn。

普通修改请求由长期主机 Thread直接在真实工作区执行，不生成审批码，也不使用 `git apply`。历史 `data/approvals/`仅作旧版本兼容，不再创建新记录或参与当前执行链。

个人工作台的唯一白名单用户在当前微信原文中明确要求安装或更新项目级依赖、创建普通 Git提交或推送当前分支时，任务直接进入 Worker，不再建立 `waiting_approval`。项目级依赖不得扩大为全局安装；普通提交只包含本次任务相关改动；“提交”或“提交代码”默认包含当前分支普通推送，“仅本地提交”除外。历史 `waiting_approval`字段和确认控制入口只保留兼容读取，不再为新任务生成。

少量明确点名的普通文件删除或移动也直接进入 Worker，由任务前 checkpoint提供自动保护；范围不明确时先澄清。批量或递归删除目录、清空数据、修改 `.env`或凭证、CI/CD、数据库迁移或批量数据删除、全局依赖、系统配置、生产部署、公开发布、删除远端资源，以及 force push、rebase、改写历史、覆盖工作区的 reset／restore／checkout仍属于二次确认红线。该确认只针对具体高风险动作，不恢复整份补丁审批。

## 16. Codex App Server与分项目 Session架构

项目当前使用一个长期运行的本地 Codex App Server；普通聊天和每个项目分别维护独立的持久 Thread。Thread启动与恢复使用 `danger-full-access`，执行 Turn使用 `dangerFullAccess`和 `never`审批策略。项目模式下所有非控制自然语言默认进入当前项目 Thread；切换项目保留旧项目 Session，清空上下文只重建当前活动 Session。

当前架构、状态模型、安全约束和运行链路见 [Codex App Server当前架构](APP_SERVER_ARCHITECTURE.md)；已停用的隔离审批方案、实施阶段和回退记录见 [App Server历史迁移记录](APP_SERVER_MIGRATION_HISTORY.md)。

## 17. 手机远程控制电脑工作

目标是让唯一白名单用户在手机上驱动当前 Windows账号有权访问的文件、项目和进程，不再把微信限制为已登记项目问答。

执行边界：

1. 直接执行：读取、搜索、修改和新增普通文件，运行测试、lint、构建和诊断；安装当前项目的项目级依赖；创建普通 Git提交和普通开发分支；推送当前分支；删除或移动少量明确点名的普通文件。
2. 自动保护但不打断：每个写任务创建 checkpoint；依赖限制在项目环境；提交只包含本任务范围；普通推送禁止 force；删除或移动的目标不明确时先澄清。
3. 二次确认后执行：批量或递归删除目录、清空数据、修改 `.env`或凭证、CI/CD、数据库迁移或批量数据删除、全局依赖、系统配置、生产部署、公开发布、删除远端资源，以及 force push、rebase、改写历史和覆盖工作区。
4. 权限范围：当前 Windows账号能够访问的本机资源；项目列表只是快捷入口，不是文件系统边界。

执行任务使用六位内部编号进行持久化和精确诊断，但手机端通常通过“刚才的任务”自然关联。任务状态只有 `queued`、`running`、`completed`、`failed`和`cancelled`；旧 `waiting_approval`与审批文件仅保留兼容读取。任务完成后按“完成结论、逐文件实际修改、验证摘要、提交与推送状态”汇报；测试只展示一次总耗时，不返回底层命令和完整日志，也不使用可能与工作区原有改动混淆的模糊范围描述。失败时保存并返回脱敏后的阶段与原因。

每个主机执行任务在启动前为当前项目工作目录创建文件 checkpoint，任务结束、失败或取消后保存结束状态并计算该任务独立差异。checkpoint只覆盖项目目录中的普通文件，跳过 `.git`、依赖、构建产物、本机运行数据、凭证、符号链接和超限文件；因此它不是整台电脑备份，也不能替代版本控制。记录保存在本机 `data/checkpoints/<task_id>/`，禁止提交。

微信中的“刚才修改了什么”“查看刚才的修改”“查看某文件的修改”由确定性控制层读取 checkpoint，不再依赖模型临时解释当前总 Git差异。默认返回文件级摘要，小范围文本变更返回改前／改后，原始 diff按需截断展示。checkpoint默认保留 30 天且最多 20 个，只清理由本模块创建并登记的目录。

“撤销刚才的修改”只生成恢复预览，不立即写文件；用户再次回复“确认恢复刚才的修改”后才执行。恢复前逐文件比较任务前、任务后与当前三份内容：未发生后续变化的文件直接恢复；文本中的任务修改仍能唯一定位时，只反向撤销本任务内容并保留后续修改；无法安全定位、二进制变化或路径冲突的文件单独跳过，不再阻塞其他文件。任务新增文件只有内容未变化时才删除，任务删除文件只有路径未被重新占用时才恢复。执行恢复前先保存当前内容到 checkpoint内的恢复备份，不使用整仓 `git reset`、`checkout`或其他覆盖命令。

普通聊天和主机 Thread都使用本机持久 rollout。执行任务继续使用同一长期 Thread，Turn设置 `dangerFullAccess`与 `never`审批策略，直接在真实工作区修改、运行测试、读取错误并继续迭代。`turn/interrupt`用于取消运行中的任务；取消只停止后续执行，不回滚已经写入的真实文件。一次性 `codex exec`只保留为迁移期回退。

Thread映射只保存会话键、Thread ID和更新时间，不保存消息正文或凭证。恢复时使用当前 cwd、基础指令、`danger-full-access`和 `never`审批覆盖旧运行参数，不使用需要 `experimentalApi`能力的实验字段。恢复超时、通信失败或 App Server退出时保留原 ID并返回失败；只有服务明确报告 rollout不存在或损坏时才移除旧映射并安全新建。清空上下文只移除当前会话映射和本地最近历史，不影响其他项目。

### 17.1 确定性项目进度读取

“下一步”“当前进展”“项目当前任务”“还有什么没完成”“最优先做什么”和“有没有阻塞”等正式进度问题不得进入项目 Thread。确定性控制层每次都重新读取当前项目根目录的 `ROADMAP.md`，解析“当前状态”中的当前阶段、进行中、阻塞和下一步，并按需列出前 5 个真实未勾选项。文件内容不缓存，旧 Thread上下文不得覆盖磁盘事实。

读取器只接受已登记项目根目录中的 `ROADMAP.md`，限制文件大小并使用 UTF-8。文件缺失、结构不完整、编码错误或超限时直接返回明确错误，不回退给模型猜测。微信端自然语言由控制层调用读取器；本地 `wxbot project status`复用同一实现，作为桌面端 Skill和 CLI接入的稳定基础。

“当前任务怎么样了”按实时状态动态路由：后台任务仍在分析、排队或执行时返回该执行状态；后台任务已经结束时改为读取 `ROADMAP.md`，不得重复返回上一条完成报告。“刚才的任务怎么样了”或“这个任务怎么样了”明确查询最近一次后台执行结果，不受其是否结束影响。

### 17.2 微信主 Thread跨客户端可见性

普通聊天和项目主 Thread必须使用 `ephemeral=false`持久化，并在 `thread/start`或迁移用 `thread/fork`中设置 `threadSource=user`。创建完成后调用 `thread/name/set`写入简短名称：项目 Thread使用“微信 项目名”，普通聊天使用“微信助手”。这样 Thread既能由 wxbot恢复，也具备 Codex CLI和桌面端任务列表使用的用户任务归类元数据。

旧版本保存但缺少可见性标记的 Thread只迁移一次：先恢复旧 Thread，再 fork为持久、真实项目目录、`threadSource=user`的新 Thread，命名成功后才原子替换本地映射。任何 fork或命名失败都继续保留旧 `thread_id`，不得因展示迁移破坏微信上下文。迁移完成状态随 Thread映射持久化，后续重启只恢复新 Thread，不重复 fork。

微信中的 Thread列表、搜索、预览和重新绑定由确定性控制层处理，不进入模型。控制层通过稳定的 `thread/list`按当前项目真实 `cwd`列出未归档交互式 Thread，并在本地对名称、摘要和工作目录执行不区分大小写的搜索；通过 `thread/read`读取选中 Thread的元数据和最近对话，不需要先恢复该 Thread。微信只展示列表序号、短标题和一行“最近提问”，不展示完整 Thread ID；标题只使用显式 Thread名称，没有名称时统一显示“未命名会话”。“最近提问”优先从 `thread/read(includeTurns=true)`返回的最近对话中提取最后一条用户消息，过滤恢复提示、结构化意图提示和 JSON历史后再截断；详情不可用时回退到 `thread/list`的 `preview`，仍无法安全提取时显示“暂无安全提问”。列表末项和操作提示之间必须保留空行。

显式查看某个 Thread时也只展示过滤后的最近用户消息与最终回复。wxbot注入的恢复上下文、意图分类说明、执行包装提示及其携带的 JSON历史不得发送到微信；能够从固定边界字段提取本轮原始用户消息时只显示该原始消息，否则跳过整条内部消息。

Codex桌面端带附件的用户消息可能包含 `# Files mentioned by the user`、本机临时文件路径、`## My request for Codex:`和 `<image>`包装。列表与预览必须丢弃附件文件清单、路径和图片标签，只展示 `My request for Codex`后的真实文字；没有文字而只有图片时统一显示“发送了一张图片”，不得暴露临时文件名或本机路径。

重新绑定必须先用当前项目工作目录和基础指令成功执行 `thread/resume`，随后才原子更新当前会话键的 `data/thread_sessions.json`映射；恢复失败时保留原映射，其他项目映射和旧 Thread均不修改。列表结果只在当前 App Server进程内用于序号选择，服务重启后必须重新列出，避免把过期序号绑定到错误 Thread。

“当前会话信息”由 `thread/read`、当前路由和 App Server运行通知共同生成，展示当前项目、Thread名称、工作目录、模型、提供方、推理等级、上下文窗口和协议已返回的 Thread累计 Token用量。协议未返回的字段明确标记，不使用模型推断。

普通聊天／项目会话仍由用户显式切换决定，不使用模型自动猜测消息属于哪个会话。普通聊天进入独立聊天 Thread；切换到项目后，咨询、闲聊和执行要求都属于当前项目长期 Thread，直到用户明确切回普通聊天或其他项目。

项目内非控制消息不再进入临时分类 Thread，也不再要求模型先输出结构化 `answer`／`change`结果。消息由后台 Worker按到达顺序直接把未改写的微信原文交给当前项目长期 Thread；同一个 Codex Turn根据项目 `AGENTS.md`判断只回答、检查还是执行。这样“可以，改吧”“按刚才说的执行”等承接式消息直接继承原 Thread上下文，不生成中间 `task_request`，也不重复调用模型。

权限、工程纪律、结果格式等长期约束由项目 `AGENTS.md`和 Thread `baseInstructions`提供，不得拼接进用户消息。每个项目任务开始前都通过 `thread/resume`重新应用当前项目目录、最新 `baseInstructions`、`danger-full-access`和 `never`审批策略，随后在 `turn/start`再次显式传入相同的主机级权限，避免长期 Thread复用旧设置或规则。项目请求涉及修改时，Thread必须先实际调用工具；只有工具明确返回权限错误时才能报告只读，不得根据旧对话或主观判断声称当前环境只读。即时控制仍由确定性控制层处理；后台 Worker、取消、重启恢复和 checkpoint继续覆盖所有项目请求。后台完成回复统一以“当前项目：项目名”开头，并用空行与正文分隔，确保微信富文本渲染后项目标题仍单独成段。因 Thread丢失而重建时允许一次性注入脱敏历史恢复提示，但正常持久 Thread不得改写微信原文。

## 18. 长任务 Worker与状态恢复

微信轮询回调只负责打印并把消息交给入站调度器，不执行 Codex或项目请求。同一项目的非控制消息直接进入后台 Worker串行队列，保持上下文顺序；任务状态、任务列表和运行中取消进入即时控制通道，不受前一条 Codex调用阻塞。不再维护独立的“正在分析请求”阶段。

项目请求先持久化为 `queued`，再由独立 Worker依次迁移为 `running`、`completed`、`failed`或 `cancelled`。任务文件保存未改写的微信原文、项目、时间、脱敏结果和错误，不保存用户 ID、`context_token`或 iLink凭证；该本机状态文件由 Git忽略，并按消息正文敏感数据保护。

任务完成通知的验收状态只读取该任务记录自身的 `acceptance_status`。不得因为项目 `ROADMAP.md`中的主任务处于“待验收”，就把咨询、普通修改、提交或推送等其他完成任务自动标记为待验收；只有本次任务被明确标记为需要人工验收时，完成通知才显示“验收状态：待验收”。

同一主机 Thread同时只执行一个 `running`任务，其余请求可以保持为多个 `queued`任务。取消 `queued`任务立即生效；取消 `running`任务通过 `turn/interrupt`中断 App Server Turn，但不回滚已经写入的文件。进程启动时，`queued`任务继续执行，遗留的 `running`任务标记为失败并提示真实项目可能已有部分修改，不自动重放。旧 `waiting_approval`记录只保留兼容读取，不再进入新执行链。

### 18.1 Hermes风格通道交互

- 同一微信收发会话、同一普通聊天／项目路由中的连续纯文本使用2秒静默窗口合并，按到达顺序以换行连接后作为一条未改写用户输入处理。确定性控制命令立即执行，不进入合并窗口；图片、文件和跨项目文本不得互相合并。合并消息关联的所有 inbox key都必须在同一次处理后统一完成或标记不确定。
- 同一项目允许一个 `running`任务和多个 `queued`任务。新请求在已有运行或排队任务时仍持久化入队，按创建时间先进先出；即时提示返回前方等待数量。服务重启后继续执行原有 `queued`任务，遗留 `running`任务仍按现有规则标记为中断，不自动重放。
- “当前任务怎么样了”返回当前运行任务和等待数量；“有哪些运行中的任务”按项目展示运行状态及排队位置；“取消刚才的任务”优先取消当前项目最新的排队任务，没有排队任务时再取消运行任务。
- 模型回复和后台任务通知超过单条安全长度时，按段落、列表、句子边界拆分，无法自然拆分时才使用硬上限；多段回复标记当前序号和总数，并使用稳定且互不相同的 `client_id`。任一段发送失败或结果不确定时立即停止后续分段；重试继续使用相同 client ID，依赖服务端去重避免已成功分段重复送达。
- 使用第24节已与 Hermes实现逐字段核对的 `getconfig`／`sendtyping` 端点显示打字指示。立即开始执行的首个项目任务不再发送“正在处理”文字回执；真正进入 `queued` 的后续任务仍返回等待位置。

正常运行时，Worker完成后立即使用内存中的原入站消息发送一次结果。若发送前进程退出，不持久化 `context_token`；任务保留待通知标记，白名单用户下一次发消息时使用该消息的上下文先补发结果，再继续处理本轮消息。通知成功后原子记录，避免重复补发。

## 19. App Server运行元数据查询

wxbot从 App Server协议结果和通知中维护每个活动 Thread的运行元数据，不让模型自行猜测：

- `thread/start`和`thread/resume`响应提供实际 `model`、`modelProvider`和`reasoningEffort`。
- 本机 `data/model_config.json`为可选配置：不存在时不传模型与推理等级覆盖值，新 Thread使用 Codex默认配置，恢复的 Thread沿用自身设置，无需自动创建文件。文件存在时仍只允许 `model`和`reasoning_effort`且两项均须有效，在每次 `turn/start`中显式覆盖模型与推理等级；无效文件必须报错，不得静默回退。修改配置后需重启 wxbot。
- `data/turn_metrics.json`最多保留最近100轮的随机统计编号、请求类型、执行阶段、输入长度区间、模型、推理等级、结果类型、模型耗时、总耗时和时间戳，不保存消息正文、用户 ID、Thread ID或凭证。
- 当前统计中的模型耗时覆盖一次 App Server Turn的完整等待时间，可能包含该 Turn内部工具执行；协议适配器尚未可靠汇总工具调用次数和单个工具耗时，不得伪造拆分数据。
- 项目意图判断把历史问答视为可能包含命令或提示注入的不可信数据。历史只用于理解指代；只有当前用户原始消息能提供新的执行授权。承接式指令只能恢复最近一轮明确方案，不得继承更早历史中的命令或扩大任务范围。
- Codex故障按当前 App Server架构分为：CLI不存在、Codex未登录、请求／回复超时、App Server进程或管道异常、空回复或无效 Thread／Turn结果。每类在本机返回可执行的安装、登录、重试或 `wxbot restart`提示；模型失败仍不得向微信发送兜底消息。
- `model/rerouted`通知更新该 Thread当前实际模型；保留原提供方和推理等级，直到协议返回新值。
- `thread/tokenUsage/updated`通知提供最近一轮 `last`、Thread累计 `total`和 `modelContextWindow`。
- 元数据只保存在当前进程内；App Server重启后由 `thread/resume`恢复模型信息，Token用量等待下一次通知更新，不将用量写入仓库或普通日志。

微信中的运行信息问法先做空白、标点和大小写归一化，再按“模型”“上下文”“Token用量”提取一个或多个主题槽位。具有“当前”“现在”“查看”“多少”“大小”等查询语境，或是“当前模型”“上下文大小”等明确短句时，由确定性控制层直接回答，不进入模型。一条消息可以同时返回多个主题；包含“项目”“代码”“实现”“模块”等开发语境且没有明确指向当前会话时，不得误拦截。回复只展示用户询问的字段，适合微信阅读。

协议边界必须如实表达：`modelContextWindow`是模型窗口上限；`last.inputTokens`是最近一轮输入量，不等于精确的当前上下文占用；`total`是 Thread累计用量，也不等于账户剩余额度。App Server没有返回精确当前占用或账户剩余额度时，回复“当前协议未提供”，不得估算或让模型猜测。

如果白名单用户重启后的第一条消息本身就是查询同一项目任务状态，系统不得先补发积压通知再返回相同查询结果；应合并为一次自然语言状态回复，并在该回复发送成功后清除待通知标记。失败或取消状态直接说明结果、脱敏原因和真实文件可能已发生的变化，不拼接内部状态字段。

## 20. 微信图片接收与 Codex图片输入

### 20.1 范围与依据

第一阶段只支持唯一白名单用户发送的单张原图，并把它作为当前长期 Thread的一次 Codex图片输入。支持“仅图片”、同一 iLink消息内的“图片＋文本”，以及微信客户端拆成两条连续消息的“先图片、后文字”；不支持图片发送、相册批量图片、缩略图降级、引用消息中的图片、动图语义保证、语音、视频或任意文件。

iLink字段和下载算法以2026-07-24核对的腾讯 `openclaw-weixin` 2.4.6、提交 `cef0bfc390393f716903e16d50408118047f87e0`为准，详细字段见 [iLink协议逐字段核对记录](ILINK_PROTOCOL_AUDIT.md)。Codex输入以 OpenAI当前 App Server协议为准：`turn/start.input`允许 `{"type":"localImage","path":"<absolute path>"}`，可与原始文本项同时提交；远程 HTTP图片 URL不能直接作为 `image`项传入，必须先下载为受控本地文件。

### 20.2 入站模型

`MessageItem`扩展为能够表达以下结构：

```python
@dataclass(frozen=True)
class CDNMedia:
    encrypt_query_param: str | None
    aes_key: str | None
    full_url: str | None

@dataclass(frozen=True)
class ImageItem:
    media: CDNMedia | None
    aeskey: str | None
    mid_size: int | None
```

解析时保留 `item_list`顺序。文本仍只来自 `type=1`的 `text_item.text`；图片只来自 `type=2`的 `image_item`，不得从未知字段、正文链接或文件名猜测。一个消息包含多个图片项时，第一阶段把它判为暂不支持并消费但不回复，避免只处理其中一张造成错误理解。

图片不参与首次白名单配对。未配对或非白名单用户的图片只完成消息级忽略，不发起 CDN下载、不创建临时文件、不交给 Codex。

微信客户端把图片和紧随其后的说明文字拆成两个独立 iLink消息时，调度器按以下规则合并：

- 白名单用户的纯图片下载完成后先进入1.5秒等待窗口，不立即创建 Turn。
- 等待窗口内收到同一 `from_user_id`和 `to_user_id`的纯文本消息时，以文本消息的 `context_token`和消息标识作为回复上下文，把图片项放在文本项前，合并为一次 App Server输入。
- 合并后图片消息和文本消息共用一次处理结果：成功时两个入站记录同时进入 `completed`，失败时同时进入 `uncertain`，微信只发送一次回复。
- 等待窗口结束仍没有文字时，按纯图片创建 Turn。第二张图片、其他用户或其他 Bot会话不会与当前等待项合并。
- 进程关闭时不强行执行等待项；取消计时器、清理已下载图片并保留 `queued`记录，重启后按原顺序重新下载和合并，避免退出阶段产生额外回复。

### 20.3 下载、密钥与内容校验

处理顺序固定如下：

1. 要求 `image_item.media`存在，且至少有 `full_url`或 `encrypt_query_param`。
2. 密钥优先使用 `image_item.aeskey`：必须是32个十六进制字符并解码为16字节。否则解析 `media.aes_key`：Base64解码结果必须是16字节原始密钥，或32个十六进制 ASCII字符再解码为16字节。禁止截断、补零或尝试其他猜测格式。
3. 有 `full_url`时优先使用；否则使用固定 CDN base `https://novac2c.cdn.weixin.qq.com/c2c`拼接 `/download?encrypted_query_param=<URL encoded value>`。
4. 下载只允许 HTTPS；`full_url`主机必须与固定 CDN主机相同。禁止重定向，连接和读取均设置有限超时。
5. 采用流式读取，同时限制密文不超过20 MiB；超过上限立即终止，不把响应正文写入错误。
6. 使用 AES-128-ECB和 PKCS#7解密；密文长度必须是16的倍数，padding必须严格有效，明文不得为空且不得超过20 MiB。
7. 只接受文件头可确认的 JPEG、PNG或 WebP；扩展名由文件头决定，不采用服务端 URL、用户文本或未知文件名。其他格式在第一阶段作为永久不支持处理。

Python标准库不提供 AES。实现已在主人确认后新增项目依赖 `cryptography`，只使用其经过维护的 AES-ECB解密原语，不复制自制密码算法。

### 20.4 临时文件生命周期

解密后的图片写入项目根目录下的 `tmp/inbound-media/<message-digest>/<random>.<ext>`：

- 目录名来自稳定消息摘要，文件名使用随机值，不使用用户 ID、`msg_id`、URL参数或微信提供的名称。
- 写入使用独占创建，保存后解析为绝对路径，并再次确认路径位于 `tmp/inbound-media/`内且不是符号链接。
- 文件只从完成内容校验起保留到对应 App Server Turn进入终态；Turn成功、失败、超时或取消都在 `finally`路径清理文件和空目录。
- 进程启动时清理超过24小时的遗留文件；只允许清理本模块清单记录且仍位于 `tmp/inbound-media/`内的文件，禁止递归清空整个 `tmp/`。
- `data/inbox.json`的 `queued`／`processing`消息可以暂存恢复所需的 CDN引用和密钥，但进入 `completed`或 `uncertain`后继续按现有规则删除整个消息对象。图片明文、密文和绝对路径不得写入该文件。
- 图片内容、密钥、`encrypt_query_param`、`full_url`、绝对路径和散列不得进入普通日志、Turn指标、对话降级历史、checkpoint、Git或完成通知。

为了兼顾重启恢复，`queued`消息重启后重新下载；遗留的 `processing`消息继续转为 `uncertain`且不自动重放。临时图片不作为任务状态来源。

### 20.5 Codex App Server输入

`AppServerClient.turn()`从只接受文本改为接受按顺序构造的用户输入列表：

```json
{
  "threadId": "<thread id>",
  "input": [
    {"type": "localImage", "path": "C:\\Users\\example\\wxbot\\tmp\\inbound-media\\...\\image.jpg"},
    {"type": "text", "text": "<微信原文>"}
  ]
}
```

只有图片时只发送 `localImage`项，不合成“请查看图片”等隐藏提示；图片带文字时，文本必须保持微信原文，不拼接权限说明、意图分类、路径、历史 JSON或结果格式。这样桌面端和微信端继续共用同一长期 Thread和同一套 `AGENTS.md`／`baseInstructions`。

Turn开始前必须确认文件仍存在、是普通文件、路径仍在允许目录内且大小未变化。图片路径只作为 App Server传输字段，不出现在发送给微信的结果或项目对话降级历史中。

### 20.6 失败、游标与回复行为

失败分为两类：

- 永久失败：字段缺失、密钥格式错误、密文长度错误、padding错误、格式不支持、超过大小限制或多个图片项。该消息标记为已处理但不回复，允许本批游标推进；只记录固定错误类别。
- 暂时失败：DNS、连接、读取超时或 CDN 5xx。保持消息为 `queued`并不推进本批游标，沿用现有有界退避重试；不得发送兜底回复。

Codex拒答、空回复、超时、Turn失败或微信发送失败继续遵循现有规则：不发送兜底消息。入站任务状态按现有 `completed`／`uncertain`语义处理，图片临时文件无论结果如何都必须清理。

### 20.7 自动测试

实现前先补协议纯函数和 HTTP mock测试，至少覆盖：

1. `type=2`、`image_item.media`、`image_item.aeskey`和 `media.aes_key`解析。
2. 两种 AES key编码、无效 Base64、错误长度、非十六进制和优先级。
3. 固定 AES-128-ECB＋PKCS#7测试向量、错误分块和错误 padding。
4. `full_url`优先、CDN回退 URL编码、HTTPS／主机限制、禁止重定向、超时与20 MiB上限。
5. JPEG、PNG、WebP文件头和无效／空明文。
6. 白名单前不下载、单图下载、多图永久忽略及文本＋图片顺序。
7. App Server `localImage`绝对路径输入、纯图片输入和原文文本输入。
8. Turn成功、失败、超时、取消后的临时文件清理，以及启动时只清理清单内过期文件。
9. `queued`恢复、`processing`转 `uncertain`、终态清除媒体敏感字段。
10. 日志、指标、降级历史和完成通知不包含密钥、URL参数、绝对路径或图片内容。
11. 永久失败推进游标，暂时失败保留游标并重试。
12. 连续图片与文字在等待窗口内合并为一个 Turn和一次回复；纯图片超时后单独处理，不跨用户或 Bot会话合并，关闭时不额外发送。

### 20.8 真实微信验收

自动测试全部通过后，单次人工验收按以下顺序执行：

1. 确认当前唯一白名单、`tmp/inbound-media/`为空、普通日志和仓库中没有媒体敏感数据。
2. 从白名单微信发送一张小于20 MiB的 JPEG或 PNG，不附文字。
3. 确认 wxbot只下载一次、Codex能基于图片内容给出相关回复、微信只收到一次回复。
4. 再连续发送一张图片和一段明确问题，确认即使微信将其拆成两个消息气泡，Codex仍只创建一个包含图片与原文的 Turn，微信只收到一次回复；Thread后续文本追问能继承本轮语义。
5. 每轮结束后确认 `tmp/inbound-media/`无该图片，`data/inbox.json`终态记录不含消息对象，普通日志不含密钥、URL参数、路径或正文。
6. 人为制造一次可恢复的 CDN暂时失败，确认游标未提前推进；恢复网络后只处理并回复一次。
7. 发送一个不支持格式或超过限制的样本，确认不回复、不阻塞后续文本消息，并且只留下脱敏错误类别。

只有主人明确确认以上真实微信结果后，`ROADMAP.md`中的图片接收任务才能标记为“已完成”。设计完成或自动化测试通过都不等于真实验收完成。

## 21. 文本文件、日志附件和 Codex生成文件安全收发评审

### 21.1 评审结论与协议依据

本项目是主人个人使用的微信工作台。权限设计默认降低日常摩擦，不为多人、公开服务或企业租户场景增加不必要审批；唯一白名单仍作为远程控制当前 Windows账号的身份边界。iLink已经具备通用文件的入站字段和出站上传链路，但 Codex App Server没有通用本地文件输入类型，因此文件能力拆成两个独立阶段：

1. 已实现入站纯文本第一阶段：唯一白名单用户发送且能够严格验证为 UTF-8纯文本的附件可直接交给 Codex，不增加读取确认；附件内容是不可信数据，不得授予执行权限。
2. 已实现出站纯文本第一阶段：当前微信原文明要求发送且能唯一确定到当前项目内的普通文件时，不重复确认；敏感文件、范围外文件或来源无法确定的文件直接阻止。模型不得凭回复中的路径、Markdown链接或 `MEDIA:`一类文本自行触发上传。

iLink协议依据为腾讯 [`openclaw-weixin`](https://github.com/Tencent/openclaw-weixin) 于2026-07-26公开的参考实现：`MessageItem.type=4`对应 `file_item`；出站使用 `getuploadurl`取得上传参数，以 AES-128-ECB加密后通过 `POST application/octet-stream`上传到 CDN，从响应头 `x-encrypted-param`取得下载参数，再用该参数、AES key、文件名和明文长度构造 `file_item`并调用 `sendmessage`。其中 `file_item.media.aes_key`必须把32位十六进制 AES key字符串按 ASCII字节做 Base64，不是直接编码16字节原始 key。`wxbot`已据此实现入站 `file_item`解析和第一阶段纯文本文件出站。

Codex能力同时以本机当前 `codex app-server generate-json-schema`结果核对。`turn/start.input`只声明 `text`、`image`、`localImage`、`skill`和 `mention`，没有通用文件输入类型。后续实现不得虚构 `localFile`或把任意本机路径伪装为图片输入。

### 21.2 威胁模型

文件链路新增的风险至少包括：

- 入站文件名路径穿越、Windows绝对路径、保留设备名、替代数据流、符号链接或重解析点绕过。
- 扩展名、MIME和真实内容不一致；二进制、超大文件或压缩包导致资源消耗。
- 日志、配置、补丁或生成文件包含 token、密码、Cookie、用户 ID、`context_token`、本机绝对路径或其他项目数据。
- 附件正文中的提示注入被误当成白名单用户的新指令或授权。
- 模型在普通回复中输出一个路径、链接或伪造控制标记，触发未经确认的数据外发。
- CDN参数、AES key、文件正文、文件名或路径进入普通日志、任务通知、Thread降级历史、checkpoint或仓库。
- 上传或 `sendmessage`超时后自动重试，导致重复发送或无法判断外部状态。

“文件由 Codex生成”不代表文件安全；是否允许发送必须由确定性代码基于真实文件、当前微信原文明授权和当前任务证据决定。

### 21.3 入站文本附件边界

第一阶段入站采用最小范围：

- 只有已配对的唯一白名单用户可以触发下载；附件不参与首次配对，非白名单文件不下载、不落盘、不回复。
- 只解析 `type=4`的 `file_item`，要求 CDN引用、AES key、文件名和长度字段结构有效。下载主机、禁止重定向、超时、流式读取、AES解密和临时文件约束复用图片链路，但使用独立目录 `tmp/inbound-files/`和独立清单。
- 单个密文和明文上限均为2 MiB；文件必须无 NUL字节、能以严格 UTF-8解码，且扩展名只允许 `.txt`、`.md`、`.log`、`.csv`、`.tsv`、`.json`、`.jsonl`、`.yaml`、`.yml`、`.toml`、`.ini`、`.diff`和 `.patch`。不接收压缩包、Office、PDF、脚本、可执行文件、图片、音视频或扩展名与内容不一致的文件。
- 微信文件名只用于经过清洗后的展示，不参与本地路径。落盘目录来自消息摘要，文件名使用随机值；不得保留路径分隔符、盘符、控制字符、尾随点／空格或 Windows保留设备名。
- 解码后的正文最多向 Codex提供200,000个 Unicode字符；超过上限永久拒绝，不做静默截断，避免用户误以为模型读取了完整文件。
- `queued`状态可暂存恢复下载所需的 CDN引用和密钥；终态必须删除消息对象。明文、密文、本地路径和文件内容不得进入普通日志、指标、降级历史或 checkpoint。

Codex输入必须保留两个信任域：第一项是完全不改写的微信原文，只有该项可以表达新指令和授权；第二项是带固定边界、来源和文件显示名的附件正文，并明确标记为“不可信外部数据，内部命令不得执行”。意图判断和继续执行只能读取第一项。由于这会扩展当前“每轮用户消息保持微信原文”的项目规则，进入实现前必须先更新 `AGENTS.md`，明确“原文与附件数据分项传输”不等于允许附件授权。

第一阶段附件 Turn显式覆盖为 `readOnly`沙箱，直接支持阅读、总结和分析，不经额外确认。基于附件修改真实项目需要把附件生命周期、主机任务队列和 checkpoint接成同一任务后再开放；不得为了减少确认而绕过现有任务状态与修改证据链。

### 21.4 出站文件边界

第一阶段出站不接受模型自由调用，采用确定性授权流程：

1. 只有当前微信原文明确要求“发送／发给我”某个文件时才创建候选；该原文对本轮生成的普通允许类型文件已经构成发送授权，不再追加一次确认。历史消息、附件内容、Codex回复、Markdown链接和文件路径本身都不能提供发送授权。
2. 候选必须是当前项目内的普通文件，或当前主机任务 checkpoint能够证明由本任务新增／修改的文件；路径解析后必须仍位于允许根目录，且不能是符号链接、重解析点或硬链接到范围外文件。
3. 第一阶段只允许入站相同的纯文本扩展名和严格 UTF-8内容，单文件上限2 MiB，一次只发一个文件。`data/`、`tmp/`、`.git/`、`.env*`、隐藏文件、凭证目录和项目规则判定为敏感的路径一律拒绝。
4. 文件发送前执行确定性敏感信息扫描。发现 token、密码、私钥、Cookie、`context_token`、微信用户标识或本机绝对路径时直接阻止，不自动修改原文件。日志如需脱敏，必须生成独立副本，预览脱敏规则和命中数量，再重新确认该副本。
5. 普通文件在本轮原文明授权、项目内唯一定位并通过类型、路径、内容和大小检查后可直接发送。文件来源不能由当前任务证明、路径敏感或扫描结果不明确时直接拒绝并说明原因，不通过模型回复或附件正文补充授权；发送前文件变化时失效。
6. 确认后把文件复制到 `tmp/outbound-files/<task-id>/`的受控暂存区，上传前再次校验类型、大小和摘要。上传密钥随机生成，只保留到发送进入终态。
7. `getuploadurl`、CDN POST和 `sendmessage`分别记录脱敏阶段状态。发送成功后不保留上传密钥；发送前确定失败直接返回失败，CDN POST或 `sendmessage`发生超时则标记 `uncertain`，禁止自动重试，避免重复外发。

完成通知只说明文件名和“已发送／状态不确定”，不得显示绝对路径、CDN URL、上传参数、AES key、文件正文或敏感扫描细节。

### 21.5 后续实现与验证顺序

实现与验证顺序为：

1. 已更新 `AGENTS.md`中的个人工作台定位、原文／附件双信任域和低摩擦权限规则。
2. 已实现 `file_item`模型、AES／CDN下载、文件名清洗、UTF-8内容校验、白名单后的受控临时文件和 Codex双输入项。
3. 已覆盖附件提示注入不能授权执行、终态清除媒体字段和临时文件清理；自动化回归通过后等待真实微信验收。
4. 入站真实微信验收完成后，已按当前参考实现增加出站上传，并继续保持收、发链路相互独立。
5. 出站自动化测试覆盖路径穿越、符号链接、摘要变化、敏感命中、CDN上传字段、超时 `uncertain`、不自动重试和全链路日志脱敏。
6. 真实发送属于人工验收；自动化通过后仍保持“待验收”，只有主人明确确认微信收到正确文件且本机未残留敏感数据后才能标为“已完成”。

PDF、Office、压缩包、源码脚本、图片出站、多个附件、语音和视频均不属于第一阶段；以后逐类增加内容验证与风险策略，不使用“任意文件”总开关。

## 22. 微信语音输入能力评审

### 22.1 结论

第一阶段值得实现，但范围应限定为“使用 iLink已经提供的语音转写文字作为 Codex输入，Codex继续回复文字”，暂不下载音频、暂不在本机转码、暂不接入第三方语音识别服务，也不实现语音回复。

原因如下：

- 腾讯当前 `openclaw-weixin`协议中，语音对应 `MessageItem.type=3`和 `voice_item`。`voice_item`可以携带 `text`转写文字，也可以携带 CDN `media`、`encode_type`、采样率和时长等音频信息。
- 腾讯参考实现优先把非空的 `voice_item.text`作为消息正文；只有没有转写文字时才下载语音媒体。下载后按现有 CDN AES链路解密 SILK，并尝试用 `silk-wasm`转为24 kHz单声道 WAV。
- 本机当前 Codex App Server的 `turn/start.input`只支持 `text`、`image`、`localImage`、`skill`和 `mention`，没有音频输入类型。即使下载出 `.silk`或 `.wav`，也不能直接作为 Codex输入，仍需先经过 STT得到文字。
- 当前项目没有 SILK解码、FFmpeg、本地 STT模型或云端 STT客户端依赖。为了语音输入直接增加这些依赖，会引入模型下载、运行资源、隐私、密钥和跨平台维护成本，不符合第一阶段最小实现。

因此，第一阶段是否可用取决于真实 iLink入站消息是否稳定提供非空 `voice_item.text`。进入实现前先用唯一白名单微信发送一条短语音，只记录固定字段是否存在、类型、转写长度和时长，不记录用户 ID、CDN参数、AES key、音频正文或转写正文。

### 22.2 最小实现范围

真实样本确认 `voice_item.text`可用后，第一阶段实现以下行为：

1. 在协议模型中增加 `VoiceItem`，只解析 `media`、`text`、`encode_type`、`sample_rate`和 `playtime`；未知字段继续忽略。
2. 只有唯一白名单用户可以触发语音处理；语音不参与首次配对，非白名单语音不下载、不转写、不回复。
3. 单条消息只接受一个语音项；要求 `voice_item.text`去除首尾空白后非空，并限制为现有文本消息相同的安全长度。不得把媒体字段、转写来源说明或权限包装拼入用户原文。
4. 经过长度校验的 `voice_item.text`作为本轮原始用户文本进入现有 App Server长期 Thread，可以表达普通对话和明确执行意图；权限仍由唯一白名单身份、当前语音转写文本和项目规则共同决定。
5. Codex只发送现有普通文字回复，不上传语音、不生成 TTS。
6. `voice_item.text`缺失、为空、过长或一条消息包含多个语音项时，第一阶段将消息标记为已处理但不回复；不得猜测内容、不得把原始 SILK伪装成 Codex输入，也不得自动调用外部 STT。

语音转写可能存在识别错误。批量或递归删除、发布、凭证、系统配置和 Git改写历史等现有二次确认红线不因输入来自语音而放宽；普通提交、当前分支普通推送和少量明确点名文件操作不增加一层“语音专属确认”。

### 22.3 隐私、状态与验证

- `voice_item.text`按普通微信正文处理，可以进入长期 Thread和必要的对话历史；CDN参数、AES key和音频媒体字段继续只允许在 `queued`／`processing`恢复状态中短暂存在，终态清除。
- 第一阶段不下载音频，因此不新增 `tmp/inbound-voice/`目录，也没有音频临时文件清理问题。
- 普通日志只允许记录“语音项存在、是否有转写、转写字符数、编码类型和时长”等固定元数据，不记录正文、用户 ID、媒体 URL、密钥或参数。
- 脱敏取样诊断已实现为 `data/voice_probe.json`，每次只覆盖保存唯一白名单用户的最新一条语音元数据：观察时间、消息项类型列表、`type=3`项数量、是否存在可解析的 `voice_item`、是否有转写、转写字符数、编码类型、采样率和时长。发现 `type=3`即记录，即使 `voice_item`缺失也能暴露结构差异。诊断不下载音频、不调用 Codex、不发送微信回复，也不保存正文、用户 ID、CDN参数或 AES key。
- 自动测试至少覆盖：`VoiceItem`解析、单语音转文本、多语音拒绝、空／过长转写、非白名单忽略、语音不参与配对、转写文本进入原长期 Thread、文字回复、重启恢复、终态媒体字段清除和日志脱敏。
- 真实验收先发送一句无敏感信息的短语音，确认只创建一个文本 Turn并收到一条相关文字回复；再发送一条明确但无风险的项目状态问题，确认长期 Thread上下文正常。只有主人明确确认后才能标记实现完成。

如果真实样本没有 `voice_item.text`，本阶段停止实现并重新评审 STT方案。优先级依次为：受控云端 STT、维护活跃且可锁定版本的本地推理方案、系统工具链；新增项目级依赖或模型文件可在明确实施任务中直接处理，涉及 API凭证或系统软件时仍必须单独说明成本并取得二次确认。

### 22.4 真实取样结论

2026-07-26 使用唯一白名单微信完成真实取样：

1. 同一 ClawBot会话发送“语音诊断在线”后，wxbot正常回复，入站完成数从84增加到85，证明登录、轮询、白名单和文本链路正常。
2. 紧接着发送一条3秒微信原生语音，客户端显示发送成功；经过完整轮询周期后，入站完成数仍为85，`data/voice_probe.json`未生成。
3. 此前两次短语音也没有产生可观察的语音诊断记录。放宽探针为发现任意 `type=3`项即记录后，结果仍不变。

因此当前真实 iLink Bot会话没有通过 `getupdates`投递语音事件。该限制发生在 wxbot收到消息之前，不是 `voice_item`解析、AES解密、SILK转码或 STT失败。本阶段停止语音输入实现；增加本地模型、云端 STT或转码依赖无法解决“没有入站音频事件”的前置阻塞。

保留脱敏探针用于以后 iLink能力变化时复测。只有腾讯侧开始投递 `type=3`或提供等价语音事件后，才恢复22.2节的最小实现；在此之前不继续试错、不宣称支持语音输入。

## 23. 音频文件普通附件出站

当前 iLink和 Hermes均没有经过真实验证的原生微信语音气泡发送闭环。第一阶段改为发送现有音频文件附件：继续使用 `media_type=3`上传，并构造 `type=4 file_item`，不使用 `media_type=4`或 `voice_item`，微信客户端按普通文件下载和播放。

范围固定如下：

1. 只有唯一白名单用户当前微信原文明要求“发送／发给我”一个明确文件时才触发，不接受模型回复或历史消息授权。
2. 文件必须位于当前项目内，是非符号链接普通文件，扩展名只允许 `.mp3`、`.wav`、`.ogg`、`.m4a`和 `.silk`，大小为1字节至20 MiB。
3. 拒绝 `data/`、`tmp/`、`.git/`、`.env*`、凭证目录、密钥后缀和其他现有敏感路径。音频是二进制内容，不执行 UTF-8和文本敏感词扫描；因此第一阶段只允许主人明确点名文件，不使用 checkpoint模糊候选自动发送。
4. 发送前记录 SHA-256并在上传前复核；变化后拒绝。上传使用现有 AES-128-ECB、`getuploadurl`、CDN POST和 `file_item`链路，上传密钥不落盘。
5. CDN POST或 `sendmessage`结果不确定时不自动重试。微信只收到文件附件，不发送额外确认文字。
6. 自动测试覆盖允许扩展名、路径穿越、敏感目录、大小限制、摘要变化、协议字段和不生成 `voice_item`。真实验收要求电脑端和手机端均能下载并播放同一个小音频文件。

本阶段不生成音频、不调用 TTS、不转码、不判断音频真实性，也不把音频交给 Codex分析。原生语音气泡以后必须单独验证 SILK编码、播放时长和客户端兼容性后再评审。

## 24. 微信打字指示器

微信工作台的长任务和模型回复常需数十秒到数分钟，此前微信端没有任何"正在处理"的反馈。参照 Hermes Agent `gateway/platforms/weixin.py`（2026-09-05 逐字段核对）已真实验证的 iLink 能力，增加 best-effort 打字指示器：处理消息和执行后台任务期间，微信端显示"对方正在输入…"。

### 24.1 协议

与 Hermes 的 `_get_config` / `_set_typing` 逐字段对齐：

1. `POST {base_url}/ilink/bot/getconfig`，body 为 `{"ilink_user_id": <user_id>, "context_token": <最新入站 context_token 可选>, "base_info": {...}}`，响应中的 `typing_ticket` 是打字状态凭证。
2. `POST {base_url}/ilink/bot/sendtyping`，body 为 `{"ilink_user_id": <user_id>, "typing_ticket": <ticket>, "status": 1|2, "base_info": {...}}`；`1` 开始显示，`2` 停止显示。
3. 两个端点都使用现有公共请求头（`Authorization: Bearer`、`X-WECHAT-UIN`、`iLink-App-Id`、`iLink-App-ClientVersion`），响应按 `ret` / `errcode` 判定成败。

### 24.2 行为

1. `TypingController` 持有每个用户的 `typing_ticket` 缓存，TTL 600 秒（与 Hermes 一致），过期后用下一次的 `context_token` 重新 `getconfig` 获取。
2. `AutoReplyService` 在消息通过白名单去重后调用 `start`，在回复发送、拒绝、跳过或异常的 `finally` 中调用 `stop`；后台执行任务在开始执行时 `start`，完成通知发送后（含失败路径）`stop`。同一用户的多个处理层可能重叠，控制器必须使用引用计数：每次 `start` 加一，每次 `stop` 减一，只在计数降为零时发送 `status=2`，避免消息处理层提前关闭后台任务的指示器。
3. 由于 iLink 打字状态可能自行过期（Hermes 对所有平台使用约 5 秒周期续期），`start` 后由单个后台线程按 5 秒周期对活动用户重发 `status=1`；`stop` 通过代次计数保证停止后不再续期。
4. 指示器完全 best-effort：任何网络或协议错误都只记录到 `last_error`，不重试、不影响回复链路、不写终端日志；未获取到 `typing_ticket` 时静默跳过。
5. 不修改白名单、信任域和权限边界；`getconfig` 只携带用户 ID 与该用户最新 `context_token`，不携带消息正文。

### 24.3 测试与验收

1. HTTP mock 覆盖两个端点的请求头、请求体字段、`typing_ticket` 提取、错误码传播和 ticket TTL 过期刷新。
2. 控制器测试覆盖缓存命中、TTL 过期、续期周期、stop 后不再续期、异常吞掉和未启动时 stop 不发送。
3. 服务集成测试覆盖白名单消息 start/stop 成对、非白名单消息不触发。
4. 真实验收：自动回复运行期间微信端出现"对方正在输入…"，回复送达后消失；后台任务执行期间持续显示，完成通知后消失。

## 25. 分页会话兼容与后台状态修复

2026-10-03：App Server优先使用本机 Codex桌面端 `OpenAI/Codex/bin/<版本目录>/codex.exe`（按可用程序的更新时间选择），不存在时保持 PATH与 NVM的 CLI查找顺序。真实对比确认 CLI 0.144.6能读分页会话摘要但恢复返回 `paginated_threads is not supported yet`，桌面端 0.159.2能恢复同一会话；不清空旧 Thread或降级为新空白会话。恢复失败继续报告错误。此调整只选择既有可执行程序，不安装或更新全局软件。

项目列表补充识别“列一下当前项目”等本次明确问法，直接读取本地项目目录。后台任务入队但不发送即时回执时，使用独立 deferred状态，终端显示“已交给后台任务处理，等待完成结果”，不得称为 Codex拒答。

官方依据：https://learn.chatgpt.com/docs/app-server ，分页历史兼容说明与本机运行结果分别核对。

## 26. 微信固定命令

`/project add 路径`接受存在且可访问的绝对目录，允许非 Git项目、嵌套目录和其他盘符；含空格路径须加引号。以目录名作为项目名，登记保存到本机 `data/projects.json`，不创建或修改目标项目文件，不自动切换。列表合并既有一级 Git扫描与登记目录，重复路径返回原项目名，同名不同路径拒绝覆盖。目录消失时从可用列表中隐藏但保留登记，恢复后重新可用。项目 Thread、任务、进度、checkpoint和文件发送统一通过登记路径解析；登记表损坏时报错，禁止覆盖。

命令与参数由纯函数统一解析，帮助和用法来自同一命令表。采用 Hermes风格名称，完整清单见 `docs/WECHAT_COMMAND_PROPOSAL.md`。未知斜杠命令、错误参数和引号不配对直接返回用法；不调用模型意图分类。普通消息仍以微信原文进入原长期 Thread，不以翻译后的控制短语替换模型输入。

命令只读取微信原文，不读取附件正文。所有斜杠命令都是2秒文本合并边界；只读项目／任务控制和取消可立即处理，项目与普通聊天切换仍按消息到达顺序串行处理。需要 App Server的会话／运行信息查询沿用会话锁，不强行打断正在执行的模型轮次。

`/new`／`/reset`复用当前会话清理链路，保留其他项目、白名单和去重；`/reset-ai-state`与 `confirm`复用同一用户、5分钟有效期的全量 AI状态清理。`/rollback latest`保存本次预览的 checkpoint目标；`/rollback confirm`只恢复该目标，不因后来产生新任务而改选最新目标。文件发送复用既有路径、类型、大小、敏感内容和摘要复核。旧中文入口继续可用，帮助以固定命令为主。

帮助文本每条命令使用空行分隔为独立段落。2026-10-03微信截图显示标题与说明段落保持分隔，但单换行命令列表显示为连续文字；发送代码保持原文，帮助不再依赖单换行作为显示边界，不改变其他回复的格式。
