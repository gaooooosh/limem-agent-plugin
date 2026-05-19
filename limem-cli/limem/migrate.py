"""migrate 子命令：清理历史项目中 LiMem 留下的 AGENTS.md/CLAUDE.md 占位块。

设计：
- ``clean_static`` — 移除占位块（按 begin/end 标记定位），保留外部内容
- ``sync_static`` — 用户主动调用时生成新的占位块（含 begin/end 标记）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

AGENTS_BEGIN = "<!-- limem:rules begin -->"
AGENTS_END = "<!-- limem:rules end -->"
CLAUDE_LINK_LINE = "See AGENTS.md `limem:rules` block for project memory rules managed by LiMem."

_BLOCK_RE = re.compile(
    re.escape(AGENTS_BEGIN) + r"[\s\S]*?" + re.escape(AGENTS_END) + r"\n?",
    re.MULTILINE,
)


@dataclass
class CleanResult:
    agents_md_cleaned: bool = False
    claude_md_link_removed: bool = False
    notes: list[str] = field(default_factory=list)


def clean_static(root: Path) -> CleanResult:
    res = CleanResult()
    agents = root / "AGENTS.md"
    if agents.exists():
        text = agents.read_text()
        new_text, n = _BLOCK_RE.subn("", text)
        if n > 0:
            agents.write_text(new_text)
            res.agents_md_cleaned = True
            res.notes.append(f"AGENTS.md: removed {n} limem block(s)")
    claude_md = root / "CLAUDE.md"
    if claude_md.exists():
        text = claude_md.read_text()
        if CLAUDE_LINK_LINE in text:
            new_text = "\n".join(
                ln for ln in text.splitlines() if ln.strip() != CLAUDE_LINK_LINE
            )
            if not new_text.endswith("\n"):
                new_text += "\n"
            claude_md.write_text(new_text)
            res.claude_md_link_removed = True
            res.notes.append("CLAUDE.md: removed limem reference line")
    return res


def sync_static(root: Path, *, agents_md: Path | None = None, rules_text: str = "") -> bool:
    """用户主动调用时生成占位块。当前实现：写入占位块（含 BEGIN/END），内容由 rules_text 提供。"""
    target = agents_md or (root / "AGENTS.md")
    block = AGENTS_BEGIN + "\n"
    if rules_text:
        block += rules_text.rstrip() + "\n"
    else:
        block += "<!-- This block is managed by LiMem. Run `limem sync-static` to refresh. -->\n"
    block += AGENTS_END + "\n"

    if target.exists():
        text = target.read_text()
        if AGENTS_BEGIN in text:
            new_text = _BLOCK_RE.sub(block, text)
            if new_text == text:
                new_text = text.rstrip() + "\n\n" + block
        else:
            new_text = block + "\n" + text
        target.write_text(new_text)
        return True
    target.write_text(block)
    return True
