# loomy2api

讯飞星火 Loomy 平台的 OpenAI/Anthropic 兼容网关：把账号池化，暴露标准 API。

## 功能

- **多账号池**：weighted 轮询、Top-N 选号、粘性会话、429 软冷却（有界指数退避）、熔断
- **登录**：面板内短信验证码 / 微信扫码 / 密码登录，全自动入池（无需手动抓 session）
- **OpenAI 兼容**：`/v1/chat/completions`（流式/非流式/工具调用/`reasoning_effort` 思考档位）、`/v1/embeddings`、`/v1/images/generations`
- **Anthropic 兼容**：`/v1/messages`
- **面板**：账号池、用量台账（落盘）、积分构成（上游流水对账）、平台任务（每日签到/兑换码/邀请码）、模型与档位（含试跑 playground）、代理出口、配置、日志
- **防风控**：请求字段与官方客户端逐字节对齐（devid/ua/version/traceid）、批量操作随机抖动、登录人类延迟、per-account 出口代理

## 快速开始

```bash
python -m loomy2api serve          # http://127.0.0.1:17890
```

面板 →「添加账号」→ 短信验证码登录，账号自动入池并持久化。

## 计费（逆向实测）

- 对话：每次保底 1 分；输出超过 `1000/倍率` token 后按 `ceil(completion/1000 × 倍率)` 加收
- 生图：110 分/次
- `reasoning_effort` 各档位对思考长度影响不显著，控成本压 `max_tokens`

## 免责声明

仅供学习研究。使用本项目产生的一切后果由使用者自行承担。
