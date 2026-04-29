from agentcore.orchestrator.runtime import Runtime
from agentcore.orchestrator.traces import TraceLog

__all__ = ["Runtime", "TraceLog", "build_app"]


def __getattr__(name: str):
    if name == "build_app":
        from agentcore.orchestrator.app import build_app

        return build_app
    raise AttributeError(name)
