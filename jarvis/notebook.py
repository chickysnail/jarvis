"""Jarvis's notebook: plain-text files it edits itself. Lives in data/ (never committed)."""

from pathlib import Path

from . import config

FILES = {
    "about_me": "About my person — who they are, goals, things they asked me to do or not do.",
    "what_works": "What works and what doesn't — how they respond to my messages, timing, tone.",
    "open_threads": "Open threads — things to follow up on, promises, ongoing situations.",
}
MAX_CHARS = 12_000  # per file; keeps the prompt bounded


def directory() -> Path:
    d = config.data_dir() / "notebook"
    d.mkdir(parents=True, exist_ok=True)
    return d


def path(name: str) -> Path:
    if name not in FILES:
        raise ValueError(f"unknown notebook file {name!r}; use one of {list(FILES)}")
    return directory() / f"{name}.md"


def read(name: str) -> str:
    p = path(name)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def write(name: str, content: str) -> None:
    if len(content) > MAX_CHARS:
        raise ValueError(f"{name} would be {len(content)} chars; max is {MAX_CHARS}. Condense it.")
    p = path(name)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(p)


def append(name: str, text: str) -> None:
    cur = read(name)
    write(name, (cur.rstrip() + "\n" if cur.strip() else "") + text.strip() + "\n")


def render() -> str:
    parts = []
    for name, desc in FILES.items():
        body = read(name).strip() or "(empty)"
        parts.append(f"### {name}.md — {desc}\n{body}")
    return "\n\n".join(parts)
