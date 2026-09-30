"""Filesystem tools: workspace-scoped, symlink-escape-proof (SPEC §9.2)."""

from __future__ import annotations

import fnmatch
import re
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from yantra_server.tools.base import Tool, ToolContext, ToolError, ToolResult

MAX_READ_BYTES = 4 * 1024 * 1024
BINARY_SNIFF = 8192


def _is_binary(path: Path) -> bool:
    with path.open("rb") as fh:
        return b"\x00" in fh.read(BINARY_SNIFF)


def _numbered(text: str, start: int = 1) -> str:
    return "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(text.splitlines(), start))


class ListDirArgs(BaseModel):
    path: str = "."
    depth: int = Field(default=2, ge=1, le=6)
    glob: str | None = Field(default=None, description="Only entries matching this pattern")


class ListDirTool(Tool):
    name = "list_dir"
    description = "List files and folders under a workspace path, up to `depth` levels."
    Args = ListDirArgs
    side_effects = "read"

    async def run(self, args: ListDirArgs, ctx: ToolContext) -> ToolResult:
        root = ctx.resolve_path(args.path)
        if not root.is_dir():
            return ToolResult.fail(f"not a directory: {args.path}")
        lines: list[str] = []
        count = 0
        base_depth = len(root.parts)
        for path in sorted(root.rglob("*")):
            rel_depth = len(path.parts) - base_depth
            if rel_depth > args.depth:
                continue
            if any(part in {".git", "__pycache__", "node_modules", ".venv"} for part in path.parts):
                continue
            rel = path.relative_to(root).as_posix()
            if args.glob and not fnmatch.fnmatch(rel, args.glob):
                continue
            indent = "  " * (rel_depth - 1)
            if path.is_dir():
                lines.append(f"{indent}{path.name}/")
            else:
                lines.append(f"{indent}{path.name} ({path.stat().st_size:,} B)")
            count += 1
            if count >= 500:
                lines.append("… [listing capped at 500 entries]")
                break
        return ToolResult(
            summary=f"{count} entries under {args.path}",
            content="\n".join(lines) or "(empty)",
            data={"entries": count},
        )


