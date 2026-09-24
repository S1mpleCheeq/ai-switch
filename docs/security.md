# 凭据与发布边界

本工具不内置有效 API key。公开 Aster 模板包含占位符，初始化时由使用者填写自己的凭据。

## 本机保存

默认状态目录 `~/.config/ai-switch/` 中的 profile、固定基准、备份和 Claude 运行配置可能包含明文凭据。Linux/macOS 目录权限为 0700，新写文件权限为 0600；Windows 在写入密钥前设置受保护 DACL，只允许当前用户、SYSTEM 和管理员访问。这属于操作系统权限保护，不是加密存储。`--api-key-env` / `--aster-key-env` 会读取并保存值，不只是保存变量名。

`status` 和 `profile show` 隐藏凭据；编辑器中的实际配置有完整凭据。环境变量和进程信息仍受本机账户及管理员权限影响。不要把完整配置、编辑器截图或状态目录发到 issue。

## 可以公开的内容

源码、使用假密钥的测试、清理过的文档、无凭据模板及许可证。公开服务地址和模型别名本身不是认证凭据。公开 CA 证书通常不是私钥，但本项目不自动分发或建立对第三方证书的信任。

## 不进入仓库或发布包的内容

- `~/.config/ai-switch/` 全部内容，包括 baselines、backups、profiles、runtime 和 repairs。
- 原生客户端的 settings/config、auth、数据库、会话 JSONL 和日志。
- `.env`、个人证书私钥、真实访问令牌和带密码的 URL。
- 实际项目的诊断记录、会话 UUID、主机路径及账户信息。

`.gitignore` 防止常见误提交；`release.py` 按白名单打包并扫描常见密钥形式，既不读取用户状态目录，也不包含 private diagnostics。模式扫描不能证明任意内容绝无敏感信息，发布前仍应审阅 Git 暂存区和最终压缩包。GitHub 的自动源码包包含 tag 中的所有已提交文件，不能只依赖自定义打包脚本排除文件。

如果真实密钥曾被推送到远端，应先撤销或轮换，再按需要清理历史；只修改最新文件不会移除旧提交中的凭据。参见 [GitHub 敏感信息清理说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)。

## 平台进程接管

`--takeover` 是用户明确要求结束旧客户端的操作。Linux 用 pidfd 固定进程身份并发送 SIGTERM；macOS 用文件描述符检查及进程创建时间再次核实后发送 SIGTERM；Windows 核实文件使用者并固定进程句柄，再调用 TerminateProcess。Windows 终止不等同于应用正常退出，未持久化的工作可能丢失。

不根据进程名称批量终止，不删除会话锁绕过原生客户端，不结束自身/父进程、后台服务或检测到的多会话进程。无法确定唯一占用者或权限不足时停止。macOS 的 PID 检查不提供 Linux pidfd 的同等内核身份绑定保证。
