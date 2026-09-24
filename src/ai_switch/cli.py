"""Command-line parsing, dispatch and user-facing output."""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from .config import APPS, SwitchError, selected
from .platforms import processes as session_process
from .sessions import repair as session_repair
from . import templates as template_profiles
from ._version import VERSION
from .manager import Manager


class HelpParser(argparse.ArgumentParser):
    """Use the installed command name and Chinese help at every command level."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self._positionals.title = "位置参数"
        self._optionals.title = "选项"
        self.add_argument("-h", "--help", action="help", help="显示本页帮助并退出")

    def format_help(self):
        return super().format_help().replace("usage: ", "用法：", 1)


def parser():
    ap = HelpParser(
        prog="ai-switch",
        description="统一管理 Claude Code 和 Codex 的多套接入配置，支持切换及续接已有会话。\n"
        "切换对当前用户全局生效，不限当前目录；新启动的客户端生效。\n"
        "本页列出全部一级命令和常用示例；完整参数见 ai-switch 命令 --help。\n"
        "支持 Linux、macOS 和原生 Windows；平台包共享命令和 profile 格式。",
        epilog="""常用命令：
  ai-switch status                          查看当前使用的配置
  ai-switch profile list                    列出全部配置
  ai-switch profile template aster --app codex
                                             查看内置参考模板，无需初始化或密钥
  ai-switch use aster                       已初始化客户端切到 AsterGate
  ai-switch use micu --app codex             只把 Codex 切回 Micu
  ai-switch baseline list                   列出并校验所有固定基准
  ai-switch baseline restore micu           恢复原始 Micu 通道设置
  ai-switch baseline restore aster          恢复固定的 AsterGate 通道设置
  ai-switch repair-session UUID --dry-run   检查记录 ID 和分页兼容性，预览修复

新增、修改、使用、删除（backup 是你给新配置起的示例名称）：
  ai-switch profile add backup --from micu   先复制已有配置，创建 backup
  ai-switch profile edit backup --app codex  再用编辑器修改 Codex 配置
  ai-switch profile edit backup --app claude 再用编辑器修改 Claude 配置
  ai-switch use backup                      启用新配置
  ai-switch use micu                         删除前先切到其他配置
  ai-switch profile delete backup           删除 backup，保留恢复备份

查看会话并续接：
  ai-switch sessions --app codex --limit 100  列出跨通道的会话；不会打开会话
  ai-switch run codex --mode aster --session UUID
                                             自动检查 Aster 历史兼容性后续接
  ai-switch run codex --session UUID --takeover --dry-run
                                             预览旧进程接管，不发送退出信号
  ai-switch run codex --session UUID --takeover
                                             结束持锁旧进程，释放后再续接
  接管最多等待 15 秒：Linux/macOS 发 SIGTERM；Windows 结束核实过的单个进程。未完成请求可能中断。

其他常用选项（完整说明见对应子命令 --help）：
  run --no-auto-repair                      跳过修复及副本复用，直接续接指定 UUID
  sessions --include-subagents              会话列表也显示子代理和 guardian
  use --discard-changes                    备份未保存改动，再应用目标 profile
  baseline restore --full-config            连同基准里的公共设置一起恢复
  baseline protect PROFILE                  一次性固定自定义配置，不切换通道

详细参数与示例：
  ai-switch 命令 --help                     查看任意一级命令的完整参数
  ai-switch profile 操作 --help             list / show / template / add / edit / delete
  ai-switch baseline 操作 --help            list / status / protect / restore
  ai-switch use --help
  ai-switch baseline restore --help
  ai-switch profile edit --help
  ai-switch sessions --help
  ai-switch repair-session --help
  ai-switch run --help
