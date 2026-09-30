"""Syntax-aware splitting for source code.

The guarantee this module provides: **a class, method, function or import block
is never cut in half**. Python is handled with the stdlib :mod:`ast`; the
C-family (Java, JavaScript/TypeScript, C, C++, C#, Go, Rust, PHP, Swift, Kotlin)
is handled by :class:`BraceScanner`, a character-level scanner that tracks brace
depth while correctly skipping comments and string literals.

Chunk text stays byte-identical to the source: concatenating the chunks of a
file reproduces that file exactly. When a method must be lifted out of an
oversized class, the enclosing class header travels in ``Block.context`` (and
therefore in the leaf metadata) rather than being spliced into the content.
"""

from __future__ import annotations

import ast
import re
from typing import Dict, List, Optional, Tuple

from ..textutils import truncate
from .base import Block, Chunk, Chunker, join_lines, split_lines

#: File extension -> language identifier.
LANGUAGE_BY_EXTENSION: Dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".java": "java",
    ".js": "javascript",
    ".mjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".cs": "csharp",
    ".go": "go",
    ".rs": "rust",
    ".kt": "kotlin",
    ".swift": "swift",
    ".php": "php",
    ".scala": "scala",
}

#: Languages driven by the brace scanner.
BRACE_LANGUAGES = frozenset(LANGUAGE_BY_EXTENSION.values()) - {"python"}

_TYPE_DECL_RE = re.compile(
    r"\b(class|interface|enum|record|struct|namespace|trait|protocol|impl)\s+([A-Za-z_][\w$]*)"
)
_FUNC_DECL_RE = re.compile(r"\b(?:function|fn|func|sub)\s+([A-Za-z_][\w$]*)")
_METHOD_DECL_RE = re.compile(r"([A-Za-z_][\w$]*)\s*\([^;{()]*\)\s*(?:const\b)?[^;{]*\{")
_IMPORT_RE = re.compile(r"^\s*(?:import\b|#include\b|using\b|package\b|from\b|require\b|export\s+\*)")
_ANNOTATION_RE = re.compile(r"^\s*[@\[]")

#: Block kinds that may be split one level deeper when oversized.
_CONTAINER_KINDS = frozenset({"class", "interface", "enum", "record", "struct", "namespace", "impl", "trait"})


