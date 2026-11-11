# 模型服务与板卡工具

模型服务页面顶部选择的是**板卡上运行的工具**，下面的卡片配置该工具使用的模型 API。工具和模型供应商可以不同；能否连接取决于 API 协议兼容性。

| 工具 | 可配置的接口 | 启动方式 |
| --- | --- | --- |
| Codex | OpenAI Responses；另支持 ChatGPT 官方账号 | 板卡 SSH 终端 |
| Claude Code | Anthropic Messages 或兼容网关 | 板卡 SSH 终端 |
| DeepSeek Harness | OpenAI Chat Completions、Responses、Anthropic Messages | 板卡后台服务 + 本地转发网页 |
| Gemini CLI | Gemini 原生 API 或兼容网关 | 板卡 SSH 终端 |
| OpenCode | OpenAI Chat Completions、Responses、Anthropic Messages、Gemini | 板卡 SSH 终端 |

Edge Switch 通过 SSH 为远程 Linux 板卡安装 CLI 工具、配置代理网络和大模型 API，并切换 API 供应商。模型服务顶部的图标表示 Codex、Claude Code 等远程 CLI 工具，不是 API 供应商。

模型服务参考 CC Switch 的工具图标栏和横向供应商卡片布局。Codex 工具使用 OpenAI 标志，**OpenAI Official** 卡片使用绿色 OpenAI 标志，并固定显示在 Codex 列表首位。悬浮卡片时显示启用、编辑、复制、测试和删除图标，悬浮图标查看说明；键盘聚焦时也会显示操作。API 复制会创建包含图标、接口配置和密钥的独立副本，沿用原记录是否加密保存密钥的选择。官方账号是当前设备环境的单个登录入口，其复制操作仅复制账号名称和备注。测试单条 API 不会切换当前活动服务；从远程板卡执行连通性检查，不发起付费推理。

添加服务按钮使用 `加.png` 图标，打开整页“添加新供应商”表单。顶部可切换“当前 CLI 供应商”和“统一供应商 · New API”。CLI 预设按顺序只保留自定义配置、该工具官方入口、OpenRouter、TheRouter；应用按工具和预设自动选择请求格式，无需手动选择协议。不同 CLI 的网关地址可能不同，预设地址参考仓库内 CC Switch 配置。供应商仍需提供对应工具兼容的接口，应用不执行协议转换。

统一 New API 在当前设备环境中保存一次，在各工具列表中复用；每个工具独立选择是否启用它。切换到 Claude 或 Gemini 时自动适配接口根路径和请求格式，实际可用性仍取决于 New API 部署支持的接口与模型。

预设下方可上传 PNG、JPG 或 WebP 图标（不超过 2 MB）；应用缩放为 128×128 PNG，随供应商配置保存在本机。恢复默认、关闭、取消或 Escape 不保存当前修改。

表单显示“API请求地址”和“模型名称”。模型名称可手填，也可点击“获取模型列表”后选择。列表获取由 Windows 控制端发出，使用当前设备环境的本机代理设置；列表卡片上的“测试”则从 SSH 连接的远程设备发出。查询失败不覆盖手填模型。

编辑时载入已保存的 API Key，默认以密码字段遮罩显示，点击眼睛查看或隐藏明文；更改请求地址会清空编辑框，必须输入目标地址对应的密钥。Codex 的 `config.toml (TOML)` 与 `auth.json (JSON)` 只读预览显示在表单底部，随字段自动生成，密钥预览随眼睛按钮一起显隐；远程配置仍由应用在同步时生成。其他工具显示连接配置摘要，不套用 Codex 的文件格式。

例如，选择 **Codex → 添加服务 → OpenAI / ChatGPT API**，填入模型 ID 和 API Key，就可以让板卡上的 Codex 使用该 API。选择 **DeepSeek Harness → 添加服务 → OpenAI / ChatGPT API**，也可以让 Harness 使用 OpenAI API；选 DeepSeek 时默认使用它的 Chat Completions 接口。

Codex 使用 DeepSeek 等模型时，需要供应商或中转网关提供 Responses 接口；DeepSeek 原生 Chat Completions 地址不能直接作为 Codex 的 Responses 地址。选择不兼容的供应商预设时，地址留空，要求填写兼容网关地址。应用不会自动转换协议。

**ChatGPT 官方账号**卡片用于 Codex 账号登录或导入 `auth.json`；**OpenAI / ChatGPT API** 是单独计费的 API Key 接入方式，订阅账号登录不等同于 API 密钥。

## 使用流程

