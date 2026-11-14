"""把发行目录里的产品资产铺到运行目录，**只补不覆盖**。

为什么要有这一步（数据与文件 P5）：人才提示词原本住在 `.ampuly/prompts/talent/`，而那个目录被 Git 跟踪着
（`.gitignore` 里专门开了口子）。于是产品在运行时动一下它们，仓库就脏一片——2026-09-24 的 `git status`
里躺着 189 个改动文件，而按协作纪律 `.ampuly/` 永远不 stage，它们既进不去、又一条 `git restore` 就没。

改法不是「别动它们」，是**把发行版那一份挪到 `distribution/`**（那里跟踪在 Git 里），运行目录变成一份
部署副本。`.ampuly/` 从此可以整个忽略。

**只补不覆盖**，理由是这个目录不只装产品的东西：`ampuly talent import_repo` 和市场包安装也往这里写。覆盖
会把用户自己导进来的人才悄悄换掉。代价是一次发行版更新不会自动追上已经装好的那份——这是个已知取舍，
写在这里而不是留给人去猜。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

# 仓库根：`ampuly/core/asset_seed.py` → `ampuly/core` → `ampuly` → 仓库。装成 wheel 之后这个目录可能不存在，
# 那就什么都不做——铺不了不是错误，只是没有可铺的东西。
REPO = Path(__file__).resolve().parents[2]
DISTRIBUTION = REPO / "distribution"

# 哪些资产按这个方式铺。每一项是 (发行目录下的相对路径, 运行目录下的相对路径)。
SEEDED: tuple[tuple[str, str], ...] = (
    ("prompts/talent", "prompts/talent"),
)


def seed(home: Path | str, *, distribution: Path | None = None) -> dict[str, Any]:
    """把缺的那些补到运行目录里。已经在的一个都不动。

    返回每一类补了几个、跳过了几个，方便启动日志说一句人话。
    """
    source_root = Path(distribution) if distribution is not None else DISTRIBUTION
    out: dict[str, Any] = {"added": 0, "kept": 0, "groups": {}}
    if not source_root.is_dir():
        return out
    for relative_from, relative_to in SEEDED:
        source = source_root / relative_from
        target = Path(home) / relative_to
        added = 0
        kept = 0
        if not source.is_dir():
            continue
        target.mkdir(parents=True, exist_ok=True)
        for item in sorted(source.rglob("*")):
            if not item.is_file():
                continue
            landing = target / item.relative_to(source)
            if landing.exists():
                kept += 1
                continue
            landing.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(item, landing)
            except OSError:
                continue
            added += 1
        out["groups"][relative_to] = {"added": added, "kept": kept}
        out["added"] += added
        out["kept"] += kept
    return out
