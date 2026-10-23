# DSH 工具（日常对话）

给 AstrBot 的**日常聊天**加一个 LLM 工具 `dsh_task`：模型自己判断什么时候该「动真格」，把写代码、执行命令、读写服务器文件、跑长任务这些活交给本机的 [DeepSeek Harness (DSH)](https://github.com/deepseek-ai/deepseek-harness) 执行，再把结果拿回来继续回答。

与 [astrbot_plugin_dsh](https://github.com/CCYellowStar2/astrbot_plugin_dsh)（`/dsh` 命令桥）互补：

| | astrbot_plugin_dsh | 本插件 |
|---|---|---|
| 触发方式 | 显式命令 `/dsh ...` | 隐式工具，模型自行判断 |
| 适用场景 | 整段会话交给 DSH | 日常聊天里偶尔需要动手 |
| 过程显示 | 可配置实时进度 | 只把最终结果返回给模型 |
| 配置 | 自带 ingress 地址 / token | **直接复用**它的配置，也可独立填写 |

## 效果

```
用户：帮我看一下服务器上 nginx 的报错日志，最近 100 行
Bot ：（模型判断需要动手，调用 dsh_task）
       → DSH 在服务器上执行 tail / grep
       → 把结论整理成自然语言回复
```

普通闲聊、问答、查资料不会触发工具；只有用户明确希望「实际动手」时才会调用。

## 前置条件

1. 本机已安装并运行 **DSH**，且启用了 `dsh-astrbot-ingress` 入站插件；
2. 已安装并配置好 **astrbot_plugin_dsh**（提供 ingress 地址与 token）；
3. AstrBot `>= 4.16`。

## 安装

把整个目录放到 AstrBot 的插件目录：

```text
<AstrBot>/data/plugins/astrbot_plugin_dsh_tool/
```

或在 AstrBot WebUI 的插件市场安装（上架后）。

安装后在「插件管理」里启用，并按需修改配置。

## 配置

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `ingress_url` | string | 空 | DSH ingress 地址，如 `http://127.0.0.1:3188`。留空则自动复用桥配置，再不行依次尝试 `host.docker.internal:3188`、`127.0.0.1:3188` |
| `token` | string | 空 | 与 DSH ingress 相同的 Bearer token。留空则复用桥配置 |
| `bridge_config_path` | string | 空 | 桥插件配置文件路径。留空自动查找 `<AstrBot>/data/config/astrbot_plugin_dsh_config.json` |
| `timeout_sec` | int | `900` | 等待 DSH 结果的超时（按流空闲时间算） |
| `max_result_chars` | int | `6000` | 返回给模型的结果最大字符数，超出截断 |
| `admin_only` | bool | `true` | 仅管理员可调用 |
| `allow_users` | list | `[]` | 额外允许的用户 ID（非管理员也可调用） |

**取值优先级**：本插件配置 → 环境变量（`DSH_INGRESS_URL` / `DSH_INGRESS_TOKEN` / `DSH_BRIDGE_CONFIG`）→ `astrbot_plugin_dsh` 的配置 → 内置默认值。

### 重要：把 AstrBot 的工具超时也调大

AstrBot 自身有一个「工具调用超时时间」，默认只有 **120 秒**：

```text
agent_runner.config.misc.tool_call_timeout
```

要跑长任务，请把它调到不小于本插件的 `timeout_sec`（例如 `900`），否则 DSH 还在干活，AstrBot 这边已经把工具调用掐断了。

### Docker 部署

AstrBot 跑在容器里时：

- 推荐直接填宿主机的 ingress 地址，例如 `http://192.168.1.10:3188`；
- 或用 `http://host.docker.internal:3188`（Docker Desktop 自带解析；Linux 需要在 compose 里加 `extra_hosts: ["host.docker.internal:host-gateway"]`）。

## 使用

装好即可，不需要任何命令。示例：

- 「帮我把 `/root/app/config.yaml` 里的端口改成 8080 并重启服务」
- 「看看服务器磁盘还剩多少，把超过 1G 的日志列出来」
- 「在 `~/projects/demo` 里跑一下测试，失败的原因贴给我」

模型会把这些转成完整的任务描述交给 DSH；如果判断只是闲聊，就不会调用。

## 权限与安全

> **这个插件等于让聊天模型在你宿主机上执行任意命令。** 请务必：

- 保持 `admin_only: true`，只让可信的管理员使用；
- 或在 `allow_users` 里精确限定用户 ID；
- ingress token 等同于宿主机执行权限，**不要提交到仓库、不要发到群里**；
- DSH 侧建议保留审批机制，或至少限制工作区与可执行操作；
- 群聊场景建议只对特定群开放（配合 AstrBot 的会话/白名单配置）。

插件本身不保存任何凭据，token 只从配置或环境变量读取。

## 工作原理

1. 模型调用 `dsh_task(task)`；
2. 插件校验权限后，向 `POST {ingress_url}/inbound` 发送：

   ```json
   {
     "umo": "tool:<unified_msg_origin>",
     "text": "<任务描述>",
     "senderId": "<发送者 ID>",
     "chatType": "group | private"
   }
   ```

3. 带 `Authorization: Bearer <token>` 消费返回的 SSE 流，收集 `text` 事件，直到 `done`；
4. 结果截断后作为工具返回值交给模型继续生成回答。

## 常见问题

**调用失败：连接被拒绝 / 超时**

- 确认 DSH 正在运行、`dsh-astrbot-ingress` 已启用；
- 确认 AstrBot 所在环境能访问 `ingress_url`（容器内 `curl` 一下最直接）；
- 检查 token 是否与 DSH 侧一致（401 一般是 token 问题）。

**任务执行到一半被中断**

- 多半是 AstrBot 的 `tool_call_timeout`（默认 120 秒）到了，调大它；
- 或者调大本插件的 `timeout_sec`。

**返回内容被截断**

- 调大 `max_result_chars`，但注意别撑爆模型上下文。

**不想让某个群用**

- 保持 `admin_only: true`，或用 AstrBot 的会话配置限制触发范围。

## 兼容性

- AstrBot `>= 4.16`（已在 `4.29.1` 上验证加载与工具注册）；
- 依赖 `httpx`（AstrBot 自带，仍建议保留 `requirements.txt`）；
- 平台无关，凡支持 LLM 工具调用的适配器均可。

## 开发

```text
astrbot_plugin_dsh_tool/
├── main.py            # 插件实现
├── _conf_schema.json  # 配置 schema
├── metadata.yaml      # 插件元数据
├── requirements.txt
├── README.md
└── LICENSE
```

改完代码后在 AstrBot 里重载插件即可；调试时看日志里 `[dsh_task]` 前缀的输出。

## 许可证

[MIT](LICENSE) © 2026 yunliac

## 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) —— 插件宿主（AGPL-3.0）
- [astrbot_plugin_dsh](https://github.com/CCYellowStar2/astrbot_plugin_dsh) —— 命令桥与 ingress 协议（MIT，作者 CCYellowStar2）
- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) —— 真正干活的执行端

## 联系

作者：**yunliac**  
邮箱：**2903934386@qq.com**  
GitHub：<https://github.com/yunliac>
