"""ACESEvals/SABER official agent bridge.

Loaded by the isolated Inspect process (via ``sitecustomize``); it does not
replace the upstream task, sandbox, telemetry backend, or scorer.
"""
from __future__ import annotations

import inspect
import json
from typing import Any

from .superagent_runtime import RuntimeConfig, ToolSpec, run_reference, run_superagent


def _tool_spec(tool: Any) -> ToolSpec:
    name = getattr(tool, "__name__", "official_tool")
    sig = inspect.signature(tool)
    props = {p.name: {"type": "string"} for p in sig.parameters.values()
             if p.name != "self"}
    required = [p.name for p in sig.parameters.values()
                if p.default is inspect.Parameter.empty and p.name != "self"]
    return ToolSpec(name, tool, getattr(tool, "__doc__", "") or "Official SABER tool",
                    {"type": "object", "properties": props, "required": required,
                     "additionalProperties": False})


def create_agent(*, arm: str = "single", **_factory_kwargs: Any):
    """Return the two-level factory expected by ``saber.create_saber_solver``."""
    def create_with_prompts(*, instruction_prompt: str = "", assistant_prompt: str = "",
                            tools=None, max_steps: int = 30, **_kwargs: Any):
        async def solve(state, _generate):
            from inspect_ai.model import ChatMessageSystem, ChatMessageUser, get_model
            from inspect_ai.util import store

            official_tools = [_tool_spec(t) for t in (tools or [])]
            messages = []
            for message in getattr(state, "messages", [])[-12:]:
                content = getattr(message, "content", "")
                if content:
                    messages.append({"role": getattr(message, "role", "user"),
                                     "content": str(content)[:12000]})
            visible = getattr(state, "input", "") or (messages[-1]["content"] if messages else "")
            tools_json = [{"name": t.name, "description": t.description,
                           "input_schema": t.input_schema} for t in official_tools]

            async def llm(system: str, user: str) -> str:
                prompt = (system + "\n\nOfficial task context:\n" + str(visible)[:12000]
                          + "\nConversation:\n" + json.dumps(messages, ensure_ascii=False)
                          + "\nAvailable official tools:\n" + json.dumps(tools_json, ensure_ascii=False)
                          + "\nRuntime request:\n" + user)
                response = await get_model().generate([
                    ChatMessageSystem(content="You are CyberOrion's auditable ExCyTIn investigator. Return only the requested JSON decision."),
                    ChatMessageUser(content=prompt),
                ])
                return response.completion

            cfg = RuntimeConfig(max_steps=min(int(max_steps), 30), max_llm_calls=min(int(max_steps), 30),
                                max_tool_calls=min(int(max_steps), 30), max_dispatches=8, max_role_steps=6)
            kwargs = {"task": str(visible), "llm": llm, "tools": official_tools,
                      "config": cfg, "mode": "superagent" if arm == "full" else "reference"}
            result = await (run_superagent(**kwargs) if arm == "full" else run_reference(**kwargs))
            store().set("cyberorion_arm", arm)
            store().set("cyberorion_runtime_trace", result)
            from inspect_ai.model import ChatMessageAssistant
            state.output = ChatMessageAssistant(content=str(result.get("output") or ""))
            return state
        return solve
    return create_with_prompts


def register_official_agents() -> None:
    from saber.agents import AgentRegistry
    for name, arm in (("cyberorion_single", "single"), ("cyberorion_full", "full")):
        if AgentRegistry.get(name) is None:
            AgentRegistry.register(name, lambda _arm=arm, **kw: create_agent(arm=_arm, **kw))
