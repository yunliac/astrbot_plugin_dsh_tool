# 可选 ingress 空闲释放补丁（离线生成）

此工具只生成一个**新文件**，不自动部署，不读取 token 配置，不修改或重启服务。原 ingress 没有 `/session/release` 时，AstrBot 端应跳过回收并警告；不能用 `/dsh end` 冒充内存释放。

## 已验证范围

- 上游 `dsh-astrbot-ingress` **0.3.23**，作者 **CCYellowStar2**，许可证 **MIT**；生成文件保留输入中现有的版权/许可信息，部署时保留上游许可证。
- 实际安装的 `@deepseek-ai/dsh`、`dsh-agent`、`dsh-agent-loop`、`dsh-session-persistence` 均为 **0.2.1-rc.2**。
- 检查对象为远端 `/root/.dsh/profiles/web/node_modules/dsh-astrbot-ingress/lib/index.js` 和 `/root/qqbot/node_modules/@deepseek-ai/` 下安装的 JS/types，**不是凭最新文档猜测**。
- 使用本次读取的完整 ingress 源生成补丁后，`node --check` 通过；另外有离线 mock 回归测试。尚未向运行中的服务部署，也没有真实线上释放验证。

**仅针对以上已审阅实现。** 版本号相同也不能保证自定义修改兼容：源码锚点不符会拒绝生成；锚点通过不等于其他版本运行时语义安全。升级 DSH 后必须重新审阅释放/维护实现，不能直接套用。

## 本地验证与生成

在仓库根目录执行（Node.js 需在 PATH；也可用 `NODE_BINARY` 指定 Node 可执行文件）：

```sh
python tools/test_patch_ingress_release.py
python tools/patch_ingress_release.py /path/to/original-index.js /path/to/index.release.mjs
node --check /path/to/index.release.mjs
```

生成器拒绝输入输出为同一路径、拒绝覆盖已有输出、拒绝重复补丁、拒绝不匹配的源码布局。测试不会连接远端或读配置。

部署必须由管理员在维护窗口**人工**完成：取得当前原始源码及备份，审阅生成差异和许可证，执行语法/回归检查，决定是否替换目标文件及重启/重载服务；需要回滚时人工恢复备份。不要在未评估热重载生命周期时直接编辑运行中的插件。npm 更新/重装可能覆盖补丁，需要重新审阅和生成。此仓库不会自动执行备份、替换或重启。

## 协议和保守边界

`GET /health` 增加能力 `session-release-v1`。新增可选路由：

```http
POST /session/release
Authorization: Bearer <原有 ingress token>
Content-Type: application/json

{"umo":"当前通道标识","sessionId":"调用方预期的完整 session-id","idle_seconds":1800}
```

不得把 token 写进 URL、日志或补丁源码。本补丁使用原 ingress 鉴权逻辑，支持其既有 `X-DSH-Token` 备用头。

- `200 {"ok":true,"released":true,"sessionId":"...","bound":true}`：已等待释放完成，并确认 agent 与 live session 从注册表消失；保留绑定、会话历史及工作区关联，下次入站由原 `getOrCreateAgent` 恢复**相同 sessionId**。
- 已冷却但历史确实存在时同样返回成功，附 `alreadyReleased:true`，以便超时重试。
- `400`：请求格式错误；`401`：鉴权失败；`409`：预期 session 不匹配、仍忙、未达空闲阈值、不持有 handle、未持久化或竞态变化；`501`：缺少必要运行时能力；`500`：释放失败或未完整脱离注册表。失败时不删除绑定，不声称释放成功。
- 空闲阈值使用服务端绑定更新时间和补丁观察到的 `session/event`、`agent/status` 活动时间的最大值，而不是仅相信客户端时间。新取得的 handle 从当前时间开始计时，网页对该 agent 的后续活动也会延后释放。
- 只释放**本 ingress 创建/恢复且保存了 AgentHandle 的 agent**。对 `agents.get()` 获取的网页/其他插件拥有的 agent，或没有 retained handle 的既有实例，返回 `409 agent_not_owned`，不尝试窃取生命周期所有权。
- 检查 `status === 'idle'`、inbox.nextTurn/nextStep 为空，并排除 turns/restoring/approval/question；额外使用 `runMaintenance` 同步认领真正 idle，避免状态仍为 idle 的维护任务被误释放。
- 释放中使用全局保守 gate 拒绝其他 ingress 请求（健康检查除外）；已有异步请求尚未结束时拒绝释放。SSE 生命周期由 turn 记录覆盖。吞吐量有意让位于安全。

## 已安装 API 与释放竞态

实际 `ctx.agents` 没有 `dispose(id)`、`remove(id)`、`unload(id)`；`create()` 和 `resume()` 返回 `AgentHandle {agent, dispose(): Promise<void>}`。`get()` 只返回裸 Agent，不授予释放能力。不能调用 `ctx.sessions` 删除注册表来代替正确释放。

已安装 AgentLoop 的 `dispose()` 在首次 await 前同步 `cancel({kind:'disposed'})`，然后等待 `whenIdle()`，清理 scope，关闭 persistence handle，最后 detach agent/session，**不删除磁盘历史**。补丁在 `runMaintenance()` 同步认领后的回调中启动 dispose，但回调自身不等待 dispose；外层先等维护回调退出，再 await dispose。这样在最终检查与冻结驱动之间没有事件循环间隙，也避免 dispose 等待自身维护任务形成死锁。

该技巧依赖**上述精确实现语义**，不是通用 DSH API 的跨版本保证。绝不能改成在维护回调中 `await handle.dispose()`。外部网页/其他同进程插件不受 ingress gate 控制；同步 claim + 同步 disposed 取消阻止释放竞态中的驱动重新唤醒，但其他持有旧 Agent 引用的代码仍须遵守宿主生命周期规则。

释放只解除可回收对象与作用域资源的引用，不保证 Node/V8 立即降低 RSS；共享缓存、其他插件引用、浏览器内存或仍活跃的独立子 agent 不在本端点的释放承诺内。

## 可选会话读取接口（1.2.1）

`sessions` 管理页需要只读接口 `/sessions/managed` 与 `/sessions/history`。生成器
`patch_ingress_sessions.py` 必须叠加在**已安装的 release 补丁之后**：

```sh
python tools/patch_ingress_release.py /path/to/original-index.js /tmp/index.release.mjs
python tools/patch_ingress_sessions.py /tmp/index.release.mjs /tmp/index.sessions.mjs
node --check /tmp/index.sessions.mjs
```

- 两个 GET 都要求原有 Bearer / X-DSH-Token；只允许 `tool:` 前缀绑定，`sessionId` 必须与当前绑定完全一致。
- `/sessions/managed` 最多返回 200 条，仅列出绑定、标题、cwd、状态和最近活动，不读取完整日志。
- `/sessions/history` 默认 100 条、最多 200 条，单条 16 KiB、总计 256 KiB；限制并发与频率，超大历史返回 413。
- 接口**不唤醒 agent**、不计入 release gate、不修改绑定或回收活动时间。
- 没有安装本补丁时，管理页仍可显示本地标记，导出会回退到本地任务摘要并明确标注。
