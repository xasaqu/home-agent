"""极简 skill 加载器。

每个 skill 是 skills/<name>/ 目录：SKILL.md（带 name/description 头信息的说明）
+ 可选的 tools.py（导出 TOOLS 列表）。SKILL.md 的正文会拼进 Agent 的系统提示，
TOOLS 会注册给 Agent。新增 skill 只需要新建目录，并在 DEFAULT_SKILLS 或环境变量里启用。
"""

from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SKILLS_DIR = Path(__file__).parent
DEFAULT_SKILLS = ["webaccess"]


@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    tools: list[Any] = field(default_factory=list)


def load_skill(name: str) -> Skill:
    folder = SKILLS_DIR / name
    text = (folder / "SKILL.md").read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
        text = m.group(2)
    tools = []
    module = f"{__name__}.{name}.tools"
    try:  # 打包后的 App 里没有 .py 文件，所以直接尝试导入，而不是检查文件是否存在
        tools = list(importlib.import_module(module).TOOLS)
    except ModuleNotFoundError as e:
        if e.name != module:
            raise
    return Skill(meta.get("name", name), meta.get("description", ""), text.strip(), tools)


def enabled_skills() -> list[Skill]:
    names = os.getenv("HOME_AGENT_SKILLS")
    wanted = [n.strip() for n in names.split(",") if n.strip()] if names else DEFAULT_SKILLS
    return [load_skill(n) for n in wanted]