class ReadFileArgs(BaseModel):
    path: str
    offset: int = Field(default=0, ge=0, description="First line to read (0-based)")
    limit: int = Field(default=2000, ge=1, le=20000, description="Max lines to return")


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a text file with line numbers. Use offset/limit for long text. For PDF/DOCX/PPTX use read_pages instead."
    Args = ReadFileArgs
    side_effects = "read"

    async def run(self, args: ReadFileArgs, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve_path(args.path)
        if not path.is_file():
            return ToolResult.fail(f"no such file: {args.path}")
        if path.suffix.lower() in {".pdf", ".docx", ".pptx"}:
            return ToolResult.fail(
                f"{args.path} is a structured document, not a text file. "
                "Use read_pages with this path and pages '1' to start; "
                "text line counts are not document page counts."
            )
        if path.stat().st_size > MAX_READ_BYTES:
            return ToolResult.fail(
                f"{args.path} is {path.stat().st_size:,} B (>4 MB); page it with offset/limit "
                "or use grep to locate the region"
            )
        if _is_binary(path):
            return ToolResult.fail(
                f"{args.path} is binary ({path.stat().st_size:,} B); use file_info or view_image"
            )
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        window = lines[args.offset : args.offset + args.limit]
        remaining = max(0, len(lines) - args.offset - len(window))
        body = _numbered("\n".join(window), start=args.offset + 1)
        summary = (
            f"{args.path}: lines {args.offset + 1}-{args.offset + len(window)} of {len(lines)}"
        )
        if remaining:
            body += (
                f"\n… [{remaining} more lines; continue with offset={args.offset + len(window)}]"
            )
        return ToolResult(summary=summary, content=body, data={"total_lines": len(lines)})


class WriteFileArgs(BaseModel):
    path: str
    content: str
    overwrite: bool = Field(default=False, description="Must be true to replace an existing file")


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create a text file (parents auto-created). Set overwrite=true to replace."
    Args = WriteFileArgs
    side_effects = "write"
    idempotent = False

    async def run(self, args: WriteFileArgs, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve_path(args.path)
        if path.exists() and not args.overwrite:
            return ToolResult.fail(f"{args.path} exists; pass overwrite=true to replace it")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args.content, encoding="utf-8", newline="\n")
        return ToolResult(
            summary=f"wrote {args.path} ({len(args.content):,} chars)",
            data={"path": args.path, "bytes": len(args.content.encode())},
        )


class EditFileArgs(BaseModel):
    path: str
    old: str = Field(
        description="Exact text to replace (must match exactly once unless occurrence set)"
    )
    new: str
    occurrence: int | None = Field(
        default=None,
        ge=1,
        description="1-based occurrence to replace when `old` appears several times",
    )


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Exact-match text replacement in one file; refuses ambiguous matches and returns a diff."
    )
    Args = EditFileArgs
    side_effects = "write"
    idempotent = False

    async def run(self, args: EditFileArgs, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve_path(args.path)
        if not path.is_file():
            return ToolResult.fail(f"no such file: {args.path}")
        text = path.read_text(encoding="utf-8")
        count = text.count(args.old)
        if count == 0:
            return ToolResult.fail(
                f"the exact text was not found in {args.path}; read_file it and copy the text verbatim"
            )
        if count > 1 and args.occurrence is None:
            return ToolResult.fail(
                f"the text appears {count} times in {args.path}; pass occurrence=N to disambiguate"
            )
        if args.occurrence is not None:
            if args.occurrence > count:
                return ToolResult.fail(f"occurrence={args.occurrence} but only {count} matches")
            index = -1
            for _ in range(args.occurrence):
                index = text.index(args.old, index + 1)
            updated = text[:index] + args.new + text[index + len(args.old) :]
        else:
            updated = text.replace(args.old, args.new, 1)
        path.write_text(updated, encoding="utf-8", newline="\n")
        diff = _unified_diff(text, updated, args.path)
        return ToolResult(
            summary=f"edited {args.path} ({count if args.occurrence else 1} replacement)",
            content=diff,
            data={"diff": diff[:4000]},
        )


def _unified_diff(before: str, after: str, name: str) -> str:
    import difflib

    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
            n=3,
        )
    )


class ApplyPatchArgs(BaseModel):
    unified_diff: str = Field(
        description="Standard unified diff (---/+++/@@ hunks), one or more files"
    )


class ApplyPatchTool(Tool):
    name = "apply_patch"
    description = (
        "Apply a unified diff to workspace files; fails atomically if any hunk does not apply."
    )
    Args = ApplyPatchArgs
    side_effects = "write"
    idempotent = False

    async def run(self, args: ApplyPatchArgs, ctx: ToolContext) -> ToolResult:
        try:
            plans = _parse_patch(args.unified_diff)
        except ToolError as exc:
            return ToolResult.fail(str(exc))
        staged: list[tuple[Path, str]] = []
        for file_name, hunks in plans:
            path = ctx.resolve_path(file_name)
            original = path.read_text(encoding="utf-8") if path.exists() else ""
            try:
                patched = _apply_hunks(original, hunks)
            except ToolError as exc:
                return ToolResult.fail(f"{file_name}: {exc}")
            staged.append((path, patched))
        for path, patched in staged:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(patched, encoding="utf-8", newline="\n")
        names = ", ".join(str(p[0]) for p in plans[:5])
        return ToolResult(
            summary=f"patched {len(plans)} file(s): {names}",
            data={"files": [p[0] for p in plans]},
        )


Hunk = tuple[int, list[str]]  # (start line in original, hunk body lines with +/-/space prefixes)