""",
    )
    ap.add_argument(
        "--version", action="version", version=VERSION, help="显示版本并退出"
    )
    ap.add_argument(
        "--state-dir", metavar="目录", help="指定配置管理目录；默认 ~/.config/ai-switch"
    )
    sp = ap.add_subparsers(
        dest="command", required=True, title="子命令", metavar="命令"
    )
    p = sp.add_parser(
        "init",
        help="首次初始化：选择客户端并固定 Micu / AsterGate 基准",
        description="仅在首次安装后运行；所选客户端须已正常使用自己的 Micu 配置。\n"
        "--app codex 或 claude 只管理该客户端；默认 all 管理两者。\n"
        "从内置公开模板生成 Aster 配置，使用自己的密钥、CA 和模型目录。\n"
        "已有配置时不要重复 init；新增配置请用 profile add。",
        epilog="示例：\n  ai-switch init --app codex --catalog /path/to/model-catalog.json --ca /path/to/ca.crt --ask-api-key\n"
        "  ai-switch init --app claude --ca /path/to/ca.crt --ask-api-key\n"
        "  ai-switch init --catalog /path/to/model-catalog.json --ca /path/to/ca.crt\n\n"
        "默认从 ASTERGATE_API_KEY 读取凭据；--ask-api-key 需要交互终端。\n"
        "完整配置步骤见随包 docs/setup.md。",
    )
    p.add_argument(
        "--app",
        choices=(*APPS, "all"),
        default="all",
        help="初始化哪个客户端；默认 all（两者），不会触碰未选择的客户端",
    )
    p.add_argument(
        "--claude-dir",
        default=str(Path.home() / ".claude"),
        help="Claude 配置目录；默认 ~/.claude",
    )
    p.add_argument(
        "--codex-dir",
        default=str(Path.home() / ".codex"),
        help="Codex 配置目录；默认 ~/.codex",
    )
    p.add_argument(
        "--catalog",
        help="教程提供的完整模型目录 JSON；初始化 Codex 时必填，仅 Claude 可省略",
    )
    p.add_argument("--ca", required=True, help="AsterGate CA 证书文件")
    credentials = p.add_mutually_exclusive_group()
    credentials.add_argument(
        "--aster-key-env",
        default="ASTERGATE_API_KEY",
        help="存放自己的 AsterGate 密钥的环境变量名；默认 ASTERGATE_API_KEY",
    )
    credentials.add_argument(
        "--ask-api-key",
        action="store_true",
        help="在交互终端隐藏输入自己的 AsterGate API key，不放在命令参数中",
    )

    p = sp.add_parser(
        "use",
        help="切换到已有配置；默认切换全部已初始化客户端",
        description="切换当前用户的全局配置；已运行进程需退出后重新启动或续接。\n"
        "PROFILE 必须已存在，可用 ai-switch profile list 查看。\n"
        "Codex 仅推理强度变化时会先备份再切换；其他未保存变更默认会阻止覆盖。",
        epilog="示例：\n  ai-switch use aster\n  ai-switch use micu --app codex\n  ai-switch use aster --dry-run\n"
        "  ai-switch use micu --discard-changes\n\n"
        "--discard-changes 将当前变更留在备份中，不写回原 profile，然后应用目标 profile。\n"
        "它使用当前保存的 profile；要恢复固定基准，使用 baseline restore PROFILE。",
    )
    p.add_argument(
        "mode", metavar="PROFILE", help="已有配置名称，如 micu、aster 或你创建的 backup"
    )
    p.add_argument(
        "--app",
        choices=(*APPS, "all"),
        default="all",
        help="要切换的客户端；默认 all（所有已初始化客户端）",
    )
    p.add_argument("--dry-run", action="store_true", help="只预览切换计划，不修改配置")
    p.add_argument(
        "--discard-changes",
        action="store_true",
        help="备份未保存变更并按目标配置切换；不 capture 到原 profile",
    )
    p = sp.add_parser(
        "baseline",
        help="固定、检查或恢复 Micu、AsterGate 及自定义配置基准",
        description="固定基准独立于可编辑的 profile；edit/capture/delete 均不会改写它。\n"
        "恢复前先校验并备份当前文件；不会修改会话历史。\n"
        "省略 PROFILE 时默认为 micu，兼容原命令。",
        epilog="示例：\n  ai-switch baseline list\n  ai-switch baseline status aster\n"
        "  ai-switch baseline restore micu\n  ai-switch baseline restore aster --dry-run\n"
        "  ai-switch baseline protect backup",
    )
    actions = p.add_subparsers(
        dest="baseline_command", required=True, title="基准命令", metavar="操作"
    )
    actions.add_parser(
        "list",
        help="列出并校验所有已固定基准",
        description="显示基准来源和主模型，不输出凭据。",
    )
    p = actions.add_parser(
        "status",
        help="校验指定基准的配置、凭据和资源快照",
        description="校验指定基准的哈希值，不输出凭据；省略 PROFILE 时检查 micu。",
    )
    p.add_argument(
        "name",
        nargs="?",
        default="micu",
        metavar="PROFILE",
        help="固定基准名称；默认 micu",
    )
    p = actions.add_parser(
        "protect",
        help="一次性固定配置；已固定则只校验",
        description="Micu 固定初始化时的原始快照；其他名称固定当前保存的 profile、引用资源及公共设置。\n"
        "不会 capture 未保存改动，也不会切换当前配置。固定后不可覆盖。\n"
        "新版 init 自动固定 micu 和 aster。",
        epilog="示例：\n  ai-switch baseline protect backup\n"
        "  ai-switch baseline protect aster --from-backup ~/.config/ai-switch/backups/备份目录",
    )
    p.add_argument(
        "name",
        nargs="?",
        default="micu",
        metavar="PROFILE",
        help="要固定的配置；默认 micu",
    )
    p.add_argument(
        "--from-backup",
        help="从本工具的备份目录提取两客户端 profile；用于固定修改前的配置（不适用于 micu）",
    )
    p = actions.add_parser(
        "restore",
        help="恢复指定基准，并重置、启用对应 profile",
        description="默认恢复通道、模型、凭据、专属设置及引用资源，保留当前 MCP、权限等公共设置。\n"
        "--full-config 也恢复该基准保存的公共设置；Micu 还恢复存在的原始 Codex 登录快照。\n"
        "恢复会先备份当前文件，不需要先 capture，也不会修改固定基准或会话历史。",
        epilog="示例：\n  ai-switch baseline restore --dry-run\n"
        "  ai-switch baseline restore micu\n  ai-switch baseline restore aster --app codex\n"
        "  ai-switch baseline restore aster --full-config",
    )
    p.add_argument(
        "name",
        nargs="?",
        default="micu",
        metavar="PROFILE",
        help="要恢复的固定基准；默认 micu",
    )
    p.add_argument(
        "--app",
        choices=(*APPS, "all"),
        default="all",
        help="恢复哪个客户端；默认 all（所有已初始化客户端）",
    )
    p.add_argument(
        "--full-config",
        action="store_true",
        help="整份恢复：包括该基准保存的公共设置；Micu 包含原始登录快照",
    )
    p.add_argument("--dry-run", action="store_true", help="只显示恢复范围，不修改文件")
    p = sp.add_parser(
        "capture",
        help="保存当前配置的手动修改；不新增配置",
        description="将客户端当前的模型、凭据等通道设置保存到正在使用的 profile。\n"
        "仍须属于同一通道；所有固定基准保留。",
        epilog="示例：\n  ai-switch capture --app codex\n  ai-switch capture --app claude",
    )
    p.add_argument(
        "--app",
        choices=(*APPS, "all"),
        default="all",
        help="要保存的客户端；默认 all（所有已初始化客户端）",
    )
    p = sp.add_parser(
        "status",
        help="查看当前通道、模型及配置是否一致",
        description="显示已初始化客户端正在使用的配置，以及是否被外部工具修改。",
        epilog="示例：\n  ai-switch status\n  ai-switch status --json",
    )
    p.add_argument("--json", action="store_true", help="以 JSON 输出状态")
    p = sp.add_parser(
        "check",
        help="检查配置、凭据与客户端；可选联网检查",
        description="默认只检查本机配置、资源及凭据引用；--network 会检查各 profile 的模型列表接口。",
        epilog="示例：\n  ai-switch check\n  ai-switch check --network",
    )
    p.add_argument(
        "--network", action="store_true", help="联网检查模型列表和认证；不发送聊天请求"
    )
    sp.add_parser(
        "recover",
        help="恢复因中断而未完成的操作",
        description="存在未完成的切换或配置管理操作时，从恢复日志还原操作前的文件。\n"
        "没有待恢复操作时不会修改配置。",
        epilog="示例：\n  ai-switch recover",
    )
    p = sp.add_parser(
        "sessions",
        help="列出跨通道的历史会话，不打开会话",
        description="输出文本列表，含会话 UUID 和工作目录；默认显示最近 20 条。\n"
        "Codex 列表跨 provider，只显示未归档会话，默认隐藏 subagent 和 guardian。\n"
        "过滤后再取 --limit 条；隐藏不删除会话，也不影响通过明确 UUID 续接。",
        epilog="示例：\n  ai-switch sessions --app codex --limit 100\n  ai-switch sessions --app claude\n"
        "  ai-switch sessions --app codex --include-subagents\n"
        "  ai-switch run codex --mode micu --session UUID",
    )
    p.add_argument("--app", choices=APPS, required=True, help="查看哪个客户端的会话")
    p.add_argument(
        "--limit", type=int, default=20, metavar="数量", help="最多列出多少条；默认 20"
    )
    p.add_argument(
        "--include-subagents",
        action="store_true",
        help="同时显示子代理及 guardian 会话，标记为 [subagent]",
    )
    p = sp.add_parser(
        "repair-session",
        help="修复 Codex 历史 ID / 新版回退兼容问题，保留原会话",
        description="处理旧会话中不兼容的响应记录 ID，适配 Codex 0.156+ 分页回退。\n"
        "保留完整历史、正文、推理、工具参数及结果，不修改原会话或祖先文件。\n"
        "Codex 0.156+ 会把 legacy 历史迁移到新的分页副本 UUID，支持回退编辑。\n"
        "旧版 Codex 的已有 legacy 副本仍可原 UUID 补全界面消息显示。\n"
        "同时识别已知修复副本的后续分支重新生成的响应 ID；不保证解决所有断流。\n"
        "不会切换配置或发送模型请求；新 UUID 可在 Micu/Aster 之间续接。",
        epilog="示例：\n  ai-switch repair-session UUID --dry-run\n"
        "  ai-switch repair-session UUID\n"
        "  ai-switch run codex --mode aster --session 新UUID\n\n"
        "新的副本会出现在 sessions 列表；请退出目标会话后操作，避免历史继续变化。",
    )
    p.add_argument("session", metavar="UUID", help="需要修复的 Codex 会话 ID")
    p.add_argument(
        "--app", choices=("codex",), default="codex", help="当前仅支持 codex（默认）"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="检查历史并预览，不创建或更新会话"
    )
    p = sp.add_parser(
        "run",
        help="启动或续接会话；--takeover 可接管被旧 Codex 进程占用的会话",
        description="默认按当前配置启动新会话；--session UUID 续接已有会话。\n"
        "Codex 接入教程中的 AsterGate 时，自动检查记录 ID 及新版分页历史兼容问题。\n"
        "有问题则保留原会话并创建兼容副本；原历史未变时复用已有副本及其后续对话。\n"
        "Micu、Claude 和新会话不自动修复；原生客户端内部 fork 需下次启动时检查。\n"
        "Codex 会话被占用时默认停止；--takeover 仅终止实际续接 UUID 的独立旧进程。\n"
        "等待退出和释放写锁最多 15 秒；Linux/macOS 发 SIGTERM，Windows 终止单个进程。后台服务或多会话进程拒绝接管。\n"
        "--mode 会先全局切换该客户端的配置，再启动；不是仅对本次启动生效。",
        epilog="示例：\n  ai-switch run codex\n  ai-switch run claude --mode aster\n"
        "  ai-switch run codex --mode micu --session UUID\n"
        "  ai-switch run codex --session UUID --dry-run\n"
        "  ai-switch run codex --session UUID --takeover --dry-run\n"
        "  ai-switch run codex --session UUID --takeover\n"
        "  ai-switch run codex --mode aster --session UUID --no-auto-repair\n"
        "  ai-switch run codex -- --no-alt-screen\n\n"
        "UUID 请从 sessions 列表复制；-- 后可透传不覆盖通道、模型或会话的客户端参数。",
    )
    p.add_argument("app", choices=APPS, help="要启动的客户端")
    p.add_argument(
        "--mode", metavar="PROFILE", help="先切换到指定配置；省略则使用当前配置"
    )
    p.add_argument("--session", metavar="UUID", help="要续接的会话 ID；省略则新开会话")
    p.add_argument(
        "--cwd",
        metavar="目录",
        help="工作目录；续接默认使用原会话目录，新会话默认当前目录",
    )
    p.add_argument(
        "--no-auto-repair",
        action="store_true",
        help="跳过 Codex/Aster 启动前修复及副本复用，直接续接指定 UUID",
    )
    p.add_argument(
        "--takeover",
        action="store_true",
        help="Codex + UUID：核实并结束持锁旧进程，释放写锁后续接；Windows 使用进程终止，未保存工作可能中断",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="预览接管、切换、自动修复和启动命令，不发送信号或写入会话/配置",
    )

    p = sp.add_parser(
        "profile",
        help="管理配置及公开模板：列出、查看、新增、修改、删除",
        description="每个 profile 包含已初始化客户端的配置，可分别修改和启用。\n"
        "template 查看内置无凭据参考，不需要初始化。\n"
        "edit 只修改已有配置；要新增名称，先运行 add。",
        epilog="示例：\n  ai-switch profile list\n  ai-switch profile show micu\n"
        "  ai-switch profile add backup --from micu\n"
        "  ai-switch profile add aster2 --from aster\n"
        "  ai-switch profile edit backup --app codex\n"
        "  ai-switch use backup\n  ai-switch use micu\n"
        "  ai-switch profile delete backup\n\n"
        "修改地址、模型、密钥等参数：ai-switch profile edit --help",
    )
    actions = p.add_subparsers(
        dest="profile_command", required=True, title="配置管理命令", metavar="操作"
    )
    p = actions.add_parser(
        "template",
        help="查看内置 Aster 公开参考模板；无需初始化，不读取用户配置",
        description="输出含凭据/路径占位符的公开模板，不包含作者或当前用户的密钥。\n"
        "使用 init 填入自己的凭据与资源，不能直接使用未替换的占位符。",
        epilog="示例：\n  ai-switch profile template aster --app codex\n  ai-switch profile template aster --app claude",
    )
    p.add_argument("name", choices=("aster",), help="内置模板名称；当前提供 aster")
    p.add_argument(
        "--app",
        choices=(*APPS, "all"),
        default="all",
        help="查看哪个客户端的模板；默认 all",
    )
    p = actions.add_parser(
        "list",
        help="列出全部配置；* 标记正在使用的客户端",
        description="列出已有配置名称和主模型，* 分别标记 Claude / Codex 当前使用的配置。",
        epilog="示例：\n  ai-switch profile list\n  ai-switch profile list --json",
    )
    p.add_argument("--json", action="store_true", help="以 JSON 输出列表")
    p = actions.add_parser(
        "show",
        help="查看已有配置的内容，密钥脱敏",
        description="查看保存的 profile，不修改配置；密钥显示为 <redacted>。",
        epilog="示例：\n  ai-switch profile show micu\n  ai-switch profile show aster --app codex",
    )
    p.add_argument("name", metavar="PROFILE", help="已有配置名称")
    p.add_argument(
        "--app",
        choices=(*APPS, "all"),
        default="all",
        help="查看哪个客户端；默认 all（所有已初始化客户端）",
    )
    p = actions.add_parser(
        "add",
        help="从已有配置复制，创建新的独立配置",
        description="复制已初始化客户端的地址、模型、凭据及专属设置；新增后不会自动切换。\n"
        "从 micu 复制普通接入配置，从 aster 复制教程配置；随后用 edit 修改。",
        epilog="示例：\n  ai-switch profile add backup --from micu\n"
        "  ai-switch profile edit backup --app codex\n"
        "  ai-switch profile add aster2 --from aster",
    )
    p.add_argument(
        "name",
        metavar="NEW_PROFILE",
        help="新名称；1～64 位字母、数字、_ 或 -，以字母或数字开头",
    )
    p.add_argument(
        "--from",
        dest="source",
        required=True,
        metavar="PROFILE",
        help="复制来源，须为已有配置，如 micu 或 aster",
    )
    p = actions.add_parser(
        "delete",
        help="删除未使用的配置，保留历史与恢复备份",
        description="任一客户端正在使用该配置时拒绝删除；请先切换到其他配置。\n"
        "删除 profile 不删除会话、恢复备份或任何已固定基准。",
        epilog="示例：\n  ai-switch use micu\n  ai-switch profile delete backup",
    )
    p.add_argument("name", metavar="PROFILE", help="要删除的已有配置名称")
    p = actions.add_parser(
        "edit",
        help="修改已有配置；可用参数，也可打开编辑器",
        description="PROFILE 必须已存在；如需创建 backup，先执行：\n"
        "  ai-switch profile add backup --from micu\n\n"
        "不带修改参数时打开 VISUAL / EDITOR 指定的编辑器，Linux/macOS 默认 vi，Windows 默认记事本。\n"
        "修改当前使用的配置会同步写入客户端设置，新启动生效。",
        epilog="示例：\n"
        "  ai-switch profile edit backup --app codex\n"
        "  ai-switch profile edit backup --app claude\n"
        "  ai-switch profile edit backup --app codex --model gpt-6-astra --effort high\n"
        "  ai-switch profile edit backup --app codex \\\n"
        "    --base-url https://your-gateway.example/v1 --ask-api-key\n"
        "  ai-switch profile edit backup --app claude \\\n"
        "    --base-url https://your-gateway.example --ask-api-key\n"
        "  ai-switch profile edit backup --app codex --api-key-env BACKUP_API_KEY\n"
        "  ai-switch profile edit aster2 --app claude --read-guard on --ultracode on\n\n"
        "--api-key-env 填环境变量名；工具读取并保存其值。--ask-api-key 隐藏输入密钥。\n"
        "--file / --editor 不能与其他修改参数一起使用。",
    )
    p.add_argument("name", metavar="PROFILE", help="要修改的已有配置名称；不会自动新增")
    p.add_argument(
        "--app", choices=APPS, required=True, help="修改该配置中的哪个客户端"
    )
    p.add_argument("--base-url", help="API 基地址；Codex 通常需含 /v1，Claude 通常不含")
    p.add_argument("--model", help="主模型名称或客户端别名")
    p.add_argument("--effort", help="推理强度，如 high / xhigh / ultra（Codex）")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--subagent-model", help="设置默认子代理模型")
    group.add_argument(
        "--clear-subagent",
        action="store_true",
        help="移除默认子代理设置；不修改 Claude 的 Haiku 别名",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--api-key-env",
        metavar="ENV_VAR",
        help="读取并私密保存该环境变量的密钥值；参数填变量名",
    )
    group.add_argument(
        "--ask-api-key", action="store_true", help="在终端隐藏输入 API key"
    )
    p.add_argument(
        "--read-guard",
        choices=("on", "off"),
        help="启用或关闭 Gemini Read hook；仅 Claude",
    )
    p.add_argument(
        "--ultracode",
        choices=("on", "off"),
        help="启用或关闭 ultracode 模式；仅 Claude",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--catalog", help="指定模型目录 JSON 文件；仅 Codex")
    group.add_argument(
        "--clear-catalog",
        action="store_true",
        help="移除自定义目录，恢复客户端原生模型目录；仅 Codex",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--file",
        help="从 JSON 文件替换该客户端的完整 profile；不能导入 show 的脱敏输出",
    )
    group.add_argument(
        "--editor", action="store_true", help="在私密临时文件中编辑完整 profile"
    )
    return ap


def main(argv=None):
    ap = parser()
    args, tail = ap.parse_known_args(argv)
    if tail and args.command != "run":
        ap.error("不支持参数：" + " ".join(tail))
    args.client_args = tail
    if args.command == "profile" and args.profile_command == "template":
        selected = APPS if args.app == "all" else (args.app,)
        content = {app: template_profiles.template(app) for app in selected}
        print(
            json.dumps(
                content if args.app == "all" else content[args.app],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    manager = Manager(args.state_dir)
    try:
        if args.command == "init":
            manager.init(args)
        elif args.command == "use":
            manager.use(args.mode, args.app, args.dry_run, args.discard_changes)
        elif args.command == "status":
            report = manager.report()
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                for app, d in report["apps"].items():
                    print(
                        f"{app}: {d['mode']} | {d['model']} | 子代理 {d['subagent']} | {d['base_url']} | "
                        + ("配置一致" if d["matches_profile"] else "配置已变更")
                    )
                    if d["changed_fields"]:
                        print(
                            "  变化字段："
                            + ", ".join(d["changed_fields"])
                            + (
                                "（仅推理强度；切换时自动备份并恢复目标设置）"
                                if d["effort_only"]
                                else ""
                            )
                        )
        elif args.command == "check":
            manager.check(args.network)
        elif args.command == "recover":
            manager.recover()
        elif args.command == "baseline":
            if args.baseline_command == "list":
                manager.list_baselines()
            elif args.baseline_command == "status":
                manager.baseline_status(args.name)
            elif args.baseline_command == "protect":
                manager.protect_baseline(args.name, args.from_backup)
            elif args.baseline_command == "restore":
                manager.restore_baseline(
                    args.name, args.app, args.full_config, args.dry_run
                )
        elif args.command == "capture":
            manager.capture_current(args.app)
        elif args.command == "sessions":
            for item in manager.sessions(args.app, args.limit, args.include_subagents):
                marker = " [subagent]" if item.get("subagent") else ""
                print(
                    f"{item['id']}  {item['provider']}{marker}  {item['cwd']}  {item['name'][:60]}"
                )
        elif args.command == "repair-session":
            manager.repair_session(args.session, args.app, args.dry_run)
        elif args.command == "run":
            return manager.launch(args) or 0
        elif args.command == "profile":
            if args.profile_command == "list":
                rows = manager.list_profiles()
                if args.json:
                    print(json.dumps(rows, ensure_ascii=False, indent=2))
                else:
                    for row in rows:
                        clients = " | ".join(
                            f"{app}{'*' if data['active'] else ''}: {data['model']}"
                            for app, data in row["apps"].items()
                        )
                        print(f"{row['name']} | {clients}")
                    print("* 表示该客户端当前使用的配置")
            elif args.profile_command == "show":
                print(
                    json.dumps(
                        manager.show_profile(args.name, args.app),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif args.profile_command == "add":
                manager.add_profile(args.name, args.source)
            elif args.profile_command == "delete":
                manager.delete_profile(args.name)
            elif args.profile_command == "edit":
                manager.edit_profile(args)
    except (
        SwitchError,
        session_repair.RepairError,
        session_process.TakeoverError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
    ) as exc:
        # JSON/TOML parser exceptions may contain secrets from the source; don't echo them.
        print(
            "ai-switch: "
            + (
                str(exc)
                if isinstance(
                    exc,
                    (
                        SwitchError,
                        session_repair.RepairError,
                        session_process.TakeoverError,
                    ),
                )
                else f"{type(exc).__name__}；操作未完成，请检查文件格式与权限。"
            ),
            file=sys.stderr,
        )
        return 1
    return 0
