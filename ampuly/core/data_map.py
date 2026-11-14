"""每一类数据在哪、归谁、留多久、进不进备份 —— 一份声明，所有人照它办（数据与文件 第 3 期）。

今天这件事被抄了至少四遍，而且已经互相不一致：「真实位置」卡片里一串硬编码、备份的三个根、业务库的七张表、
还原前置说明里的一段常量（其中两行已经是假话——工单和验收卡早就在备份里了）。抄四遍的结果不是冗余，是**谁都
不知道哪一份是真的**。

这份清单是唯一的真相来源，并且把 owner 的裁决写成了类型约束而不是注释：

* **凡是 `user_material`，`retention` 必须是 `forever`。** 用户的订单历史和资料一律永久保留，删不删由用户自己
  决定，系统不自动删除。这一条由测试强制，写错了测试当场红。
* **凡是 `in_backup=False`，必须写明理由。** 「忘了带」和「故意不带」看起来一模一样，只有理由能把它们分开。
* **认不出来的路径不归任何一类。** :func:`claim` 返回 ``None``，而清理规则把 ``None`` 一律当作「保留」。不知道
  的东西永远归用户——这是整套设计里最要紧的一句话。

纯声明与纯函数：不碰磁盘，不读配置，不导入引擎。判断一条路径属于哪一类只看它的形状。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# 一条数据的归属。三个 kind 的含义：
#   user_material  用户的订单历史与资料。永久保留，只有用户自己能删。
#   system_internal 产品自己的东西，可以按 owner 裁决的三族之一自动清理。
#   config / runtime_db / binary  既不是用户材料、也不在三族里：一律保留，只是进不进备份各有说法。
KINDS = ("user_material", "system_internal", "config", "runtime_db", "binary")
RETENTIONS = ("forever", "days_30")
# 一条路径从哪个根算起。把根说清楚，才不会出现「以为是项目内的、其实是全局的」这类错。
ANCHORS = ("home", "workplace_root", "project_workplace", "project_dir")


@dataclass(frozen=True)
class DataKind:
    """一类数据：叫什么、在哪、归谁、留多久、进不进备份。"""

    key: str
    label_zh: str
    anchor: str
    parts: tuple[str, ...]
    kind: str
    retention: str
    in_backup: bool
    reason: str = ""
    # 纯容器：只是「一个装东西的地方」，本身说明不了里面那份文件归谁。容器不参与认领，否则它会把下面
    # 一切都认走，`unclaimed` 就永远是空的——而「产品不认识的东西」正是最值得看一眼的东西。
    container: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"{self.key}: kind {self.kind!r} 不在 {KINDS}")
        if self.retention not in RETENTIONS:
            raise ValueError(f"{self.key}: retention {self.retention!r} 不在 {RETENTIONS}")
        if self.anchor not in ANCHORS:
            raise ValueError(f"{self.key}: anchor {self.anchor!r} 不在 {ANCHORS}")
        # owner 的裁决，写成类型约束：用户材料不可能被标成可清理。
        if self.kind == "user_material" and self.retention != "forever":
            raise ValueError(f"{self.key}: 用户材料必须永久保留")
        if not self.in_backup and not self.reason.strip():
            raise ValueError(f"{self.key}: 不进备份的，必须写明理由")


def _k(key: str, label: str, anchor: str, parts: tuple[str, ...], kind: str, *,
       retention: str = "forever", in_backup: bool = True, reason: str = "", container: bool = False,
       note: str = "") -> DataKind:
    return DataKind(key=key, label_zh=label, anchor=anchor, parts=parts, kind=kind, retention=retention,
                    in_backup=in_backup, reason=reason, container=container, note=note)


# ``{pid}`` 由 :func:`path_of` 代入。顺序从具体到笼统：:func:`claim` 取最具体的那一条。
DATA_MAP: tuple[DataKind, ...] = (
    # ── 用户的订单历史与资料：永久保留，只有用户自己能删 ──────────────────────
    _k("project_workplace", "这个项目的文件（订单、交付物、过程稿）", "project_workplace", (), "user_material",
       note="「资料」入口显示的就是这里"),
    _k("attachments", "你上传的附件", "project_dir", ("attachments",), "user_material"),
    _k("project_memory", "这个项目记住的事", "home", ("memory", "projects", "{pid}.md"), "user_material"),
    _k("customer_memory", "按客户分开记住的事", "home", ("memory", "projects", "{pid}", "customers"), "user_material",
       note="按客户分开存；以前从来不进备份"),
    _k("customer_service", "这个项目的客服历史（准入、待发与渠道选择）", "home",
       ("customer_service", "{pid}"), "user_material",
       note="历史事实进备份并会放回去；发送权、账号租约和未完成的外发不自动恢复"),
    _k("project_backups", "项目备份包", "home", ("backups", "projects"), "user_material",
       in_backup=False, reason="备份不会把自己也装进去，不然每备一次体积就翻一倍"),
    _k("exports", "导出的交付包", "home", ("exports",), "user_material",
       in_backup=False, reason="导出包是从项目文件生成的，而项目文件已经在备份里了"),
    _k("browser_artifacts", "浏览器操作留下的截图和记录", "home", ("browser", "artifacts"), "user_material",
       in_backup=False,
       reason="现在还分不清哪些属于哪个项目，所以暂时不带；也不会删"),
    _k("external_agent_logs", "外部助手的运行记录", "home", ("logs", "external_agents"), "user_material",
       in_backup=False, reason="里面可能夹着订单内容，所以既不自动删，也暂时不带进备份"),

    # ── 运行库：装着用户的业务真相，一律保留 ──────────────────────────────────
    _k("business_db", "订单、验收与审批记录", "home", ("business.db",), "runtime_db",
       in_backup=False, reason="所有项目共用这一个文件；备份只带走属于这个项目的那部分（订单、验收、事件、预算）"),
    _k("ui_state_db", "聊天记录", "home", ("ui_state.db",), "runtime_db",
       in_backup=False, reason="所有项目共用这一个文件；备份只带走这个项目的聊天"),
    # 2026-09-24：原来这条写着「属第 4 期的迁移范围」。第 4 期做完了，而它压根不是迁移问题——它按
    # 「哪条消息处理过」记，行里根本不带项目，切不出「本项目那一片」。说清真实原因，别留一句会过期的话。
    _k("channel_inbound_db", "渠道收到的消息", "home", ("channel_inbound.db",), "runtime_db",
       in_backup=False, reason="它记的是「哪条消息处理过」，分不出属于哪个项目，所以单独带不走；整机备份会带上它"),
    # 数据与文件 P4：默认项目的运行库原本孤零零躺在总目录根上叫 `global.db`；现在它可以和别的项目一个
    # 形状，搬进 `projects/default/`。运行库的**内容**由上面那条 `project_tasks_db` 带走，不论它在哪——
    # 所以这一条说的只是「老位置上的那个文件」，`in_backup=False` 的意思是「这个路径不单独再装一遍」，
    # 不是「这份数据没人备份」。搬过之后它留在原地当退路，不再更新。
    _k("global_db", "搬家前留下的旧文件", "home", ("global.db",), "runtime_db",
       in_backup=False, reason="运行记录由上面那一条带走；这个文件是搬家前的旧件，留着以防万一，不再更新"),
    _k("project_tasks_db", "这个项目的运行记录", "project_dir", ("tasks.db",), "runtime_db"),
    _k("project_dir", "这个项目的系统文件", "project_dir", (), "runtime_db",
       note="系统用的文件，不在「资料」里显示"),

    # ── 配置 ────────────────────────────────────────────────────────────────
    _k("approval_allowlist", "你批准过的访问权限", "home", ("config", "approval_allowlist.yaml"), "config",
       in_backup=False, reason="只把这个项目的那部分放进备份，绝不整份拷走"),
    _k("llm_config", "模型设置与密钥", "home", ("config", "llm_config.yaml"), "config",
       in_backup=False, reason="里面有密钥，永远不进备份"),
    _k("config_dir", "产品设置", "home", ("config",), "config",
       in_backup=False, reason="产品自带的设置，跟着版本走，不属于某一个项目"),
    _k("prompts", "人才设定", "home", ("prompts",), "config",
       in_backup=False, reason="产品自带的，跟着版本走"),
    _k("skills", "已安装的技能", "home", ("skills",), "config",
       in_backup=False, reason="由业务流模板装上，开单时会重新装一遍"),
    _k("workflows", "业务流模板", "home", ("workflows",), "config",
       in_backup=False, reason="产品自带的，跟着版本走"),

    _k("browser_profiles", "浏览器里的登录状态", "home", ("browser", "profiles"), "system_internal",
       in_backup=False,
       reason="里面可能存着客户网站的登录状态，不跟着备份到处走；产品也不会自动删它"),

    # ── 浏览器引擎本体 ──────────────────────────────────────────────────────
    _k("browser_engine", "浏览器程序", "home", ("browsers",), "binary",
       in_backup=False, reason="装上的浏览器程序，几百 MB。删了就用不了浏览器，所以产品永远不会自己删它；真删了重装能回来"),

    # ── 产品自己的东西：owner 裁决的三族，保留 30 天 ──────────────────────────
    _k("node_trials", "搭建台的试跑记录", "home", ("node_trials",), "system_internal",
       retention="days_30", in_backup=False, reason="产品自己的试跑记录，不是你的资料"),
    _k("runtime_logs", "系统运行日志", "home", ("logs",), "system_internal",
       retention="days_30", in_backup=False, reason="产品自己的运行日志，不是你的资料",
       note="只清按日期分的那些；外部助手的记录是你的资料，另有说法"),
    _k("skill_candidates", "生成技能时的临时文件", "home", ("skill_candidates",), "system_internal",
       retention="days_30", in_backup=False, reason="生成技能时留下的中间文件"),

    # 控制台自己的两样运行状态（2026-09-25 上户口）：原来不在清单里，备份前会被当成「产品不认识的东西」报给你。
    _k("console_lock", "控制台同一时间只开一个用的锁", "home", ("office_ui.lock",), "system_internal",
       in_backup=False, reason="控制台开着时独占它，保证同一套数据只开一个控制台；关掉就没用了"),
    _k("console_address", "控制台现在开在哪个端口", "home", ("console-address.json",), "system_internal",
       in_backup=False, reason="控制台每次启动时重写、退出时删掉；命令行靠它找到这一套数据自己的控制台"),

    # ── 最笼统的两条放最后：前面都不认领时才轮到它们 ──────────────────────────
    _k("workplace_root", "所有项目文件的存放处", "workplace_root", (), "user_material",
       in_backup=False, reason="每个项目的文件各自进各自的备份", container=True),
    _k("ampuly_home", "全部数据的存放处", "home", (), "runtime_db",
       in_backup=False, reason="这里本身不是一类数据；下面的每一类各有各的说法", container=True),
)

BY_KEY: dict[str, DataKind] = {entry.key: entry for entry in DATA_MAP}
# 进备份的、故意不进的（连同理由）。备份那一侧照这两份名单办，不再自己列。
BACKED_UP: tuple[str, ...] = tuple(entry.key for entry in DATA_MAP if entry.in_backup)
DELIBERATELY_OUT: dict[str, str] = {entry.key: entry.reason for entry in DATA_MAP if not entry.in_backup}


@dataclass(frozen=True)
class Paths:
    """算一条路径要用到的四个根。工作区根与项目工作区分开传，因为项目工作区可以被钉到别处。"""

    home: Path
    workplace_root: Path
    project_workplace: Path
    project_dir: Path


def roots_for(home: Path, workplace_root: Path, project_id: str, *, project_workplace: Path | None = None) -> Paths:
    """按常规布局算出四个根。项目工作区被钉到别处时，由调用方传进来，不在这里猜。"""
    pid = str(project_id or "").strip()
    return Paths(
        home=Path(home),
        workplace_root=Path(workplace_root),
        project_workplace=Path(project_workplace) if project_workplace is not None else Path(workplace_root) / pid,
        project_dir=Path(home) / "projects" / pid,
    )


def path_of(entry: DataKind, paths: Paths, project_id: str = "") -> Path:
    """这一类数据在这台机器上的位置。"""
    base = {"home": paths.home, "workplace_root": paths.workplace_root,
            "project_workplace": paths.project_workplace, "project_dir": paths.project_dir}[entry.anchor]
    pid = str(project_id or "").strip()
    return Path(base).joinpath(*[part.replace("{pid}", pid) for part in entry.parts]) if entry.parts else Path(base)


def _inside(path: Path, root: Path) -> bool:
    """``path`` 是否就是 ``root`` 或在它下面。逐段按操作系统比较名字，大小写变体混不进来。

    不解析符号链接：解析了的话，一个指向工作区的目录联接会让工作区「看起来」在可清理的目录下面。
    """
    try:
        here = [os.path.normcase(part) for part in Path(path).parts]
        there = [os.path.normcase(part) for part in Path(root).parts]
    except (OSError, ValueError):
        return False
    return len(here) >= len(there) and here[:len(there)] == there


def claim(path: Path | str, paths: Paths, project_id: str = "") -> DataKind | None:
    """这条路径归哪一类；认不出来返回 ``None``。

    取**最具体**的那一条：路径越深的根越优先，所以 `memory/projects/<pid>/customers/a.md` 归客户记忆，而不是
    笼统地归数据总目录。同样深度时按 :data:`DATA_MAP` 的先后。

    纯容器（数据总目录、工作区根目录）不参与认领：它们说明不了里面那份文件归谁。

    ``None`` 不是「可以删」，是「不知道」。清理规则把不知道一律当作保留。
    """
    try:
        here = Path(path)
    except (OSError, ValueError):
        return None
    best: tuple[int, DataKind] | None = None
    for entry in DATA_MAP:
        if entry.container:
            continue
        root = path_of(entry, paths, project_id)
        if not _inside(here, root):
            continue
        depth = len(Path(root).parts)
        if best is None or depth > best[0]:
            best = (depth, entry)
    return best[1] if best is not None else None


def unclaimed(candidates: Iterable[Path | str], paths: Paths, project_id: str = "") -> list[str]:
    """这些路径里，产品认不出来的那些（按传进来的样子返回，不解析成绝对路径）。

    备份打包前扫一遍，把它们说给 owner 听：不认识的东西照样备份走，但值得看一眼是谁放进来的。
    """
    return [str(item) for item in candidates if claim(item, paths, project_id) is None]


def protected_keys() -> tuple[str, ...]:
    """永久保留、绝不自动清理的那些类。清理规则的保护根由这里生成，不手抄。"""
    return tuple(entry.key for entry in DATA_MAP if entry.retention == "forever")


def cleanable_keys() -> tuple[str, ...]:
    """可以自动清理的那几类（owner 裁决：只有产品自己的东西，保留 30 天）。"""
    return tuple(entry.key for entry in DATA_MAP if entry.retention == "days_30")