def _parse_patch(diff: str) -> list[tuple[str, list[Hunk]]]:
    files: list[tuple[str, list[Hunk]]] = []
    current_file: str | None = None
    current_hunks: list[Hunk] = []
    lines = diff.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            if current_file is not None:
                files.append((current_file, current_hunks))
            i += 1
            if i >= len(lines) or not lines[i].startswith("+++ "):
                raise ToolError("malformed diff: '---' without '+++'")
            target = lines[i][4:].strip()
            current_file = re.sub(r"^b/", "", target.split("\t")[0])
            if current_file == "/dev/null":
                raise ToolError("file deletion via patch is not supported; use delete_file")
            current_hunks = []
        elif line.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not match or current_file is None:
                raise ToolError(f"malformed hunk header: {line}")
            start = int(match.group(1))
            body: list[str] = []
            i += 1
            while i < len(lines) and (lines[i][:1] in (" ", "+", "-") or lines[i] == ""):
                if lines[i].startswith(("--- ", "@@")):
                    break
                body.append(lines[i] if lines[i] else " ")
                i += 1
            current_hunks.append((start, body))
            continue
        i += 1
    if current_file is not None:
        files.append((current_file, current_hunks))
    if not files:
        raise ToolError("no file sections found in the diff")
    return files


def _apply_hunks(original: str, hunks: list[Hunk]) -> str:
    lines = original.splitlines()
    offset = 0
    for start, body in hunks:
        expected = [b[1:] for b in body if b[:1] in (" ", "-")]
        insert = [b[1:] for b in body if b[:1] in (" ", "+")]
        index = start - 1 + offset
        window = lines[index : index + len(expected)]
        if window != expected:
            found = _find_context(lines, expected)
            if found is None:
                raise ToolError(
                    f"hunk at line {start} does not apply (context mismatch); "
                    "re-read the file and regenerate the diff"
                )
            index = found
        lines[index : index + len(expected)] = insert
        offset += len(insert) - len(expected)
    return "\n".join(lines) + ("\n" if original.endswith("\n") or not original else "")


def _find_context(lines: list[str], expected: list[str]) -> int | None:
    if not expected:
        return None
    matches = [
        i for i in range(len(lines) - len(expected) + 1) if lines[i : i + len(expected)] == expected
    ]
    return matches[0] if len(matches) == 1 else None


class GlobArgs(BaseModel):
    pattern: str = Field(
        description="Glob like **/*.py or reports/*.xlsx, relative to the workspace"
    )


class GlobTool(Tool):
    name = "glob"
    description = "Find files by glob pattern; newest first."
    Args = GlobArgs
    side_effects = "read"

    async def run(self, args: GlobArgs, ctx: ToolContext) -> ToolResult:
        root = ctx.workspace.resolve()
        matches = [
            p
            for p in root.glob(args.pattern)
            if p.is_file()
            and not any(part in {".git", "node_modules", ".venv"} for part in p.parts)
        ]
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        rels = [p.relative_to(root).as_posix() for p in matches[:200]]
        return ToolResult(
            summary=f"{len(matches)} file(s) match {args.pattern}",
            content="\n".join(rels) or "(no matches)",
            data={"files": rels},
        )


class GrepArgs(BaseModel):
    pattern: str = Field(description="Regular expression to search for")
    path: str = Field(default=".", description="File or directory to search")
    regex: bool = Field(default=True, description="false = treat pattern as a literal string")
    context: int = Field(default=0, ge=0, le=10, description="Lines of context around each match")
    glob: str | None = Field(default=None, description="Only search files matching this glob")


