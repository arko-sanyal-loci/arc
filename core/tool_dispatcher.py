"""
core/tool_dispatcher.py — OpenAI-compatible tool registration and dispatch.

Design
------
Ported and re-architected from:
  arkosanyal/loci-engine/Workers/learning_loop.py  ~lines 2860-3000
  _make_tool_schema() + tool registration + dispatch loop

The dispatcher holds a registry of ``ToolDefinition`` objects.  Each
definition carries:
  • An OpenAI-compatible JSON schema (passed verbatim to the LLM as the
    ``tools`` list in every chat-completion request).
  • A synchronous Python handler function that accepts a dict of arguments
    and returns a string result.

Registered built-ins
--------------------
  scratch_write  / scratch_read / scratch_clear   — in-process scratchpad
  arc_browser_fetch                               — web fetch (Phase 1 stub;
                                                    filled by core/browser.py
                                                    when that module is loaded)

Usage from app/server.py
------------------------
    from core.tool_dispatcher import dispatcher

    # 1. Get the tools list to pass to the LLM provider.
    tools = dispatcher.schemas()

    # 2. After the LLM responds with tool_calls, dispatch them.
    results = dispatcher.dispatch_all(response.choices[0].message.tool_calls)

    # 3. Append tool results to messages and re-call the LLM.
    for tc, result in results:
        messages.append({
            "role":         "tool",
            "tool_call_id": tc.id,
            "content":      result,
        })

Registering a new tool at runtime
----------------------------------
    from core.tool_dispatcher import dispatcher, _make_tool_schema

    def my_handler(args: dict) -> str:
        return json.dumps({"result": args["x"] * 2})

    dispatcher.register(
        name        = "double_it",
        description = "Doubles the input number.",
        parameters  = {
            "type":       "object",
            "properties": {"x": {"type": "number", "description": "Input."}},
            "required":   ["x"],
        },
        handler     = my_handler,
    )
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema builder (mirrors _make_tool_schema from learning_loop.py)
# ---------------------------------------------------------------------------

def _make_tool_schema(
    name:        str,
    description: str,
    parameters:  Dict[str, Any],
) -> Dict[str, Any]:
    """
    Build an OpenAI-compatible function-call tool schema.

    Args:
        name:        Function name (must be unique in a tools list).
        description: One-sentence description shown to the LLM.
        parameters:  JSON Schema object for the function's arguments.

    Returns:
        ``{"type": "function", "function": {...}}`` dict.
    """
    return {
        "type": "function",
        "function": {
            "name":        name,
            "description": description,
            "parameters":  parameters,
        },
    }


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

Handler = Callable[[Dict[str, Any]], str]


@dataclass
class ToolDefinition:
    """A registered tool: its schema + its Python handler."""

    name:    str
    schema:  Dict[str, Any]        # full OpenAI tool object
    handler: Handler
    # Optional metadata
    tags:    List[str] = field(default_factory=list)
    enabled: bool      = True


# Minimal stand-in for openai.types.chat.ChatCompletionMessageToolCall
# so we can dispatch without requiring the openai package at import time.
@dataclass
class ToolCall:
    """Normalised tool call from an LLM response."""
    id:        str
    name:      str
    arguments: Dict[str, Any]   # already parsed JSON


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class ToolDispatcher:
    """
    Thread-safe registry + dispatch engine for LLM tool calls.

    Thread safety: a single RLock guards the registry dict so handlers
    can register new tools from worker threads without data races.
    """

    def __init__(self) -> None:
        self._lock:     threading.RLock                   = threading.RLock()
        self._registry: Dict[str, ToolDefinition]         = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name:        str,
        description: str,
        parameters:  Dict[str, Any],
        handler:     Handler,
        *,
        tags:        Optional[List[str]] = None,
        overwrite:   bool                = False,
    ) -> None:
        """
        Register a new tool (or overwrite an existing one).

        Raises:
            ValueError: If the name is already registered and
                        ``overwrite=False``.
        """
        schema = _make_tool_schema(name, description, parameters)
        defn   = ToolDefinition(
            name    = name,
            schema  = schema,
            handler = handler,
            tags    = tags or [],
        )
        with self._lock:
            if name in self._registry and not overwrite:
                raise ValueError(
                    f"Tool {name!r} is already registered.  "
                    "Pass overwrite=True to replace it."
                )
            self._registry[name] = defn
            logger.debug("Registered tool: %s", name)

    def register_schema(
        self,
        schema:  Dict[str, Any],
        handler: Handler,
        *,
        tags:    Optional[List[str]] = None,
        overwrite: bool              = False,
    ) -> None:
        """
        Register a tool from a pre-built OpenAI-compatible schema dict.

        Useful when importing schemas from another module (e.g. scratchpad).
        """
        fn   = schema.get("function", schema)   # unwrap if needed
        name = fn["name"]
        with self._lock:
            if name in self._registry and not overwrite:
                raise ValueError(
                    f"Tool {name!r} already registered.  Pass overwrite=True."
                )
            defn = ToolDefinition(
                name    = name,
                schema  = schema if "type" in schema else {"type": "function", "function": schema},
                handler = handler,
                tags    = tags or [],
            )
            self._registry[name] = defn
            logger.debug("Registered tool (from schema): %s", name)

    def unregister(self, name: str) -> bool:
        """Remove a tool by name.  Returns True if it was present."""
        with self._lock:
            if name in self._registry:
                del self._registry[name]
                return True
            return False

    def set_enabled(self, name: str, enabled: bool) -> None:
        """Enable or disable a tool without removing it."""
        with self._lock:
            if name in self._registry:
                self._registry[name].enabled = enabled

    # ------------------------------------------------------------------
    # Schema export
    # ------------------------------------------------------------------

    def schemas(self, *, tags: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Return the list of tool schemas to pass to the LLM provider.

        Args:
            tags: If given, only return tools whose tag list overlaps.
                  None returns all enabled tools.
        """
        with self._lock:
            defs = list(self._registry.values())

        result = []
        for defn in defs:
            if not defn.enabled:
                continue
            if tags and not any(t in defn.tags for t in tags):
                continue
            result.append(defn.schema)
        return result

    # ------------------------------------------------------------------
    # Dispatch — single call
    # ------------------------------------------------------------------

    def dispatch(self, tool_call: ToolCall) -> str:
        """
        Execute a single tool call and return the result string.

        Never raises: all exceptions are caught, logged, and returned
        as a JSON error so the LLM can recover gracefully.
        """
        with self._lock:
            defn = self._registry.get(tool_call.name)

        if defn is None:
            msg = f"Tool {tool_call.name!r} is not registered."
            logger.warning(msg)
            return json.dumps({"error": msg})

        if not defn.enabled:
            msg = f"Tool {tool_call.name!r} is disabled."
            logger.warning(msg)
            return json.dumps({"error": msg})

        try:
            result = defn.handler(tool_call.arguments)
            # Ensure we always return a string.
            if not isinstance(result, str):
                result = json.dumps(result)
            return result
        except Exception as exc:   # noqa: BLE001
            msg = f"Tool {tool_call.name!r} raised {type(exc).__name__}: {exc}"
            logger.exception("Tool dispatch error: %s", tool_call.name)
            return json.dumps({"error": msg})

    # ------------------------------------------------------------------
    # Dispatch — batch (all tool_calls from one LLM response)
    # ------------------------------------------------------------------

    def dispatch_all(
        self,
        tool_calls: List[Any],
    ) -> List[Tuple[Any, str]]:
        """
        Dispatch all tool calls from an LLM response in order.

        Accepts either:
          • A list of ``ToolCall`` dataclass instances (this module's type)
          • A list of openai ``ChatCompletionMessageToolCall`` objects

        Returns:
            List of ``(tool_call, result_string)`` pairs in the same order
            as the input, ready to append to the messages list.
        """
        results = []
        for tc in tool_calls:
            normalised = _normalise_tool_call(tc)
            result     = self.dispatch(normalised)
            results.append((tc, result))
        return results

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._registry)

    def __contains__(self, name: str) -> bool:
        with self._lock:
            return name in self._registry

    def __repr__(self) -> str:  # pragma: no cover
        with self._lock:
            names = sorted(self._registry)
        return f"<ToolDispatcher tools={names}>"


