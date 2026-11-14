"""整棵已安装依赖树的锁：pip 看到什么就记什么，不猜、不装、不联网。

规划 W15 最后一项：运行时 SBOM 记的是六个直接组件及其声明依赖；发行还需要
**整棵传递依赖树**在这台机器上实际装的是什么版本。仓库里没有 pip-tools 或 uv，
本脚本不引入它们，只把项目 .venv 里 `pip freeze --all` 的结果规范化后写下来：

    .venv/Scripts/python scripts/runtime_lock.py write    # distribution/runtime/requirements.lock.txt
    .venv/Scripts/python scripts/runtime_lock.py check    # 与已提交文件比对，漂移即非零退出

规范化：按名字排序；`name @ file://...` 这类本机路径改写为 `name==version`
（路径含用户名，也不可移植），并在行尾注释说明来源是本机 wheel；不记任何
环境变量。`check` 进门禁（`tests/test_runtime_lock.py`），所以任何 pip 层面的
升级/降级都必须同时更新这个文件并被审阅。这是**锁**（已装即所锁），不是可解析
的约束文件——在别的机器重建环境时用 `pip install -r` 它，差异会在 SBOM/lock
检查里显示出来。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUTPUT = REPO / "distribution" / "runtime" / "requirements.lock.txt"
HEADER = ("# superones-runtime lock: exactly what this environment has installed (pip freeze --all, normalised).\n"
          "# The product's own editable install is excluded: it is what this lock is for, not a dependency of it.")
_LOCAL = re.compile(r"^(?P<name>[A-Za-z0-9_.\-]+) @ (?P<url>file:.*)$")
PRODUCT_DISTRIBUTION = "ampuly"


def _pin_name(line: str) -> str:
    return re.split(r"[ =@#]", line.strip(), maxsplit=1)[0].lower()


def _pin(line: str) -> str:
    """`name==version` only: the trailing note says where *this* machine got it from, and
    a fresh machine gets the same version from the index (V4, 2026-09-12)."""
    return line.split("#", 1)[0].strip()


def _freeze() -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "freeze", "--all", "--disable-pip-version-check"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip() and not line.startswith("#")]


def normalise(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if line.startswith("-e ") or line.startswith("--editable ") or _pin_name(line) == PRODUCT_DISTRIBUTION:
            # The product's own install, editable (pinned to the current
            # commit, which changes on every commit) or from its wheel on a
            # fresh machine (V4, 2026-09-12): it is what this lock is for,
            # not a dependency of it, so it is left out on purpose.
            continue
        match = _LOCAL.match(line)
        if match:
            name = match.group("name")
            try:
                version = metadata.version(name)
            except metadata.PackageNotFoundError:
                version = "unknown"
            out.append(f"{name}=={version}  # installed from a local wheel on this machine")
            continue
        if " @ " in line:
            # A remote URL install: keep the pin, drop the URL (it may carry a token).
            name = line.split(" @ ", 1)[0].strip()
            try:
                version = metadata.version(name)
            except metadata.PackageNotFoundError:
                version = "unknown"
            out.append(f"{name}=={version}  # installed from a URL; pin recorded, URL not")
            continue
        out.append(line)
    return sorted(out, key=lambda s: s.split("==", 1)[0].lower())


def build() -> str:
    return HEADER + "\n" + "\n".join(normalise(_freeze())) + "\n"


def write(path: Path = OUTPUT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build(), encoding="utf-8", newline="\n")
    return path


def check(path: Path = OUTPUT) -> list[str]:
    if not path.is_file():
        return [f"missing: {path}"]
    recorded = {_pin_name(l): _pin(l) for l in path.read_text(encoding="utf-8").splitlines()
                if l and not l.startswith("#")}
    current = {_pin_name(l): _pin(l) for l in normalise(_freeze())}
    problems: list[str] = []
    for name, line in current.items():
        if name not in recorded:
            problems.append(f"{name}: installed but not in the lock ({line})")
        elif recorded[name] != line:
            problems.append(f"{name}: {recorded[name]!r} -> {line!r}")
    for name in set(recorded) - set(current):
        problems.append(f"{name}: in the lock but not installed")
    return sorted(problems)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("write", "check"))
    parser.add_argument("--path", default=str(OUTPUT))
    args = parser.parse_args()
    path = Path(args.path)
    if args.command == "write":
        print(f"wrote {write(path)}")
        return 0
    problems = check(path)
    for line in problems:
        print(f"drift: {line}")
    print("runtime lock: OK" if not problems else f"runtime lock: {len(problems)} difference(s); review the change, then `write`")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
