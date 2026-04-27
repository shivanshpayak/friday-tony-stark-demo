"""Standalone visible console executor for heavy tasks."""
import asyncio
import sys
import os
import argparse
from datetime import datetime
from pathlib import Path

from livekit.agents import llm
from livekit.agents.llm.mcp import MCPServerStdio, MCPToolset
from friday.providers import build_llm
from friday.tasking.store import load_task, save_task

_REPO_ROOT = Path(__file__).resolve().parents[2]

async def execute_in_terminal(task_id: str):
    print(f"\033[96m[JARVIS SUB-ROUTINE ONLINE]\033[0m")
    print(f"Task ID: {task_id}")
    
    task = load_task(task_id)
    if not task:
        print(f"\033[91mError: Task {task_id} not found locally.\033[0m")
        input("Press Enter to close...")
        return
        
    task.status = "running"
    task.updated_at = datetime.now().isoformat()
    save_task(task)
    
    print(f"\n\033[94mGoal:\033[0m {task.goal}\n")
    print(f"\033[90mBooting up MCP standard I/O toolkit...\033[0m")
    
    mcp_server = MCPServerStdio(
        command=sys.executable,
        args=[str(_REPO_ROOT / "server.py")],
        cwd=str(_REPO_ROOT),
    )
    
    toolset = MCPToolset(mcp_server=mcp_server)
    await toolset.initialize()
    
    print(f"\033[32mAgent initialized. Thinking...\033[0m\n")
    
    llm_idx = build_llm(mode="planner")
    ctx = llm.ChatContext()
    ctx.append(role="system", content="You are an autonomous sub-routine agent spawned by JARVIS to execute a complex coding or logical task in the background. You have access to his tool suite. DO NOT ask the user questions, as you are a headless background agent. DO work step by step, utilizing your tools. When you are finished, output a concise final summary of what you did.")
    ctx.append(role="user", content=f"Target Goal: {task.goal}")
    
    response_text = ""
    try:
        # Autonomous tool-calling loop
        for step in range(15): # Max 15 steps per task
            chat_stream = await llm_idx.chat(chat_ctx=ctx, fnc_ctx=toolset)
            
            ai_msg = llm.ChatMessage(role="assistant", content="")
            tool_calls = []
            
            async for chunk in chat_stream:
                if not chunk.choices: continue
                delta = chunk.choices[0].delta
                
                # Render text
                if delta.content:
                    ai_msg.content += delta.content
                    sys.stdout.write(f"\033[37m{delta.content}\033[0m")
                    sys.stdout.flush()
                    
                # Collect tool calls built by the LLM wrapper
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        tool_calls.append(tc)
            
            # Store the agent's message in the context
            ai_msg.tool_calls = tool_calls
            ctx.messages.append(ai_msg)
            
            if not tool_calls:
                break # Agent has finished and didn't invoke any tools!
                
            print("\n")
            
            # Execute all tool calls
            for call in tool_calls:
                func_name = call.function.name
                print(f"\033[93m[Executing Tool] {func_name}...\033[0m")
                
                # Use livekit utility to execute against the toolset
                result = await llm.execute_function_call(call, toolset)
                
                # Append tool result to context
                tool_msg = llm.ChatMessage(
                    role="tool", 
                    tool_call_id=call.call_id, 
                    content=result.result,
                    name=func_name
                )
                ctx.messages.append(tool_msg)
                print(f"\033[90m  -> Result: {str(result.result)[:100]}...\033[0m")

        print("\n\033[92m[TASK COMPLETE]\033[0m")
        task.status = "completed"
        task.final_summary = ai_msg.content.strip() if ai_msg.content else "Task completed successfully."
        
    except Exception as e:
        print(f"\n\033[91m[TASK FAILED]\033[0m {e}")
        task.status = "failed"
        task.final_summary = f"I ran into an error while working on that: {e}"
    finally:
        task.updated_at = datetime.now().isoformat()
        save_task(task)
        await toolset.aclose()
        # Windows pause so the user can literally read the final output before the cmd window vaporizes
        input("\nPress Enter to exit...")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m friday.tasking.standalone_executor <task_id>")
        sys.exit(1)
        
    asyncio.run(execute_in_terminal(sys.argv[1]))
