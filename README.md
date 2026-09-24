# ai-switch

本地管理 Claude Code 和 Codex 的多套 API 接入配置。切换通道、恢复固定基准、列出跨通道会话，并调用原生客户端续接。工具不提供 API 服务，也不附带可用的账户或密钥。

发布版：**1.9.0** · Python **3.10+** · [MIT](LICENSE)

## 平台与客户端

| 环境 | 支持范围 | 验证状态 |
| --- | --- | --- |
| Linux | 配置管理、会话列表/续接、Codex 修复和 `--takeover` | 原生平台测试 |
| macOS | 同一套完整命令；接管用文件占用检查和 SIGTERM | GitHub macOS runner 验证 |
| Windows 原生 | 同一套完整命令；原生 `.exe` 入口、ACL 和文件锁 | GitHub Windows runner 验证 |
| WSL2 | 在 WSL 内安装客户端和工具，走 Linux 实现 | WSL2 本身未单独实机验证 |

三个平台使用相同版本和 profile 格式，提供 `-linux.tar.gz`、`-macos.tar.gz`、`-windows.zip` 三种源码安装包。它们仍需要 Python 和所选原生客户端，不是内置账户的一键运行程序。Windows 与 WSL 使用各自的配置和进程，不应混用。

Codex 的验证版本为 **0.155.1、0.156.0**，Claude Code 为 **2.1.258**。这是已验证版本清单，不表示所有中间或未来版本已测试。Claude 支持独立配置切换和原生续接；Codex 另有针对特定历史格式的兼容修复。详见[支持与验证范围](docs/compatibility.md)。

## 安装

下载本项目源码后，在解压目录内执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python install.py
export PATH="$HOME/bin:$PATH"
ai-switch --version
```

Windows PowerShell 安装（无需激活虚拟环境）：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe install.py
$env:Path = "$HOME\bin;$env:Path"
ai-switch --version
```

Windows 安装生成原生 `ai-switch.exe`，可从 PowerShell 或 CMD 使用；Linux/macOS 使用 `ai-switch` 脚本入口。首次安装后将用户 `bin` 目录加入 PATH。默认数据位置仍为用户主目录下的 `.config/ai-switch`，保留旧版兼容。

安装程序仅复制工具和依赖，不初始化、不切换配置。常用终端的启动文件中可加入 `export PATH="$HOME/bin:$PATH"`，让后续终端也找到命令。原生 `codex` / `claude` 需自行安装，只安装所选客户端即可。

## 首次配置

先让所选客户端按服务商文档正常连接自己的 Micu 账户，再由 ai-switch 保存其基准。**无需同时使用两个客户端**。教程对应的模型目录和 CA 证书由使用者从可信来源获取；仓库不包含作者的配置、证书信任库或账号。

只管理 Codex：

```bash
ai-switch init --app codex \
  --catalog /absolute/path/model-catalog.json \
  --ca /absolute/path/astergate-ca.crt \
  --ask-api-key
```

只管理 Claude Code，无需 Codex 模型目录：

```bash
ai-switch init --app claude \
  --ca /absolute/path/astergate-ca.crt \
  --ask-api-key
```

同时管理两者时用 `--app all`（默认）。输入提示索取的是使用者自己的 AsterGate API key，输入不会回显。初始化保存 Micu 与 Aster 两套配置及基准，当前客户端仍保持 Micu。已有旧版状态可直接继续使用，不要重复 `init`。

模型目录、CA、原生配置示例、环境变量凭据、初始化后新增 profile，以及日后增加另一个客户端的方式，见[完整配置指南](docs/setup.md)。

## 内置 Aster 模板

未初始化也可以查看，不会读取本机 profile：

```bash
ai-switch profile template aster --app codex
ai-switch profile template aster --app claude
```

模板文件位于 [`templates/aster/`](templates/aster/README.md)，只包含服务地址、教程模型及设置。凭据是 `__ASTERGATE_API_KEY__` 占位符；`init` 使用用户输入替换它，并把私密配置写到用户自己的机器。不能直接使用未填充的模板发起请求。

模板保留教程的 Claude `ultracode` 与 Codex `ultra` 设置供参考。这些名称及 Gemini 子代理支持取决于客户端和服务端，**不代表每个任务必须启用，也不保证自动委派**。可以按自己的客户端能力调整 profile。

