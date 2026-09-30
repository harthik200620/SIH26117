"""Built-in tools. `register_builtin_tools` wires everything available at this milestone;
knowledge/vision/render tools register themselves when their subsystems land."""

from __future__ import annotations

from pathlib import Path

from yantra_server.tools.registry import ToolRegistry

from .artifact import ReadArtifactTool
from .execution import BashTool, PythonTool, RunTestsTool, SqlQueryTool
from .fs import (
    ApplyPatchTool,
    DeleteFileTool,
    EditFileTool,
    FileInfoTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    MoveFileTool,
    ReadFileTool,
    WriteFileTool,
)
from .harness import AskUserTool, DelegateTool


def register_builtin_tools(registry: ToolRegistry) -> ToolRegistry:
    from .calculator import CalculateTool
    from .knowledge import register_knowledge_tools
    from .memory import register_memory_tools
    from .quantities import CalculateQuantityTool
    from .render import register_render_tools
    from .vision import register_vision_tools

    for tool in (
        CalculateTool(),
        CalculateQuantityTool(),
        ListDirTool(),
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ApplyPatchTool(),
        GlobTool(),
        GrepTool(),
        MoveFileTool(),
        DeleteFileTool(),
        FileInfoTool(),
        BashTool(),
        PythonTool(),
        RunTestsTool(),
        SqlQueryTool(),
        ReadArtifactTool(),
        DelegateTool(),
        AskUserTool(),
    ):
        registry.register(tool)
    register_knowledge_tools(registry)
    register_render_tools(registry)
    register_vision_tools(registry)
    register_memory_tools(registry)
    return registry


def default_registry(assets_dir: Path) -> ToolRegistry:
    registry = ToolRegistry(fewshots_dir=assets_dir / "tools" / "fewshot")
    return register_builtin_tools(registry)