1. 创建工作环境，或悬停具体环境后点击“编辑”。在“SSH 与网络”中配置板卡与代理，在“AI 工具”中查询版本、安装独立 Node.js 和所需工具。
2. 进入模型服务，选择 Codex、Claude Code、DeepSeek Harness、Gemini CLI 或 OpenCode。
3. 添加服务，选择 CLI 或统一 New API 供应商，填写 API 请求地址、准确的模型名称和对应 API Key。
4. 保存后悬停服务卡片，点击第一个启用图标；选中后变为“使用中”。悬停其他操作图标可查看含义。环境与供应商列表均支持拖动排序，顺序自动保存。
5. 展开列表下方的“验证与同步”，点击“连通性检查”读取模型列表；部分网关不提供模型列表，可以手填模型名称。推理验证会先提示可能消耗额度，确认后才发出最小请求；它不代表完整工具调用兼容性已验证。任务会自动弹出进度、结果或错误，也可在“设置 → 日志”中回看。
6. 点击“同步到板卡”，再点击“打开”启动工具。启动也会先检查并同步所选配置。

首次使用时，各 CLI 默认展示自己的官方入口：Codex 为 OpenAI Official，Claude Code 为 Claude Official，Gemini CLI 为 Gemini Official，OpenCode 为 OpenCode Zen。Codex 在“设置 → 认证”中登录或导入凭据；其他工具点击官方卡片的登录图标，在板卡终端按提示授权。打开登录终端并不代表授权完成。各工具的官方登录与自定义 API 使用独立目录，切换后保留凭据。Harness 默认展示待配置的 DeepSeek API，填写模型和 API Key 后使用；已有供应商不会被清除。

Harness 多供应商设置要求 **0.2.2 或更新版本**。当前适配器不提供 Gemini 原生自定义路由；需要使用 Gemini CLI、OpenCode，或为 Harness 选择兼容的 OpenAI 网关。Harness 运行期间更换配置会被拒绝，请先在具体工作环境的“编辑 → AI 工具”中停止 Harness，再同步并重新打开，避免中断现有任务；配置不变时可以重新打开现有网页。

## 配置与凭据

旧版未标注所属工具的 API 记录继续归于 Codex，已有 ChatGPT 官方账号保留。其他工具新建独立服务卡片和活动服务绑定。

本地可选择 Windows DPAPI 加密保存 API Key；不勾选时，密钥仅在本次进程中保留。更改 API 地址必须重新输入目标地址对应的 Key。同步到板卡后，运行所需密钥以仅当前用户可读的文件保存在该工作环境的专属目录。应用使用工具专属配置目录与启动脚本，避免串用其他工具的 API Key；不清除用户项目已有的工具配置。

Harness 通过独立 `--patch` 配置多供应商适配器和默认模型，不覆盖用户的 `settings.yaml`、home patch 和现有会话。OpenCode 使用独立配置文件与数据目录，Gemini 使用 `GEMINI_CLI_HOME`，Claude 使用 `CLAUDE_CONFIG_DIR`。模型卡片的“删除”删除本地记录和本地密钥；已有板卡文件保留。

### Codex 账号切换与聊天记录

同一 Linux 用户、同一 Edge Switch 工作环境内，远程 VS Code 的 Codex 插件和受管 Codex 终端共享聊天历史。官方账号、API 供应商使用固定的 `edge` 历史标识；切换服务时，已有会话仍可出现在历史列表中。两个认证目录继续分别保存官方 OAuth 与 API Key，不共用认证文件，不扫描其他工作环境或用户原有的 `~/.codex`。

升级后首次点击“同步到板卡”或打开 Codex 时，会合并该工作环境原有 `codex-home` 与 `codex-chatgpt-home` 的会话、归档和历史索引，写入 `codex-history`；`sqlite_home` 固定指向此目录，新版索引也沿用同一位置。原数据保留在同级 `codex-history-backup-v1`。只调整会话头部与索引中的供应商标识，消息正文保留。

首次迁移前需要关闭该工作环境的远程 VS Code 窗口及 Codex 终端，然后重新同步并打开。终端 Codex 与 VS Code 附带的 Codex 可以使用不同版本：历史库属于同一条成功升级链、旧字段可无损导入时，应用自动采用较新结构合并。若升级记录不兼容、字段无法无损导入、记录冲突，或迁移中断后原目录继续产生记录，应用会停止处理并保留数据，不能通过删除备份强行重试。迁移完成后，切换服务并重新打开 Codex 即可读取同一历史；继续旧会话时，请先确认当前供应商，因为后续上下文将发送给当前选定的服务。

## 参考与验证范围

品牌图标来自 [LobeHub Icons](https://lobehub.com/zh/icons)，以本地 SVG 随应用打包，授权与固定来源版本记录在 [图标目录](../image/providers/README.md)。

配置格式参考 [Claude Code settings](https://code.claude.com/docs/en/settings)、[Gemini CLI configuration](https://geminicli.com/docs/reference/configuration/)、[OpenCode providers](https://opencode.ai/docs/providers/) 及发布到 npm 的 `@deepseek-ai/dsh`、`@deepseek-ai/dsh-base`、`@deepseek-ai/dsh-llm-pi-ai` 0.2.2-rc.1 文档。Codex 延续已有的 Responses 配置与官方账号隔离实现。

本地回归覆盖工具服务隔离、配置生成、原生协议测试请求、密钥边界与桌面交互。未使用真实 API Key 调用付费模型，未连接真实板卡；真实供应商的模型权限、流式响应、工具调用以及不同 Linux 架构上的安装仍需实机验证。
