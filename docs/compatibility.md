# 支持与验证范围

## 操作系统与功能

1.10.0 使用同一套核心逻辑和 profile 格式，通过系统适配层提供全部现有命令。Linux/macOS/Windows 分别提供源码安装包，仍需 Python 3.10+ 和所选原生客户端。此次模块重构保持版本 1 的状态和基准格式，已有用户无需重复初始化。

| 平台 | profile / 切换 / 基准 | 会话列表、原生续接、Codex 修复 | Codex `--takeover` |
| --- | --- | --- | --- |
| Linux | 支持 | 支持 | `/proc` 核实写锁，pidfd 固定目标后发送 SIGTERM；需要支持 pidfd 的内核/Python |
| macOS | 支持 | 支持 | 系统 lsof、文件锁及进程身份检查，向核实的单个进程发送 SIGTERM |
| Windows 原生 | 支持 | 支持 | LockFileEx、Restart Manager、进程身份检查及固定进程句柄，终止单个目标 |
| WSL2 | 使用 Linux 实现 | 客户端及历史须位于 WSL 内 | 依赖其 Linux 内核接口；未单独实测 WSL2 |

普通续接不会结束旧进程。接管必须显式使用 `--takeover`；可先 `--dry-run`。遇到多个文件使用者、后台服务、检测到的多会话进程、权限不足或身份变化时，接管会停止。Windows 的 TerminateProcess 不等于客户端正常退出，尚未持久化的工作可能丢失。macOS 使用创建时间复核 PID，不具有 Linux pidfd 同等的内核身份绑定能力。

Windows 文件使用受保护 DACL，允许当前用户、SYSTEM、管理员访问。安装生成原生 `ai-switch.exe`。原生 `.exe` 或标准 npm 客户端受支持；npm 入口根据已安装包的 `bin` 字段解析为原生程序或 Node 脚本，未知批处理包装器会被拒绝。Claude 的命令 hook 使用其要求的 Git Bash，不能用 CMD 的引用规则替代。

默认目录仍是用户主目录下 `.config/ai-switch`、`.codex` 和 `.claude`，保留旧版状态。跨机器拷贝完整基准中的绝对路径和凭据不是本工具的迁移功能；应在各机器分别初始化。

## 原生客户端版本

| 客户端 | 版本 | 验证重点 |
| --- | --- | --- |
| Codex CLI | 0.156.0 | 新会话、同 UUID 多次切换续接、路由和历史保留；真实 CLI 写锁、接管、接管后续接 |
| Claude Code | 2.1.258 | 新会话、同 UUID 双向续接、Messages 路由、模型及 Micu 环境清理 |
| Codex CLI | 0.155.1 | 此前 Linux 版本验证记录；本次没有重新安装该旧版本 |

这是已验证客户端版本清单，不表示所有未来版本永久兼容。Claude 使用原生恢复，不提供 Codex 专用的 `repair-session` 或接管命令；这一客户端功能边界在三个操作系统相同。

## 自动化验证

工作流 `.github/workflows/ci.yml` 包含 Ubuntu、macOS、Windows × Python 3.10/3.12 六组单元测试及安装验证，另外有三个平台的原生客户端集成作业。2026-09-24 的 [九个 CI 作业全部通过](https://github.com/S1mpleCheeq/ai-switch/actions/runs/35970765128)：六组单元/安装测试，以及三个平台的真实原生客户端集成与接管测试。最终发布提交的状态可在 [GitHub Actions](https://github.com/S1mpleCheeq/ai-switch/actions) 查看。

1.10.0 的 153 项测试保留 1.9.1 的全部 152 项用例，并增加原命令树的参数、默认值与选项兼容检查。Linux 接管测试在 Linux 执行；macOS/Windows 原生锁及接管测试在相应 runner 执行，其他平台跳过。测试覆盖配置事务回滚、profile CRUD、基准和资源校验、历史修复、Windows ACL、跨进程锁、真实子进程接管、参数传递、含空格/中文路径、安装入口与发布包边界。启动器预检查、异常锁拒绝、旧 hook 清理和 PATH 中不同 Python 的回归用例继续保留。

同一套测试分别对源码和已安装 wheel 执行。安装模式从临时工作目录运行，并确认导入安装后的包，防止源码目录掩盖漏装模块或资源的问题。不能把 Linux 上模拟平台分支等同于其他系统上的执行。

原生集成测试固定安装 Codex 0.156.0、Claude Code 2.1.258，使用临时 HOME/CODEX_HOME、合成模型目录、假密钥和本地模拟 Responses/Messages API。两客户端均执行新会话及四次跨 profile 续接；另让真实 Codex 进程持锁等待本地 API，验证接管预览、结束旧进程、写锁释放以及同 UUID 再次续接保留前文。

集成测试不接触用户的配置、进程或会话，不请求真实服务商。不证明服务商的额度、计费、上游可用性或任意长历史兼容。验证范围为 CI 使用的操作系统与架构，不承诺所有旧系统、文件系统或 CPU 组合。

## 教程扩展与服务端边界

Claude 的 `ultracode`、`enableWorkflows`、Fable 模型别名等来自参考教程。工具会写入这些字段，但不能让不识别它们的原生客户端获得对应功能。Read/View hook 负责读取纠偏，不修改上下文窗口或缓存。`/workflow` 与 `/workflows` 不是本工具注册的命令。

模型目录、证书信任、服务端认证与协议兼容由使用者及服务商确认。`check --network` 只检查模型列表接口，不是完整聊天测试；历史兼容修复不能保证解决上游断流、超时或所有未来历史格式。