# ---------------------------------------------------------------------------
# Normalisation helper
# ---------------------------------------------------------------------------

def _normalise_tool_call(tc: Any) -> ToolCall:
    """
    Convert an openai tool call object (or any duck-typed equivalent) to
    our internal ToolCall dataclass.
    """
    if isinstance(tc, ToolCall):
        return tc

    # openai SDK: tc.function.name / tc.function.arguments (JSON string)
    if hasattr(tc, "function"):
        raw_args = tc.function.arguments
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {}
        else:
            args = raw_args or {}
        return ToolCall(id=tc.id, name=tc.function.name, arguments=args)

    # Dict-style: {"id": ..., "function": {"name": ..., "arguments": ...}}
    if isinstance(tc, dict):
        fn   = tc.get("function", {})
        raw  = fn.get("arguments", "{}")
        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
        return ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args)

    raise TypeError(f"Cannot normalise tool call of type {type(tc)!r}")


# ---------------------------------------------------------------------------
# Built-in handler factories
# ---------------------------------------------------------------------------

def _make_browser_fetch_stub() -> Handler:
    """
    Placeholder browser fetch handler.

    Replaced at runtime by core/browser.py:
        from core.tool_dispatcher import dispatcher
        dispatcher.register(
            "arc_browser_fetch", ..., handler=real_handler, overwrite=True
        )
    """
    def handler(args: Dict[str, Any]) -> str:  # noqa: ANN001
        return json.dumps({
            "error": (
                "arc_browser_fetch is not yet wired.  "
                "Load core/browser.py to activate this tool."
            )
        })
    return handler