class BraceScanner:
    """Finds top-level declaration boundaries in C-family source.

    The scanner is deliberately lexical rather than a full parser: it only needs
    to know where a top-level ``{...}`` block closes, where a top-level
    statement ends in ``;``, and where a preprocessor line ends — while never
    being fooled by braces inside comments or string literals.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self._ends: Optional[List[int]] = None
        self._line_depths: Dict[int, int] = {}

    def unit_end_lines(self) -> List[int]:
        """Return the 1-based line numbers where top-level units end."""
        if self._ends is None:
            self._ends = self._scan()
        return self._ends

    def depth_at_line_ends(self) -> Dict[int, int]:
        """Brace depth at the end of each 1-based line.

        A chunk boundary at depth 0 sits between top-level declarations; at
        depth 1 it sits between the members of a class. Anything deeper would
        mean a method was cut in half — which the chunker never does, and which
        the test suite asserts against.
        """
        self.unit_end_lines()
        return dict(self._line_depths)

    def _scan(self) -> List[int]:
        """Single pass: records unit boundaries and per-line brace depth."""
        text = self.text
        length = len(text)
        ends: List[int] = []
        index = 0
        line = 1
        depth = 0
        has_content = False
        at_line_start = True

        def close_unit(at: int) -> None:
            nonlocal has_content
            if not ends or ends[-1] != at:
                ends.append(at)
            has_content = False

        def span_lines(from_line: int, to_line: int) -> None:
            """Record the (unchanged) depth of lines crossed by a skip."""
            for crossed in range(from_line, to_line):
                self._line_depths[crossed] = depth

        while index < length:
            char = text[index]
            nxt = text[index + 1] if index + 1 < length else ""

            if char == "\n":
                self._line_depths[line] = depth
                line += 1
                index += 1
                at_line_start = True
                continue

            if char in " \t\r":
                index += 1
                continue

            # ---- comments ------------------------------------------------
            if char == "/" and nxt == "/":
                index = self._skip_to_eol(index)
                continue
            if char == "/" and nxt == "*":
                previous_line = line
                index, line = self._skip_block_comment(index, line)
                span_lines(previous_line, line)
                continue

            # ---- preprocessor (C/C++): the line itself is the unit --------
            if char == "#" and at_line_start and depth == 0:
                end = self._skip_to_eol(index)
                consumed = text[index:end]
                line_end = line + consumed.count("\n")
                close_unit(line_end)
                span_lines(line, line_end)
                line = line_end
                index = end
                at_line_start = False
                continue

            # ---- string literals -----------------------------------------
            if text.startswith('"""', index):
                previous_line = line
                index, line = self._skip_delimited(index + 3, line, '"""')
                span_lines(previous_line, line)
                has_content = True
                at_line_start = False
                continue
            if char in "\"'`":
                previous_line = line
                index, line = self._skip_string(index, line, char)
                span_lines(previous_line, line)
                has_content = True
                at_line_start = False
                continue

            # ---- structure -----------------------------------------------
            at_line_start = False
            if char == "{":
                depth += 1
                has_content = True
            elif char == "}":
                depth = max(0, depth - 1)
                if depth == 0:
                    # Absorb a trailing ';' (C++ `};`) into the same unit.
                    close_unit(line)
            elif char == ";" and depth == 0:
                if has_content:
                    close_unit(line)
            else:
                has_content = True
            index += 1

        total_lines = self.text.count("\n") + (0 if self.text.endswith("\n") else 1)
        self._line_depths.setdefault(max(line, 1), depth)
        if has_content or not ends:
            if total_lines and (not ends or ends[-1] < total_lines):
                close_unit(max(total_lines, 1))
        return ends

    # ------------------------------------------------------------- scanning
    def _skip_to_eol(self, index: int) -> int:
        """Skip to the end of the logical line, honouring ``\\`` continuations."""
        text = self.text
        while index < len(text):
            if text[index] == "\\" and index + 1 < len(text) and text[index + 1] == "\n":
                index += 2
                continue
            if text[index] == "\n":
                return index
            index += 1
        return index

    def _skip_block_comment(self, index: int, line: int) -> Tuple[int, int]:
        end = self.text.find("*/", index + 2)
        end = len(self.text) if end == -1 else end + 2
        return end, line + self.text.count("\n", index, end)

    def _skip_string(self, index: int, line: int, quote: str) -> Tuple[int, int]:
        text = self.text
        cursor = index + 1
        while cursor < len(text):
            char = text[cursor]
            if char == "\\":
                cursor += 2
                continue
            if char == "\n":
                line += 1
                # An unterminated single-line literal: stop at the newline
                # rather than swallowing the rest of the file.
                if quote != "`":
                    return cursor + 1, line
            elif char == quote:
                return cursor + 1, line
            cursor += 1
        return cursor, line

    def _skip_delimited(self, index: int, line: int, closing: str) -> Tuple[int, int]:
        end = self.text.find(closing, index)
        end = len(self.text) if end == -1 else end + len(closing)
        return end, line + self.text.count("\n", index, end)


