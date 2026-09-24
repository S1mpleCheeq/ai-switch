# 本地准备与发布

本仓库按 MIT 许可分发；安装时带入的 tomlkit、psutil 和 Windows 启动器依赖 distlib 保留其单独许可证。原生 Codex、Claude Code 及第三方模型目录不包含在本源码包中。

## 验证与打包

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest -q
.venv/bin/python release.py
```

标准 Python wheel 可用 `.venv/bin/python -m pip wheel --no-deps -w dist/wheel .` 构建；版本从 `src/ai_switch/_version.py` 读取。wheel 包含运行代码和公开资源，依赖由安装环境提供。开发时可用 `.venv/bin/python -m pip install -e .`，源码与已安装包测试的区别见兼容性文档。

打包工具只接受显式发布文件清单：源码、测试、公开模板、文档和许可。缺文件、符号链接或命中常见密钥形式时会失败。输出通用源码包以及 Linux/macOS `.tar.gz`、Windows `.zip` 三个平台源码安装包，每个附带 `.sha256`。平台包来自相同白名单，不包含其他系统机器上的私人文件。不要手动把整个工作目录或 HOME 打包。

原生客户端集成验证是可选步骤，需要自行安装已支持版本的 CLI，默认自动生成合成测试模型目录，也可显式提供有效目录。测试使用临时配置、假密钥和本地模拟 API，不应使用真实服务凭据：

```bash
.venv/bin/python -m pip install zstandard==0.23.0
.venv/bin/python tests/integration/native_clients.py
```

## 发布到 GitHub

1. 审核 `git status` 和 `git diff --cached`；确认没有用户配置、会话、真实密钥或个人诊断记录。
2. 将此独立项目仓库推送到自己的 GitHub 仓库，不要把包含其他工作项目的父目录作为仓库根。
3. 为经过测试的提交创建版本 tag，再创建对应 Release，上传源码包和 SHA256 文件，描述支持平台、已验证客户端版本及已知限制。

GitHub 也会按 tag 提供自动源码压缩包。远程地址、仓库可见性和正式发布由发布者决定；本工具不会自动创建远端仓库或上传。[GitHub Release 说明](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)

## 版本记录

### 1.10.0

- 整理为 `src/ai_switch` Python 包；CLI、profile、基准、事务存储、初始化、会话和启动分别由独立模块负责。
- `Manager` 保留原有方法入口，服务通过组合协作；Claude JSON/hook 与 Codex TOML 的配置转换、专属校验移入客户端适配模块。
- 明确平台进程分发与 Linux、macOS/Windows 后端，统一错误类型，清理不可达的旧平台分支。
- 公开模板和 hook 改用包资源读取，测试移入 `tests/`，安装和发布使用统一包结构。
- 保持命令参数、版本 1 状态格式、固定基准、事务回滚、会话修复和显式接管行为；既有安装升级无需重新初始化。
- 原有回归用例全部保留，并以 1.9.1 的命令树快照校验参数、默认值和选项兼容性。设计与维护约束见[架构文档](architecture.md)。

### 1.9.1

本次对三个平台的代码复核发现并修复四个问题：

- Windows npm 启动器原先在接管结束旧进程后才解析；现在在接管及配置切换前检查，无法解析时保留旧进程和配置。
- macOS 的会话锁若异常变为 FIFO，检查原先会卡在打开文件；现在非阻塞打开并拒绝非普通文件，不结束进程。
- Python 安装路径变化后，Claude 的旧 Read/View hook 原先可能无法清除；现在按工具自有脚本和限定参数识别，切回 Micu 时清理旧 hook，并保留其他 hook。
- Linux/macOS 安装入口原先依赖 PATH 中的 `python3`；现在固定安装时的基础解释器，避免终端选到不同版本。

增加对应回归用例及安装入口验证；测试集合为 152 项，平台专属用例按系统执行或跳过。原生客户端验证范围和 CI 入口见[兼容性文档](compatibility.md)。升级不会重写现有 profile、固定基准或会话。

### 1.9.0

- 支持 Linux、macOS、Windows 原生配置管理、基准、会话列表、续接与 Codex 兼容修复。
- 提供各平台 Codex 接管后端，核实文件占用者、进程身份和会话范围；Windows 明确采用单进程终止语义。
- Windows 使用私密 DACL、LockFileEx、原生 `.exe` 入口及 npm 客户端解析；统一 UTF-8 并处理带空格路径。
- 发布三个平台源码安装包和通用包，保留相同命令与旧版状态格式。
- GitHub Actions 在 Ubuntu、macOS、Windows 及 Python 3.10/3.12 上验证；详细结果与范围见兼容性文档。


### 1.8.1

- 将公开 Codex Aster 模板的 provider `name` 恢复为教程和固定基准中的 `OpenAI`。
- 明确模板与 PDF 示例、完整本机配置、公共权限/存储字段及 hook 实现之间的差异。
- 现有用户 profile 和固定基准保持不变；本次修正影响内置模板和今后的初始化。

### 1.8.0

- 发布源码与用户私密配置分离，公开 Aster 模板与 MIT 许可证。
- 初始化可选择 Codex、Claude 或两者，支持隐藏输入 AsterGate key。
- 未初始化也可查看 Aster 模板，实际 profile 拒绝未替换的占位符。
- 明确 Linux、macOS、WSL2、Windows 原生的支持边界；接管限定 Linux。
- 保留旧双客户端状态、profile 管理、固定基准、历史兼容修复与显式接管用法。