# ---------------------------------------------------------------------------
# Module-level singleton + built-in registration
# ---------------------------------------------------------------------------

#: Global dispatcher.  Import and use directly.
dispatcher: ToolDispatcher = ToolDispatcher()


def _bootstrap() -> None:
    """Register all built-in tools into the global dispatcher."""

    # --- Scratchpad tools (from core/scratchpad.py) ---
    from core.scratchpad import scratch  # local import to avoid cycles

    for schema in scratch.tool_schemas():
        dispatcher.register_schema(
            schema  = schema,
            handler = lambda args, _sc=scratch, _n=schema["function"]["name"]: (
                _sc.handle_tool_call(_n, args)
            ),
            tags    = ["scratchpad"],
        )

    # --- Browser fetch stub (overwritten by core/browser.py at startup) ---
    dispatcher.register(
        name        = "arc_browser_fetch",
        description = (
            "Fetch a web page and return its title, plain-text body, and "
            "outbound links.  Result is automatically ingested into the "
            "Loci knowledge store."
        ),
        parameters  = {
            "type":       "object",
            "properties": {
                "url": {
                    "type":        "string",
                    "description": "Fully-qualified HTTPS URL to fetch.",
                },
                "extract_links": {
                    "type":        "boolean",
                    "description": "Whether to include outbound <a href> links in the result.",
                    "default":     False,
                },
            },
            "required": ["url"],
        },
        handler = _make_browser_fetch_stub(),
        tags    = ["browser"],
    )

    # --- Calendar memory tools (from core/calendar_memory.py) ---
    from core.calendar_memory import calendar  # local import to avoid cycles

    for schema in calendar.tool_schemas():
        dispatcher.register_schema(
            schema  = schema,
            handler = lambda args, _cal=calendar, _n=schema["function"]["name"]: (
                _cal.handle_tool_call(_n, args)
            ),
            tags    = ["calendar", "memory"],
        )

    logger.info(
        "tool_dispatcher bootstrapped: %d tools registered", len(dispatcher)
    )


_bootstrap()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "ToolDispatcher",
    "ToolDefinition",
    "ToolCall",
    "dispatcher",
    "_make_tool_schema",
]
