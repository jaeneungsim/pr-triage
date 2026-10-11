from __future__ import annotations

import re

_COMMENT = re.compile(r"<!--.*?-->", re.S)


def normalize(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def section(text: str | None, title: str) -> str | None:
    """The body of the first `#`/`##` heading named `title`, up to the next level-1/2 heading."""
    text = normalize(text)
    heading = re.compile(rf"^#{{1,6}}[ \t]*{re.escape(title)}[ \t]*$", re.I | re.M)
    match = heading.search(text)
    if not match:
        return None
    rest = text[match.end() :]
    following = re.search(r"^#{1,2}[ \t]+\S", rest, re.M)
    return rest[: following.start()] if following else rest


def first_line(text: str | None) -> str | None:
    """The first non-empty line once HTML comments are removed."""
    for line in _COMMENT.sub("", text or "").splitlines():
        if line.strip():
            return line.strip()
    return None
