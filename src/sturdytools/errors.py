"""Exceptions raised by sturdytools."""

from __future__ import annotations


class ToolTimeout(TimeoutError):
    """A tool call ran longer than its time limit."""

    def __init__(self, tool_name: str, seconds: float):
        self.tool_name = tool_name
        self.seconds = seconds
        super().__init__(f"tool '{tool_name}' took longer than {seconds:g}s")


class CircuitOpenError(RuntimeError):
    """Calls are paused because the tool failed too many times in a row."""

    def __init__(self, retry_in: float):
        self.retry_in = retry_in
        super().__init__(
            f"calls are paused after repeated failures, next try allowed in {retry_in:.1f}s"
        )