模板不是教程所有示例或某台机器完整配置的逐字副本；推理强度示例、公共权限/存储设置和 hook 适配范围见[模板与教程的差异说明](templates/aster/README.md#与教程及完整配置的关系)。

## 切换与配置管理

```bash
ai-switch status
ai-switch profile list
ai-switch use aster                 # 切换全部已初始化客户端
ai-switch use micu --app codex      # 只切换 Codex
ai-switch run codex
ai-switch run claude

ai-switch profile add backup --from micu
ai-switch profile edit backup --app codex --base-url https://api.example.com/v1 --ask-api-key
ai-switch profile edit backup --app codex --model YOUR_MODEL --effort high
ai-switch use backup
ai-switch use micu
ai-switch profile delete backup
```

切换是当前用户的全局配置变更，新启动的客户端生效。`run --mode PROFILE` 也会先切换该客户端的全局配置。正在运行的请求不会热迁移。单客户端初始化不会生成、修改或检查另一客户端的配置。

普通切换保留公共 MCP、权限和信任目录等设置；profile 只管理通道及教程相关字段。Claude 默认目录下的用户 MCP 位置与原生客户端一致。修改当前 profile 会同步配置文件，已有进程需重启。

## 会话列表、续接与接管

```bash
ai-switch sessions --app codex --limit 100
ai-switch run codex --mode aster --session UUID
ai-switch run codex --mode micu --session UUID
ai-switch sessions --app claude
ai-switch run claude --mode micu --session UUID
```

列表是文本列表，默认隐藏能识别的 subagent / guardian / sidechain；`--include-subagents` 可显示。两个客户端各用自己的历史，不互相转换会话。Codex 原生选择列表可能按 provider 过滤，跨通道找 UUID 可使用本工具。

终端关闭后若旧 Codex 进程仍占用会话，三个平台都可显式接管：

```bash
ai-switch run codex --session UUID --takeover --dry-run
ai-switch run codex --session UUID --takeover
```

普通启动只报告占用。显式接管核实占用者、账户、进程身份和会话范围后：Linux/macOS 发送 SIGTERM；Windows 使用固定的进程句柄终止单个目标，未保存工作可能丢失。最多等待 15 秒，不追加批量结束或删除锁文件，不结束后台服务或能识别的多会话进程；身份不明确时拒绝操作。续接读取已保存历史。

Aster 的 Codex 续接会检查已知历史 ID 和新版分页格式问题；必要时保留原会话并创建兼容副本，打印实际 UUID。再次启动可复用已有副本及后续对话。需要严格打开指定 UUID 时，加 `--no-auto-repair`。修复不能保证解决服务端断流或超时。独立入口为 `ai-switch repair-session UUID --dry-run`。

## 基准与外部修改

```bash
ai-switch baseline list
ai-switch baseline restore micu --dry-run
ai-switch baseline restore micu
ai-switch baseline restore aster
ai-switch baseline protect backup
ai-switch capture --app codex
ai-switch use micu --discard-changes
```

固定基准独立于可编辑 profile；`edit`、`capture`、`delete` 不改写基准。`baseline restore` 先备份，再重置并启用对应 profile，默认保留当前公共设置。`--full-config` 才整份恢复，Micu 还可恢复当时保存的 Codex 登录文件。

`capture` 保存手动改动到当前 profile。`--discard-changes` 把未保存改动留在备份中，然后应用目标 profile；它不会把已修改的 profile 变回最初版本。Codex 仅推理强度变化时可自动备份后切换。中断的配置事务可用 `ai-switch recover` 恢复。

## 帮助、验证和发布

```bash
ai-switch --help
ai-switch init --help
ai-switch run --help
ai-switch profile edit --help
ai-switch baseline restore --help
ai-switch check                     # 本地检查
ai-switch check --network           # 可选：使用自己的凭据检查模型列表
.venv/bin/python -m unittest -q
.venv/bin/python release.py         # 按文件白名单生成源码发布包
```

本地私密文件存放在 `~/.config/ai-switch/`，包含明文凭据，Linux/macOS 目录权限 0700、文件 0600；Windows 使用仅当前用户、SYSTEM 和管理员可访问的 DACL；不要上传该目录。安装、测试与发布不需要作者的 API key。详见[凭据与发布边界](docs/security.md)及[发布流程](docs/releasing.md)。

项目不隶属于 OpenAI、Anthropic、Micu 或 AsterGate。第三方模型别名、接口、证书与教程扩展可能变化。
