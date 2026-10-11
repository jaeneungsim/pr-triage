"""Path globs with `**` support: `**/` matches any number of directories, `*` stays in one."""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import cache


@cache
def compile_glob(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(path: str, pattern: str) -> bool:
    return bool(compile_glob(pattern).match(path))


def matching(path: str, patterns: Iterable[str]) -> list[str]:
    return [p for p in patterns if matches(path, p)]
