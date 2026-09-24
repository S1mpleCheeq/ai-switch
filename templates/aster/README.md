# AsterGate 参考模板

`claude.json` 和 `codex.json` 是工具使用的公开模板，不从开发者的本地 profile 导出。两个文件均不含可用密钥。

| 占位符 | 初始化时的来源 |
| --- | --- |
| `__ASTERGATE_API_KEY__` | 用户通过 `--ask-api-key` 或 `--aster-key-env` 提供的凭据 |
| `__ASTERGATE_CA__` | 用户的 CA 文件复制到其 ai-switch 私密资源目录后的绝对路径 |
| `__MODEL_CATALOG__` | 用户的模型目录复制后的绝对路径，仅 Codex |

`ai-switch profile template aster --app codex` / `--app claude` 可在未初始化时查看模板。使用 `init` 生成实际 profile，不要直接把占位符模板写入原生客户端配置。保存后的私密 profile 与固定基准不属于公开模板，不随发布包分发。

模板反映教程中的服务地址、模型别名、Gemini 子代理配置及 Claude 扩展字段。主模型仍由各自原生客户端调用；工具不会把 Codex 接入 Claude 运行时。CLI 是否识别扩展字段以及网关是否支持模型，需使用者按自己的版本与账户验证。

## 与教程及完整配置的关系

参考文档为 7 页的《fable5.1/gpt6astra 混动 gemini3.8flash》。模板提供 ai-switch 管理的通道字段，不是整份客户端配置，也不宣称逐字实现 PDF 中所有方案。

| 项目 | 教程位置 | 本工具的处理 |
| --- | --- | --- |
| Claude 地址、Fable 别名、Gemini 子代理、`enableArtifact: false` | 第 1 页 | 保留对应值；主模型选择与工作流设置另由模板显式给出 |
| Haiku 映射 Gemini、`ultracode` 使用建议 | 第 5 页 | 默认写入 Haiku 映射及 Claude 教程开关；字段是否生效取决于客户端 |
| Codex Gemini 子代理、子代理 effort `high` | 第 1 页 | 保留对应值 |
| Codex 主模型 effort | 第 2 页正文要求 Ultra；第 6 页示例为 `xhigh` | 模板采用验证基准的 `ultra`，不把两处写法说成字面一致；用户可编辑 |
| provider ID 与主模型 | 第 6 页示例为 `custom` / `Some-Model-1` | 具体化为 `aster` / `gpt-6-astra` |
| provider `name` | 第 6 页为 `OpenAI`，附注“远程压缩” | 保留 `OpenAI`，不自行假定该字段只是显示标签 |
| `disable_response_storage`、`approval_policy`、`default_permissions` | 第 6 页示例含这些字段 | 视为原生公共设置，模板不强制写入；沿用用户现有值，因此新用户不一定具有示例中的值 |
| Read/View hook | 第 2–5 页 | 使用适配后的 `read_guard.py`，通过 `options.read_guard` 在 Claude 渲染时挂载；不逐字复制脚本，也不自动写入 Codex 的 `hooks.json` |
| Claude SDK 替换 Codex runtime | 第 5 页的另一方案 | 未采用，保持原生 Codex |

本工具的 hook 只保留 Gemini 读取越界纠正和短切片提示。它用实际 payload 或 transcript 中的模型识别当前代理，不用默认子代理环境变量推断；不提供原脚本的强制启用/自定义模型列表入口，不能据此承诺激活 1M 上下文或改变缓存机制。

MCP、权限、信任目录、状态栏等公共设置不包含在模板中。JSON 中的 `values/env/agents/providers/options` 是 ai-switch 的 profile 格式，工具会转换成原生配置。Codex 模板只公开 Aster provider；初始化会保留用户自己已有的 Micu provider，不复制发布者的账户配置。

模型目录和 CA 文件没有随模板分发。使用不同的资源文件，即使 JSON 字段一致，也不能据此声称全部模型行为和 TLS 环境一致。
