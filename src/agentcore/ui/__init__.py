"""Lightweight Jinja UI for the orchestrator.

Registered by `orchestrator/app.py` under `/ui`. Classless Pico CSS +
shared.css + Lucide icons; no bundler, no build step. Read-only views
for dashboard / agents / chains / jobs / wiki — all surfaces the HTTP
API already exposes.
"""

from __future__ import annotations

from agentcore.ui.routes import mount_ui

__all__ = ["mount_ui"]
