"""Task orchestration package.

The public API is resolved lazily (PEP 562). Tool modules in the MCP server
import only `friday.tasking.models` / `.store`; an eager import of `.service`
here chained into `.executor` → `friday.providers` and loaded the entire LiveKit
+ Google Cloud voice stack into the tool server (~22s of its boot, which the
agent's session.start() waits on). Guarded by tests/test_boot_imports.py.
"""

_EXPORTS = {
    "start_task": "service",
    "get_task_status": "service",
    "summarize_task": "service",
    "register_toolset": "service",
    "start_worker": "service",
    "set_completion_callback": "service",
    "classify_request": "router",
}


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(f".{module_name}", __name__), name)


__all__ = list(_EXPORTS)
