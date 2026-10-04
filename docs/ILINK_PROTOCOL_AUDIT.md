# iLink 协议逐字段核对记录

核对日期：2026-07-24

## 参考基线

- 维护方：Tencent
- 仓库：[Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin)
- 版本：`2.4.6`
- 提交：[`cef0bfc390393f716903e16d50408118047f87e0`](https://github.com/Tencent/openclaw-weixin/tree/cef0bfc390393f716903e16d50408118047f87e0)
- 核对源码：[`src/api/api.ts`](https://github.com/Tencent/openclaw-weixin/blob/cef0bfc390393f716903e16d50408118047f87e0/src/api/api.ts)、[`src/api/types.ts`](https://github.com/Tencent/openclaw-weixin/blob/cef0bfc390393f716903e16d50408118047f87e0/src/api/types.ts)、[`src/auth/login-qr.ts`](https://github.com/Tencent/openclaw-weixin/blob/cef0bfc390393f716903e16d50408118047f87e0/src/auth/login-qr.ts)、[`src/auth/accounts.ts`](https://github.com/Tencent/openclaw-weixin/blob/cef0bfc390393f716903e16d50408118047f87e0/src/auth/accounts.ts)、[`src/cdn/aes-ecb.ts`](https://github.com/Tencent/openclaw-weixin/blob/cef0bfc390393f716903e16d50408118047f87e0/src/cdn/aes-ecb.ts)、[`src/cdn/cdn-url.ts`](https://github.com/Tencent/openclaw-weixin/blob/cef0bfc390393f716903e16d50408118047f87e0/src/cdn/cdn-url.ts)、[`src/cdn/pic-decrypt.ts`](https://github.com/Tencent/openclaw-weixin/blob/cef0bfc390393f716903e16d50408118047f87e0/src/cdn/pic-decrypt.ts)、[`src/media/media-download.ts`](https://github.com/Tencent/openclaw-weixin/blob/cef0bfc390393f716903e16d50408118047f87e0/src/media/media-download.ts)、[`package.json`](https://github.com/Tencent/openclaw-weixin/blob/cef0bfc390393f716903e16d50408118047f87e0/package.json)

该仓库是腾讯维护的 OpenClaw 微信渠道实现，不是微信公众平台开放 API 文档。本项目仍将 iLink 描述为协议兼容实现。

## 公共字段

| 项目 | 腾讯参考实现 | wxbot核对结果 |
|---|---|---|
| `Content-Type` | 业务 POST为 `application/json` | 一致 |
| `AuthorizationType` | `ilink_bot_token` | 一致 |
| `Authorization` | 有 token时为 `Bearer <token>` | 一致 |
| `X-WECHAT-UIN` | 每次请求生成随机 `uint32`，转十进制字符串后 Base64 | 一致 |
| `iLink-App-Id` | 读取包级 `ilink_appid`，当前为 `bot` | 一致 |
| `iLink-App-ClientVersion` | `major << 16 | minor << 8 | patch` | 一致，使用 wxbot自身版本 |
| `base_info.channel_version` | 当前包版本 | 一致，使用 wxbot自身版本 |
| `base_info.bot_agent` | 自声明调用方标识 | 一致，wxbot使用 `wxbot/<version>` |
| `Content-Length` | 由 HTTP客户端根据 UTF-8 JSON正文生成 | 一致，由 `httpx`生成 |
| `SKRouteTag` | 可选配置 | wxbot未配置；非必需，本次不增加 |

## 登录

| 步骤或字段 | 腾讯参考实现 | 核对结论与处理 |
|---|---|---|
| 获取二维码 | `POST ilink/bot/get_bot_qrcode?bot_type=3` | 一致 |
| 二维码请求体 | `local_token_list`，最多携带最近10个本地 token | 单账号 wxbot改为重新登录时携带现有 token，首次登录传空数组 |
| 二维码状态 | `GET ilink/bot/get_qrcode_status?qrcode=...`，配对时追加 `verify_code` | 一致 |
| 状态 GET请求头 | 只带 App ID、ClientVersion和可选 RouteTag | wxbot移除多余的业务鉴权头和随机 UIN |
| `scaned_but_redirect` | 将轮询地址切换到服务端给出的 HTTPS host | wxbot固定为 HTTPS并拒绝缺失 host |
| `need_verifycode` | 读取配对码并继续轮询 | 一致 |
| `expired`／`verify_code_blocked` | 最多刷新二维码3次 | wxbot补齐刷新，不再立即结束登录 |
| `binded_redirect` | 视为已经连接，保留现有凭证 | wxbot有现有会话时返回原会话；无现有会话时明确报错，不再空转到超时 |
| `confirmed` | 要求 `ilink_bot_id`，返回 token、baseurl和扫码用户 ID | wxbot额外要求 token和baseurl非空，属于更严格的安全校验 |

## `getupdates`

| 字段或行为 | 腾讯参考实现 | wxbot核对结果 |
|---|---|---|
| Method／Path | `POST ilink/bot/getupdates` | 一致 |
| 请求体 | `get_updates_buf`＋`base_info` | 一致 |
| 首次游标 | 空字符串 | 一致 |
| 正常响应 | `ret`、`msgs`、`get_updates_buf`、`longpolling_timeout_ms` | 一致 |
| 会话失效 | `errcode=-14` | 一致，保留错误码并停止轮询 |
| 客户端超时 | 返回空消息并保留原游标 | 一致；wxbot允许比服务端建议值多5秒的网络余量 |
| 新游标为空字符串 | 按服务端原值继续 | wxbot修正为接受明确返回的空字符串，不再误用旧游标 |
| 消息解析 | `WeixinMessage[]`，文本在 `item_list[].text_item.text` | 文本范围一致，未知或无效消息安全忽略 |

## `sendmessage`

| 字段或行为 | 腾讯参考实现 | wxbot核对结果 |
|---|---|---|
| Method／Path | `POST ilink/bot/sendmessage` | 一致 |
| 外层字段 | `msg`＋`base_info` | 一致 |
| `from_user_id` | 空字符串 | 一致 |
| `to_user_id` | 入站消息发送者 | 一致 |
| `client_id` | 调用方生成的稳定标识 | 一致 |
| `message_type` | `2`（BOT） | 一致 |
| `message_state` | `2`（FINISH） | 一致 |
| 文本项 | `type=1`＋`text_item.text` | 一致 |
| `context_token` | 原样回传对应入站消息 token | 一致，并在缺失时拒绝发送 |
| 响应 | HTTP成功后检查非零 `ret` | 一致；wxbot兼容额外的非零 `errcode` |

真实验收：2026-07-24使用同一入站消息派生的稳定 `client_id`，连续提交两次内容、接收方和 `context_token`均相同的文本回复，两个请求均成功，微信端最终只展示一条回复，确认当前iLink服务端会对相同 `client_id`进行发送去重。

## 入站图片与 CDN 下载

本节只记录腾讯参考实现已经公开的入站字段和下载方式。wxbot当前尚未实现图片接收，表中的“待实现”不是验收结论。

| 字段或行为 | 腾讯参考实现 | wxbot设计结论 |
|---|---|---|
| 消息项类型 | `MessageItem.type=2` | 待扩展 `MessageItem`模型，保留文本项和图片项的原始顺序 |
| 图片结构 | `image_item.media`为原图 CDN引用，另有可选 `thumb_media` | 第一阶段只处理 `media`原图；缺失原图引用时不回退到缩略图猜测 |
| CDN引用 | `encrypt_query_param`、`aes_key`、`encrypt_type`和可选 `full_url` | 只保存活动任务所需字段；不得进入普通日志、历史或终态入站记录 |
| 优先密钥 | `image_item.aeskey`为16字节原始密钥的十六进制字符串，优先于 `media.aes_key` | 待实现严格十六进制长度校验 |
| 兼容密钥 | `media.aes_key`可能为 Base64后的16字节原始密钥，也可能为 Base64后的32字符十六进制文本 | 待实现两种格式的严格解析；其他长度直接判为永久无效 |
| 加密方式 | AES-128-ECB，使用 PKCS#7 padding | Python标准库不提供 AES；实现前需确认增加项目依赖 `cryptography` |
| 下载地址 | 优先使用服务端 `full_url`；缺失时使用配置的 CDN base拼接 `/download?encrypted_query_param=...` | 默认 CDN base与腾讯实现一致，为 `https://novac2c.cdn.weixin.qq.com/c2c`；只允许 HTTPS和受信主机 |
| 下载响应 | GET返回密文二进制内容 | 待实现流式大小上限、超时、HTTP状态和解密校验 |
| 本地保存 | 腾讯插件交由宿主统一媒体存储 | wxbot只写入项目 `tmp/inbound-media/`，Codex Turn结束后清理，不进入 `data/`或仓库 |

腾讯实现的默认媒体上限是100 MiB；wxbot面向个人手机图片，采用更严格的20 MiB密文和明文上限。这是本项目的本地资源保护策略，不是 iLink协议字段。

## 本次边界

- 不增加可选 `SKRouteTag`，当前单机文本闭环没有该部署要求。
- 本轮只完成图片接收与 Codex图片输入设计，不实现或真实下载图片。
- 第一阶段图片实现不包含图片发送、缩略图降级、引用图片、语音、文件、视频、typing或 notify接口。
- 不复制腾讯实现的日志内容，继续执行本项目更严格的脱敏规则。
