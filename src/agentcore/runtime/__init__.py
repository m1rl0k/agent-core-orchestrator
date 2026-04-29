"""Runtime executors — pre-LLM hooks that ground agents in real tool output."""

from agentcore.runtime.executors import EXECUTORS, run_executor

__all__ = ["EXECUTORS", "run_executor"]
