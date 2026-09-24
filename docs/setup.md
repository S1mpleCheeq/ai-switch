# 首次配置与 profile 管理

## 1. 选择客户端

安装 Python 3.10+，以及需要使用的原生 Codex 或 Claude Code。运行 `codex --version` / `claude --version` 确认 PATH 能找到它们。不要为了初始化而伪造另一客户端的配置：`init --app codex` 或 `init --app claude` 即可。

本版本的 `micu` 是“用户原先已接好的 Micu 配置”，不是作者提供的免费账户。需要先在所选原生客户端中配置自己的服务地址、模型与凭据，并确认一次普通请求能够完成。

Codex 配置的 provider ID 需为 `micu`。以下只是结构示意，应合并到自己的配置中，不要整份覆盖 MCP、项目权限等设置：

```toml
model_provider = "micu"
model = "YOUR_AVAILABLE_CODEX_MODEL"
model_reasoning_effort = "high"

[model_providers.micu]
name = "Micu"
base_url = "https://www.micuapi.ai/v1"
wire_api = "responses"
env_key = "MICU_API_KEY"
```

上例的 `MICU_API_KEY` 需在运行原生客户端和首次初始化的同一个终端中有值。可在 Bash 中隐藏输入，避免把值写进 shell 命令历史：

```bash
read -rsp 'Micu API key: ' MICU_API_KEY
printf '\n'
export MICU_API_KEY
```

这是 Bash 语法，macOS 用户可以先执行 `bash`。初始化会把该环境变量的值私密保存到 profile；以后通过 `ai-switch run` 启动时会注入所需环境变量。直接执行原生 `codex` 时仍需满足它本身的凭据环境要求。已经使用内联 token 的有效原生配置也可保存，无需改为此示例。

Claude 的 `~/.claude/settings.json` 需有有效的 `model` 和 `env.ANTHROPIC_BASE_URL`，当前初始化要求地址属于自己的 Micu 接入；凭据使用本人已有的 `ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_API_KEY`。结构示意：

```json
{
  "model": "sonnet",
  "env": {
    "ANTHROPIC_BASE_URL": "https://www.micuapi.ai",
    "ANTHROPIC_AUTH_TOKEN": "YOUR_PERSONAL_MICU_KEY",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "YOUR_AVAILABLE_CLAUDE_MODEL"
  }
}
```

使用服务商为自己账户提供的实际地址和模型名。上面的占位符不能用于请求，示例文件不应提交到公共仓库。原生登录账户/OAuth 模式和任意服务商的首次导入不在当前 `init` 范围内；初始化后可以新增其他 API profile。

## 2. 准备 Aster 资源

- 自己的 AsterGate API key。
- 教程/服务商提供并经自己确认来源的 CA 证书文件，使用绝对路径。
- 若管理 Codex：相应的 `model-catalog.json`，至少包含 `gpt-6-astra` 和 `gemini-3.8-flash-high` 两个模型条目，实际客户端需要完整有效的模型元数据。

本仓库只内置无凭据的 profile 结构，不分发账户、不自动下载或信任证书，也不把简化的测试模型目录当作生产资源。Claude 的 Node 请求会使用 profile 中的 `NODE_EXTRA_CA_CERTS`。Codex 原生进程需要自己的 TLS 信任链已经接受目标网关证书；`--ca` 不会替用户修改系统信任库。`check --network` 加载证书成功也不代表 Codex 的系统证书配置已完成。

模型目录与证书的获取和信任步骤，请依照自己的服务商说明；不要关闭 TLS 验证。Aster 服务地址和教程模型来自配置模板，可先用 `ai-switch profile template aster --app codex` 查看。

## 3. 初始化

仅 Codex：

```bash
ai-switch init --app codex --catalog /absolute/path/model-catalog.json \
  --ca /absolute/path/astergate-ca.crt --ask-api-key
```

仅 Claude：

```bash
ai-switch init --app claude --ca /absolute/path/astergate-ca.crt --ask-api-key
```

两者：

```bash
ai-switch init --app all --catalog /absolute/path/model-catalog.json \
  --ca /absolute/path/astergate-ca.crt --ask-api-key
```

`--ask-api-key` 只询问本人的 AsterGate key，需交互终端。自动化中可以用 `--aster-key-env ASTERGATE_API_KEY`，从已有环境变量读取值；两个入口不能同时指定。默认环境变量名仍为 `ASTERGATE_API_KEY`，兼容原用法。不要把真实 key 写在命令参数中。

初始化不切换当前配置。`profile list` 可看到 `micu` 与 `aster`；`baseline list` 可核实两个固定基准。`all` 在单客户端安装中表示所有已初始化客户端，不会触碰另一个客户端。

重复 `init` 会拒绝覆盖原基准。本版本未提供“向同一管理目录追加客户端”的命令。若日后需要另一个客户端，可为它使用独立管理目录，并始终在命令前指定该目录，例如：

```bash
ai-switch --state-dir ~/.config/ai-switch-claude init --app claude \
  --ca /absolute/path/astergate-ca.crt --ask-api-key
ai-switch --state-dir ~/.config/ai-switch-claude use aster
ai-switch --state-dir ~/.config/ai-switch-claude run claude
```

不要让两个管理目录同时管理同一客户端的同一配置文件。

## 4. 新增、修改和删除 profile

```bash
ai-switch profile add backup --from micu
ai-switch profile edit backup --app codex --base-url https://api.example.com/v1 --ask-api-key
ai-switch profile edit backup --app codex --model YOUR_AVAILABLE_MODEL --effort high
ai-switch profile show backup --app codex
ai-switch use backup --app codex
```

`--from micu` 复制普通接入配置，`--from aster` 复制教程设置。新增只复制已初始化客户端的部分，不自动切换。`profile show` 的凭据已脱敏，不能直接把它的输出作为完整 profile 导入。

Claude 的编辑方法相同，把 `--app codex` 换为 `--app claude`，使用该服务商的 Messages API 基地址与模型。Claude 的 `--effort` 使用 `low/medium/high/xhigh`；Codex 使用当前客户端和模型支持的值。

不指定修改字段时，`profile edit NAME --app CLIENT` 打开 `VISUAL` / `EDITOR` 指定的编辑器，默认 `vi`；编辑临时文件包含真实凭据，权限 0600，退出后删除。可用 `--file` 导入自己准备的完整客户端 profile，但需保持原 provider ID，且不能包含脱敏或模板占位符。

```bash
ai-switch baseline protect backup        # 可选：一次性固定自己的参考基准
ai-switch use micu
ai-switch profile delete backup
```

正在使用的 profile 不能删除。删除不会移除历史、备份或固定基准。

## 5. 教程设置可以调整

模板只是参考默认值，不要求所有任务都使用高推理强度或子代理：

```bash
ai-switch profile edit aster --app codex --effort high
ai-switch profile edit aster --app claude --effort high --ultracode off
ai-switch profile edit aster --app codex --clear-subagent
```

Claude 的 Haiku 别名映射与默认子代理模型是不同设置；`--clear-subagent` 不清理 Haiku 映射。需要修改更多字段时使用编辑器。`/workflow`、`/workflows` 等斜杠命令不是 ai-switch 实现的功能，应以自己所用客户端或扩展的帮助为准。
