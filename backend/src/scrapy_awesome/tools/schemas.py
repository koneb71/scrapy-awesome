"""Turn `Tools` methods into provider-neutral `ToolSpec`s (name, description, JSON schema, fn).

Schemas come from the same helper FastMCP uses, so the in-app designer and MCP clients see the
identical tool contract.
"""

from __future__ import annotations

import inspect
from typing import Any

from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from scrapy_awesome.llm.base import ToolSpec
from scrapy_awesome.tools.core import TOOL_NAMES, Tools

# Tools that only make sense for an *external* agent (the in-app designer already lives in the UI).
IN_APP_EXCLUDE = frozenset({"open_ui", "app_status"})


def _strip(schema: Any) -> Any:
    """Drop `title` noise recursively (keeps schemas small for the model)."""
    if isinstance(schema, dict):
        return {k: _strip(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip(x) for x in schema]
    return schema


def tool_specs(tools: Tools, names: list[str] | None = None) -> list[ToolSpec]:
    out: list[ToolSpec] = []
    for name in names or TOOL_NAMES:
        fn = getattr(tools, name)
        meta = func_metadata(fn)
        schema = _strip(meta.arg_model.model_json_schema())
        out.append(
            ToolSpec(
                name=name,
                description=inspect.getdoc(fn) or name,
                input_schema=schema,
                fn=fn,
            )
        )
    return out


def in_app_tool_specs(tools: Tools) -> list[ToolSpec]:
    return tool_specs(tools, [n for n in TOOL_NAMES if n not in IN_APP_EXCLUDE])
