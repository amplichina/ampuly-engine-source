"""What a block of Python source literally names, read from its grammar.

The approval layer enforces a denied-path rule by comparing it against a
tool's path argument. `python_exec` has no path argument -- it has `code`,
and every place it will touch is a string inside that code. Without a reader
for those strings the rule simply does not apply to it, so a target refused
to `file_write` is reached by `python_exec` with no prompt at all. Measured
on 2026-09-10: `file_write` denied, `python_exec` allowed, same target.

This reads the strings with `ast`, not with a pattern. That distinction is
the whole point of the module. A regex over source text guesses at Python's
grammar and is wrong in both directions -- it finds paths inside comments and
misses them inside f-strings -- and a guess that is treated as a check is
worse than no check. Parsing gives the literals the interpreter will see.

What it does not give: a path the code assembles at runtime.
``open("sec" + "rets")``, a name from `os.environ`, a value read from a file.
Those are invisible here and are meant to be -- this is a preflight over what
the code *says*. What the code *does* is the execution end's job: the
workspace fence and the process sandbox. Neither replaces the other, and
this module claims only its half.
"""

from __future__ import annotations

import ast
from pathlib import Path


# Long enough to be a path, short enough to still be one. A whole document
# pasted into a string is not a filename, and resolving it wastes the check.
_MAX_LITERAL_CHARS = 4096


def python_code_string_literals(code: str) -> list[str]:
    """Every string constant in a block of Python source, in source order.

    Unparseable source yields nothing rather than raising. The code will not
    run either -- `python_exec` hands the same text to the same grammar -- so
    a syntax error is not a hole to fall through, and a caller that has to
    guard every call would eventually forget.
    """

    text = str(code or "")
    if not text.strip():
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if value and len(value) <= _MAX_LITERAL_CHARS:
                found.append(value)
    return found


def python_code_path_literals(code: str, *, working_directory: str) -> list[str]:
    """The string literals in `code`, resolved as paths against `cwd`.

    Every literal is offered, not only the ones that look like paths. A rule
    denies a place; whether a given string was *meant* as a path is not
    knowable here and guessing at it only creates a way to be missed. A
    literal that is not a path resolves under the working directory, matches
    no rule, and costs one comparison.

    Literals holding a shell metacharacter, a newline or a NUL are dropped:
    those cannot be a single filename, and `resolve()` on Windows raises on
    some of them.
    """

    try:
        cwd = Path(working_directory).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return []
    resolved: list[str] = []
    seen: set[str] = set()
    for literal in python_code_string_literals(code):
        if any(char in literal for char in "\x00\n\r\t*?<>|"):
            continue
        try:
            path = Path(literal).expanduser()
            target = (
                path.resolve(strict=False)
                if path.is_absolute()
                else (cwd / path).resolve(strict=False)
            )
        except (OSError, RuntimeError, ValueError):
            continue
        text_value = str(target)
        if text_value not in seen:
            seen.add(text_value)
            resolved.append(text_value)
    return resolved
