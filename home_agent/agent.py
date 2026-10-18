"""Agent 本体：OpenAI Agents SDK + DeepSeek（OpenAI 兼容接口）。"""

from __future__ import annotations

from datetime import datetime

from agents import (
    Agent,
    ModelSettings,
    OpenAIChatCompletionsModel,
    Runner,
    SQLiteSession,
    set_tracing_disabled,
)
from openai import AsyncOpenAI

from . import config
from .skills import enabled_skills
from .tools.devices import DEVICE_TOOLS

# 追踪默认会把数据上报到 OpenAI 平台；这里用的是 DeepSeek，没有 OpenAI key，直接关掉
set_tracing_disabled(True)

BASE_INSTRUCTIONS = """你是用户的私人智能家居管家，运行在用户家里的电脑上。

工作方式：
- 用户提到设备时，先用 list_devices 看已知设备；找不到就用 discover_devices 扫描局域网（必要时 sweep=true）。
- 查看状态需要 view 权限，操作需要 control 权限。权限不够时调用 request_device_access 申请，
  并告诉用户怎么批准；不要尝试绕过权限、猜密码或用其它途径访问设备。
- 一次操作多台设备、或会影响安全的操作（门锁、燃气、安防、暖气长时间开启）前，先向用户确认。
- 操作完成后用一两句话说明结果；失败时说明原因和下一步建议。
- 回答简洁，用用户使用的语言。当前时间：{now}。消息来源：{source}。
"""

_agent: Agent | None = None


def build_agent() -> Agent:
    if not config.DEEPSEEK_API_KEY:
        raise RuntimeError("还没有设置 DeepSeek API Key，请在网页的“设置”里填写")
    client = AsyncOpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL)
    model = OpenAIChatCompletionsModel(model=config.DEEPSEEK_MODEL, openai_client=client)

    skills = enabled_skills()
    skill_text = "\n\n".join(f"## Skill: {s.name}\n{s.description}\n\n{s.instructions}" for s in skills)
    skill_tools = [t for s in skills for t in s.tools]

    def instructions(ctx, agent) -> str:
        source = (ctx.context or {}).get("source", "ui") if isinstance(ctx.context, dict) else "ui"
        return BASE_INSTRUCTIONS.format(now=datetime.now().strftime("%Y-%m-%d %H:%M"), source=source) \
            + "\n\n# 已加载的技能\n\n" + skill_text

    return Agent(
        name="HomeAgent",
        instructions=instructions,
        model=model,
        model_settings=ModelSettings(temperature=0.3),
        tools=[*DEVICE_TOOLS, *skill_tools],
    )


def invalidate() -> None:
    """key 或模型改了以后调用，下次对话时用新配置重建 Agent。"""
    global _agent
    _agent = None


def get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


async def ask(text: str, session_id: str, source: str = "ui") -> str:
    """跑一轮对话，返回最终回复。每个 session_id 有独立的多轮记忆。"""
    session = SQLiteSession(session_id, str(config.SESSIONS_DB))
    result = await Runner.run(get_agent(), text, session=session, max_turns=25,
                              context={"source": source})
    return str(result.final_output)


async def reset(session_id: str) -> None:
    await SQLiteSession(session_id, str(config.SESSIONS_DB)).clear_session()
