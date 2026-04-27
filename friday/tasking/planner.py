"""Builds and revises step lists using LLM."""
import logging
from livekit.agents import llm
from friday.providers import build_llm
from friday.config import MAX_PLAN_STEPS
from .models import TaskStep

logger = logging.getLogger("friday-agent")

_PLANNER_PROMPT = f"""
You are the JARVIS Planner. Break down the user's goal into a maximum of {MAX_PLAN_STEPS} steps.
Provide the steps formatted EXACTLY as a JSON array of strings. Do not provide any other text.
Example: ["Search for topic X", "Summarize results"]
Goal: {{goal}}
"""

async def plan_steps(goal: str) -> list[TaskStep]:
    llm_instance = build_llm(mode="planner")
    ctx = llm.ChatContext().append(role="user", content=_PLANNER_PROMPT.format(goal=goal))
    
    try:
        response = ""
        # We manually consume the generator
        async for chunk in await llm_instance.chat(chat_ctx=ctx):
            if chunk.choices and chunk.choices[0].delta.content:
                response += chunk.choices[0].delta.content
                
        import json, re
        match = re.search(r"\[.*\]", response, re.DOTALL)
        if match:
            steps_list = json.loads(match.group(0))
            return [TaskStep(id=i+1, title=title) for i, title in enumerate(steps_list)]
    except Exception as e:
        logger.error(f"Planning failed: {e}")
        
    return [TaskStep(id=1, title="Execute task tools to achieve goal")]