class GrepTool(Tool):
    name = "grep"
    description = (
        "Search file contents by regex or literal; exact tags like P-101A need regex=false."
    )
    Args = GrepArgs
    side_effects = "read"

    async def run(self, args: GrepArgs, ctx: ToolContext) -> ToolResult:
        target = ctx.resolve_path(args.path)
        pattern = args.pattern if args.regex else re.escape(args.pattern)
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return ToolResult.fail(f"bad regex: {exc}")
        files = (
            [target]
            if target.is_file()
            else [
                p
                for p in sorted(target.rglob("*"))
                if p.is_file()
                and not any(
                    part in {".git", "node_modules", ".venv", "__pycache__"} for part in p.parts
                )
                and (
                    args.glob is None
                    or fnmatch.fnmatch(p.relative_to(target).as_posix(), args.glob)
                )
            ]
        )
        out: list[str] = []
        hits = 0
        for path in files[:2000]:
            if path.stat().st_size > MAX_READ_BYTES or _is_binary(path):
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            rel = (
                path.relative_to(ctx.workspace.resolve()).as_posix()
                if path != target
                else args.path
            )
            for line_no, line in enumerate(lines, 1):
                if compiled.search(line):
                    hits += 1
                    lo = max(0, line_no - 1 - args.context)
                    hi = min(len(lines), line_no + args.context)
                    for j in range(lo, hi):
                        marker = ":" if j == line_no - 1 else "-"
                        out.append(f"{rel}{marker}{j + 1}{marker}{lines[j][:300]}")
                    if hits >= 200:
                        out.append("… [capped at 200 matches]")
                        return ToolResult(
                            summary=f"200+ matches for /{args.pattern}/",
                            content="\n".join(out),
                            data={"matches": hits},
                        )
        return ToolResult(
            summary=f"{hits} match(es) for /{args.pattern}/",
            content="\n".join(out) or "(no matches)",
            data={"matches": hits},
        )


class MoveFileArgs(BaseModel):
    source: str
    dest: str
    overwrite: bool = False


class MoveFileTool(Tool):
    name = "move_file"
    description = "Move or rename a file within the workspace."
    Args = MoveFileArgs
    side_effects = "write"
    idempotent = False

    async def run(self, args: MoveFileArgs, ctx: ToolContext) -> ToolResult:
        source = ctx.resolve_path(args.source)
        dest = ctx.resolve_path(args.dest)
        if not source.exists():
            return ToolResult.fail(f"no such file: {args.source}")
        if dest.exists() and not args.overwrite:
            return ToolResult.fail(f"{args.dest} exists; pass overwrite=true")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(dest))
        return ToolResult(summary=f"moved {args.source} → {args.dest}")


class DeleteFileArgs(BaseModel):
    path: str


class DeleteFileTool(Tool):
    name = "delete_file"
    description = "Permanently delete one file. High risk; usually denied by policy."
    Args = DeleteFileArgs
    side_effects = "delete"
    risk = "high"
    idempotent = False

    async def run(self, args: DeleteFileArgs, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve_path(args.path)
        if not path.is_file():
            return ToolResult.fail(f"no such file: {args.path}")
        path.unlink()
        return ToolResult(summary=f"deleted {args.path}")


class FileInfoArgs(BaseModel):
    path: str


class FileInfoTool(Tool):
    name = "file_info"
    description = "Size, type, modified time and hash of one file."
    Args = FileInfoArgs
    side_effects = "read"

    async def run(self, args: FileInfoArgs, ctx: ToolContext) -> ToolResult:
        import hashlib
        from datetime import UTC, datetime

        path = ctx.resolve_path(args.path)
        if not path.exists():
            return ToolResult.fail(f"no such path: {args.path}")
        if path.is_dir():
            children = len(list(path.iterdir()))
            return ToolResult(
                summary=f"{args.path}: directory, {children} entries", data={"type": "dir"}
            )
        stat = path.stat()
        digest = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if stat.st_size < 64 * 1024 * 1024
            else "(large)"
        )
        info: dict[str, Any] = {
            "type": "binary" if _is_binary(path) else "text",
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            "sha256": digest,
            "suffix": path.suffix,
        }
        return ToolResult(summary=f"{args.path}: {info['type']}, {stat.st_size:,} B", data=info)