class CodeAwareChunker(Chunker):
    """Splits source files on syntactic boundaries only."""

    name = "code"

    def __init__(self, config, language: Optional[str] = None) -> None:
        super().__init__(config)
        self.language = language

    # ------------------------------------------------------------------ api
    @property
    def symbol_mode(self) -> bool:
        """One leaf per class member instead of packed runs of blocks."""
        return getattr(self.config, "code_chunk_mode", "pack") == "symbol"

    def _split_threshold(self) -> int:
        """Size above which a class is split into its members.

        In ``pack`` mode only oversized classes are split (block integrity
        first). In ``symbol`` mode any class with more than a couple of small
        members is, so that a lookup can return a single method and a project
        map can list them; tiny types (records, interfaces) stay whole.
        """
        if not self.symbol_mode:
            return self.config.max_tokens
        return max(60, 2 * max(24, self.config.target_tokens // 4))

    def chunk(self, text: str, *, source: str = "") -> List[Chunk]:  # type: ignore[override]
        """Split code on syntactic boundaries; see :meth:`Chunker.chunk`."""
        if not text.strip():
            return []
        blocks = self.blocks(text, source=source)
        return self.pack_each(blocks) if self.symbol_mode else self.pack(blocks)

    @staticmethod
    def language_for(source: str) -> Optional[str]:
        """Map a filename/extension to a language identifier."""
        if not source:
            return None
        suffix = source[source.rfind(".") :].lower() if "." in source else ""
        return LANGUAGE_BY_EXTENSION.get(suffix)

    def blocks(self, text: str, *, source: str = "") -> List[Block]:
        """Detect indivisible code blocks, choosing the strategy per language."""
        language = self.language or self.language_for(source) or self._sniff(text)
        if language == "python":
            try:
                blocks = self._python_blocks(text)
            except (SyntaxError, ValueError, RecursionError):
                # Broken or partial source still deserves structural chunking:
                # degrade to the lexical scanner, never to a blind character cut.
                blocks = self._brace_blocks(text, language="python")
        else:
            blocks = self._brace_blocks(text, language=language or "unknown")

        if not blocks:
            blocks = self._fallback_blocks(text)
        for block in blocks:
            block.tags = [t for t in block.tags if t] or block.tags
            if language:
                block.tags.append(f"lang:{language}")
        return blocks

    @staticmethod
    def _sniff(text: str) -> Optional[str]:
        """Guess the language of a snippet with no filename."""
        head = text[:4000]
        if re.search(r"^\s*(def|class)\s+\w+.*:\s*$", head, re.MULTILINE) and "{" not in head[:200]:
            return "python"
        if re.search(r"\bpublic\s+(final\s+|abstract\s+)?class\b|^\s*package\s+[\w.]+;", head, re.MULTILINE):
            return "java"
        if re.search(r"#include\s*[<\"]", head):
            return "cpp"
        if re.search(r"\b(function|const|let|=>)\b", head):
            return "javascript"
        return None

    # --------------------------------------------------------------- python
    def _python_blocks(self, text: str) -> List[Block]:
        """Block detection driven by the Python AST.

        Consecutive imports and consecutive simple statements are coalesced;
        every class and function becomes its own block, decorators included.
        Blocks tile the file with no gaps, so comments and blank lines between
        definitions are always carried by the following block.
        """
        tree = ast.parse(text)
        lines = split_lines(text)
        if not tree.body:
            return self._fallback_blocks(text)

        units: List[Dict[str, object]] = []
        for node in tree.body:
            start = node.lineno
            for decorator in getattr(node, "decorator_list", []) or []:
                start = min(start, decorator.lineno)
            end = getattr(node, "end_lineno", None) or start

            if isinstance(node, (ast.Import, ast.ImportFrom)):
                kind, name = "imports", "imports"
            elif isinstance(node, ast.ClassDef):
                kind, name = "class", node.name
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind, name = "function", node.name
            else:
                kind, name = "module", "module level"

            mergeable = kind in ("imports", "module")
            if units and mergeable and units[-1]["kind"] == kind:
                units[-1]["end"] = max(int(units[-1]["end"]), end)
                units[-1]["nodes"].append(node)  # type: ignore[union-attr]
            else:
                units.append({"start": start, "end": end, "kind": kind, "name": name, "nodes": [node]})

        blocks: List[Block] = []
        cursor = 1
        for unit in units:
            end = int(unit["end"])
            kind = str(unit["kind"])
            name = str(unit["name"])
            title = name if kind in ("imports", "module") else f"{kind} {name}"
            tags = [f"{kind}:{name}"] if kind not in ("imports", "module") else [kind]
            block = Block(
                text=join_lines(lines, cursor, end),
                title=title,
                start_line=cursor,
                end_line=end,
                kind=kind,
                tags=tags,
            )
            if kind == "class" and block.tokens > self._split_threshold():
                node = unit["nodes"][0]  # type: ignore[index]
                blocks.extend(self._split_python_class(lines, block, node))  # type: ignore[arg-type]
            else:
                blocks.append(block)
            cursor = end + 1

        if cursor <= len(lines) and blocks:
            trailing = join_lines(lines, cursor, len(lines))
            blocks[-1].text += trailing
            blocks[-1].end_line = len(lines)
        return blocks

    def _split_python_class(self, lines: List[str], block: Block, node: ast.ClassDef) -> List[Block]:
        """Split an oversized class into header + one block per member."""
        members = [
            m
            for m in node.body
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        if not members:
            return [block]

        def member_start(member: ast.AST) -> int:
            start = member.lineno  # type: ignore[attr-defined]
            for decorator in getattr(member, "decorator_list", []) or []:
                start = min(start, decorator.lineno)
            return start

        header_end = member_start(members[0]) - 1
        context = truncate(join_lines(lines, node.lineno, min(header_end, node.lineno + 2)), 200)
        out: List[Block] = []
        if header_end >= block.start_line:
            out.append(
                Block(
                    text=join_lines(lines, block.start_line, header_end),
                    title=f"class {node.name} (header)",
                    start_line=block.start_line,
                    end_line=header_end,
                    kind="class-header",
                    tags=[f"class:{node.name}"],
                )
            )
        cursor = max(header_end + 1, block.start_line)
        for index, member in enumerate(members):
            end = getattr(member, "end_lineno", None) or member_start(member)
            if index == len(members) - 1:
                end = block.end_line
            out.append(
                Block(
                    text=join_lines(lines, cursor, end),
                    title=f"{node.name}.{getattr(member, 'name', 'member')}",
                    start_line=cursor,
                    end_line=end,
                    kind="method",
                    tags=[f"class:{node.name}", f"method:{getattr(member, 'name', '')}"],
                    context=context,
                )
            )
            cursor = end + 1
        return out

    # ---------------------------------------------------------- brace family
    def _brace_blocks(self, text: str, *, language: str) -> List[Block]:
        """Block detection for C-family syntax via :class:`BraceScanner`."""
        lines = split_lines(text)
        if not lines:
            return []
        ends = BraceScanner(text).unit_end_lines()
        if not ends:
            return self._fallback_blocks(text)

        blocks: List[Block] = []
        cursor = 1
        for end in ends:
            end = min(end, len(lines))
            if end < cursor:
                continue
            body = join_lines(lines, cursor, end)
            kind, name = self._identify(body)
            block = Block(
                text=body,
                title=f"{kind} {name}".strip() if name else kind,
                start_line=cursor,
                end_line=end,
                kind=kind,
                tags=[f"{kind}:{name}"] if name else [kind],
            )
            if kind in _CONTAINER_KINDS and block.tokens > self._split_threshold():
                blocks.extend(self._split_container(lines, block, name))
            else:
                blocks.append(block)
            cursor = end + 1

        if cursor <= len(lines):
            if blocks:
                blocks[-1].text += join_lines(lines, cursor, len(lines))
                blocks[-1].end_line = len(lines)
            else:  # pragma: no cover - defensive
                blocks.append(
                    Block(
                        text=join_lines(lines, cursor, len(lines)),
                        title="source",
                        start_line=cursor,
                        end_line=len(lines),
                        kind="segment",
                    )
                )
        return blocks

    def _split_container(self, lines: List[str], block: Block, container: str) -> List[Block]:
        """Split an oversized class/struct/namespace into its members.

        The body is re-scanned with the same brace logic, so members are cut at
        their own closing braces and stay intact.
        """
        open_line = self._find_body_open_line(lines, block)
        if open_line is None or open_line >= block.end_line:
            return [block]

        body_start = open_line + 1
        body_end = block.end_line - 1
        if body_end < body_start:
            return [block]

        body_text = join_lines(lines, body_start, body_end)
        inner_ends = BraceScanner(body_text).unit_end_lines()
        if len(inner_ends) < 2:
            return [block]

        # The context is the declaration itself, not whatever documentation
        # precedes it: a long Javadoc would otherwise fill the budget and hide
        # the very line a reader needs ("class InventoryService {").
        context = truncate(join_lines(lines, max(block.start_line, open_line - 2), open_line), 200)
        out = [
            Block(
                text=join_lines(lines, block.start_line, open_line),
                title=f"{block.kind} {container} (header)".strip(),
                start_line=block.start_line,
                end_line=open_line,
                kind=f"{block.kind}-header",
                tags=[f"{block.kind}:{container}"] if container else [block.kind],
            )
        ]
        cursor = body_start
        for offset in inner_ends:
            end = min(body_start + offset - 1, body_end)
            if end < cursor:
                continue
            member_text = join_lines(lines, cursor, end)
            _, member_name = self._identify(member_text)
            out.append(
                Block(
                    text=member_text,
                    title=f"{container}.{member_name}" if member_name else f"{container} member",
                    start_line=cursor,
                    end_line=end,
                    kind="method",
                    tags=[t for t in (f"{block.kind}:{container}", f"method:{member_name}") if t],
                    context=context,
                )
            )
            cursor = end + 1
        # The closing brace of the container tails the last member.
        if cursor <= block.end_line:
            out[-1].text += join_lines(lines, cursor, block.end_line)
            out[-1].end_line = block.end_line
        return out

    @staticmethod
    def _find_body_open_line(lines: List[str], block: Block) -> Optional[int]:
        """Line number of the ``{`` that opens a container's body."""
        for line_no in range(block.start_line, block.end_line + 1):
            stripped = lines[line_no - 1]
            if "{" in stripped and not stripped.lstrip().startswith(("//", "*", "/*")):
                return line_no
        return None

    @staticmethod
    def _identify(body: str) -> Tuple[str, str]:
        """Infer ``(kind, name)`` from the declaration text of a block."""
        significant = [
            line.strip()
            for line in body.splitlines()
            if line.strip()
            and not line.strip().startswith(("//", "*", "/*", "*/"))
            and not _ANNOTATION_RE.match(line)
        ]
        head = " ".join(significant[:4])
        if not head:
            return "comment", ""
        if _IMPORT_RE.match(significant[0]) or significant[0].startswith("#"):
            return "imports", ""
        type_match = _TYPE_DECL_RE.search(head)
        if type_match:
            return type_match.group(1), type_match.group(2)
        func_match = _FUNC_DECL_RE.search(head)
        if func_match:
            return "function", func_match.group(1)
        method_match = _METHOD_DECL_RE.search(head)
        if method_match:
            return "method", method_match.group(1)
        if "=" in head or head.endswith(";"):
            return "declaration", ""
        return "block", ""
