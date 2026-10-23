# DSH 工具（日常对话）

> **开发状态：本插件目前仍处于初步开发阶段，部分功能可能存在问题或尚不完善。使用过程中如遇到异常、功能失效或有改进建议，欢迎通过 [GitHub Issues](https://github.com/yunliac/astrbot_plugin_dsh_tool/issues) 反馈。反馈时请尽量附上插件版本、复现步骤和已脱敏的错误日志，切勿公开 token、API key 等敏感信息。**

给 AstrBot 的**日常聊天**加一个 LLM 工具 `dsh_task`：模型自己判断什么时候该「动真格」，把写代码、执行命令、读写服务器文件、跑长任务这些活交给本机的 [DeepSeek Harness (DSH)](https://github.com/deepseek-ai/deepseek-harness) 执行，再把结果拿回来继续回答。

支持**多会话并行**：同一个对话里，模型可以为不同任务开不同的 DSH 会话，互不阻塞；后台任务完成后再把结果发回聊天。

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

```
用户：同时做三件事：查 nginx 日志、跑一遍测试、看看磁盘
Bot ：（分别用 session="logs" / "tests" / "disk" + background=true 提交）
       → 三个 DSH 会话同时跑，谁先完成谁先把结果发回群
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
| `max_parallel` | int | `3` | 同时运行的后台任务上限（1–20） |
| `idle_reclaim_enabled` | bool | `false` | 空闲会话资源回收开关，需要 ingress 配套支持 |
| `idle_timeout_sec` | int | `1800` | 任务结束后空闲多久回收（秒，60–604800） |
| `idle_check_interval_sec` | int | `60` | 检查间隔（秒，10–3600） |
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

## 并行与多会话

DSH ingress 用 `umo` 区分会话：一个 `umo` 对应一条 DSH 会话。插件在默认 `umo` 后面加通道后缀，于是同一场 AstrBot 对话里也能开出多条互不干扰的 DSH 会话。

| `session` 参数 | 实际 umo | 行为 |
|---|---|---|
| 留空 / `auto` | `tool:<umo>` | 当前对话的默认会话（与 1.0 行为一致） |
| `build`（任意短名） | `tool:<umo>#build` | **同名复用**同一条 DSH 会话，上下文延续 |
| `new` | `tool:<umo>#new-<随机>` | 每次新开一条，不自动删除历史；可启用空闲资源回收 |

配合 `background=true`：

- 工具**立即返回**一个 ticket，DSH 任务在后台继续跑；
- 跑完后插件把结果直接发到聊天的同一个会话里（格式：`【DSH 后台任务完成】#ticket · 通道 · 会话`）；
- 每路通道同时只允许一个后台任务，避免后一个把前一个打断；
- 全局并发上限由 `max_parallel` 控制，超出会提示等一会儿。

示例：用户说「同时做 A、B、C」，模型可以一次提交三路：

```text
dsh_task(task="...A...", session="a", background=true)
dsh_task(task="...B...", session="b", background=true)
dsh_task(task="...C...", session="c", background=true)
```

三路各自在独立的 DSH 会话里并行执行，结果分三条消息陆续回到群里。

> 注意：同一个通道**不能**并发。ingress 收到同一会话的新消息时，会中断上一条正在推送的回合（DSH 里那轮可能还在跑）。所以并行任务一定要用不同的 `session` 名字，或直接用 `new`。
>
> `background=false`（默认）会一直等到 DSH 跑完才返回，因此同一轮里连续调用多个工具仍是串行的；要并行必须用 `background=true`。

## 空闲会话自动回收（1.2.0）

在 AstrBot 插件配置中打开 **启用空闲 DSH 会话资源回收**，例如：

```json
{
  "idle_reclaim_enabled": true,
  "idle_timeout_sec": 1800,
  "idle_check_interval_sec": 60
}
```

默认关闭，启用后每分钟检查一次，任务结束后连续空闲 30 分钟才尝试回收。
保存配置后重载插件生效。关闭开关不会主动释放任何会话，也不会恢复已经卸载的 agent；下次调用时自动恢复。

- **保留聊天历史和通道绑定**，只卸载 DSH agent 的内存资源；下次使用相同通道会从历史恢复，可能有短暂加载时间。
- 只管理本次插件加载以来，通过 `dsh_task` 返回过会话 ID 的会话。重启后不扫描/接管已有的 DSH 会话，也不清理历史文件或主动结束脱离 agent 管理的 shell 进程。
- 前台执行、后台执行、同通道排队的请求不回收。服务端还会检查运行/维护/审批/问题等待等状态，不确定就跳过。
- 并发任务可能仍共享工作目录；会话隔离不等于文件系统隔离。
- 回收失败保留记录并在下一轮重试，只有服务端确认释放了对应会话后才移除本地活动记录。

### 需要 ingress 配套接口

现有 `dsh-astrbot-ingress` 0.3.23 的 `/end` **只解除绑定，不释放 agent 内存**。
因此本插件不使用 `/end` 假装回收，而是调用鉴权的 `POST /session/release`：

```json
{"umo": "tool:<umo>#build", "sessionId": "session-...", "idle_seconds": 1800}
```

接口必须验证会话 ID 与绑定仍然一致、会话确实空闲且由 ingress 自己持有可释放句柄，
再调用官方 `AgentHandle.dispose()`；成功返回 `{"ok":true,"released":true,"sessionId":"session-..."}`。
绑定与磁盘历史保留。插件对旧接口的 404/405/501 仅警告一次并跳过，普通任务不受影响；
安装支持后重载 AstrBot 插件以重新检测。

仓库附带[独立兼容补丁生成工具和说明](tools/README.md)。它只生成新文件，不自动覆盖已安装 ingress，不自动重启 DSH。
补丁只能回收它自己创建/恢复并持有句柄的 agent；网页或补丁前已加载的会话会安全跳过。
**仅更新 AstrBot 插件而未安装 ingress 支持，不会实际释放 DSH 资源。**

## 会话管理页面（1.2.1）

插件安装后会在 AstrBot「拓展功能」里出现 **DSH 会话管理**页面，无需另开端口：

- 查看日常对话用过的 DSH 会话、来源、运行状态、最近使用和空闲倒计时；
- 搜索标题 / 通道 / 会话 ID，按运行、等待、空闲、冷会话筛选；
- 独立开关 **长期保留** 与 **免回收**，也可以给会话改名；
- 导出会话记录（Markdown / JSON）；
- 页面通过 AstrBot 登录鉴权，再用ingress token 在服务端读取；浏览器拿不到 token。

### 两个开关的区别

| 开关 | 作用 |
|---|---|
| 长期保留 | 持久化保存该会话的管理标记，便于筛选和后续管理；**不等于常驻** |
| 免回收 | 跳过空闲资源回收，让该会话的 agent 尽量保持加载 |

标记保存在 `<AstrBot>/data/plugin_data/astrbot_plugin_dsh_tool/sessions.json`，
AstrBot 重启后仍然有效。远端刷新不会覆盖用户设置的标记。

### 在 QQ 里自动标记

用户明确说“这个 DSH 会话长期保留 / 不要自动回收”时，模型可以调用 `dsh_session`：

```text
dsh_session(action="update", session="部署维护", long_term=true)
```

目标不明确（同一聊天有多个会话且没有指明）时会拒绝猜测。普通聊天里提到“长期保留”但并非指 DSH 会话，不应触发。

### 网页预览对话

点击会话行中的「查看对话」，可在网页中阅读用户和助手的文字正文，并点击「加载更多」向后翻页。
长消息会分段传输，不再受导出接口默认 100 条 / 每条 16 KiB 的预览限制。
阅读器明确标记是否已加载到末尾；不显示思考过程、工具调用及附件二进制内容，疑似凭据会脱敏。
冷会话历史文件超过 8 MiB 时仍会安全拒绝全量读取，页面将提示不可完整读取或本地摘要回退，不冒充完整历史。
需要在已有 managed-sessions 补丁后叠加 `tools/patch_ingress_preview.py`；未安装时仅能查看本地摘要。

### 导出说明

优先读取 DSH 的真实历史；如果 ingress 未安装会话读取接口或历史不可用，则导出插件本地记录的
“任务 / 返回摘要”，并在文件里明确标注，不会冒充完整 DSH 历史。
会话读取接口是**可选**的，见[补丁工具说明](tools/README.md)。

## 权限与安全

> **这个插件等于让聊天模型在你宿主机上执行任意命令。** 请务必：

- 保持 `admin_only: true`，只让可信的管理员使用；
- 或在 `allow_users` 里精确限定用户 ID；
- ingress token 等同于宿主机执行权限，**不要提交到仓库、不要发到群里**；
- DSH 侧建议保留审批机制，或至少限制工作区与可执行操作；
- 群聊场景建议只对特定群开放（配合 AstrBot 的会话/白名单配置）。

插件本身不保存任何凭据，token 只从配置或环境变量读取。

## 工作原理

1. 模型调用 `dsh_task(task, session="", background=false)`；
2. 插件校验权限，按 `session` 拼出 umo，向 `POST {ingress_url}/inbound` 发送：

   ```json
   {
     "umo": "tool:<unified_msg_origin>[#通道]",
     "text": "<任务描述>",
     "senderId": "<发送者 ID>",
     "chatType": "group | private"
   }
   ```

3. 带 `Authorization: Bearer <token>` 消费返回的 SSE 流，收集 `text` 事件，直到 `done`（并发下会记下 `ack` / `done` 里的会话 id）；
4. 前台模式：结果截断后作为工具返回值交给模型继续生成回答；
5. 后台模式：工具立刻返回 ticket，插件用后台 asyncio 任务继续消费 SSE，结束后通过 `StarTools.send_message` 把结果发回原会话（会自动按 1800 字分段）。

## 常见问题

**调用失败：连接被拒绝 / 超时**

- 确认 DSH 正在运行、`dsh-astrbot-ingress` 已启用；
- 确认 AstrBot 所在环境能访问 `ingress_url`（容器内 `curl` 一下最直接）；
- 检查 token 是否与 DSH 侧一致（401 一般是 token 问题）。

**任务执行到一半被中断**

- 多半是 AstrBot 的 `tool_call_timeout`（默认 120 秒）到了，调大它；
- 或者改用 `background=true`（不受这个 120 秒限制），或调大本插件的 `timeout_sec`。

**并行任务互相打断**

- 两个任务用了同一个 `session` 名字。把它们改成不同的名字，或让模型用 `new`。

**后台任务一直没回消息**

- 看 AstrBot 日志里有没有 `[dsh_task] 后台结果发送失败`；
- 确认 `max_parallel` 没被占满（超出会直接提示，不会静默丢弃）；
- 后台任务只在插件进程存活期间有效，重载/重启 AstrBot 会取消它们。

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
├── main.py            # 插件实现与 dsh_task / dsh_session 工具
├── management.py      # 会话管理数据接口
├── session_store.py   # 标记与本地摘要的持久化
├── webui.py           # 受 AstrBot 登录鉴权的管理接口
├── views/dsh/         # 拓展功能页面
├── tools/             # 可选的 ingress 补丁生成器
├── tests/             # 回归测试
├── _conf_schema.json
├── metadata.yaml
├── requirements.txt
├── CHANGELOG.md
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
GitHub：<https://github.com/yunliac>
