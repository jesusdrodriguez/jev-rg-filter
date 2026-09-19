"""Context Sniper: ripgrep candidates in, JEV-ranked snippets out.

Pipeline:
  1. Derive search terms from the natural-language query (or take --pattern verbatim).
  2. Run ripgrep to find candidate match lines.
  3. Grow each match into a code chunk (enclosing definition when we can find one).
  4. Fire every chunk at JEV / TypeSafe System One *in parallel*, asking:
       relevance  Score  - does this chunk perform the logic the query asks about?
       code_type  Choice - definition_and_logic | import_statement | test_mock | comment_only
       implements Noul   - is this the actual implementation, not a caller or re-export?
  5. Drop the imports/mocks/comments, rank the rest, print only the top N snippets.

Only step 5's output ever reaches Claude's prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score
except ImportError:  # pragma: no cover - surfaced as a setup error, not a traceback
    print(
        "error: typesafe_sdk is not installed for this interpreter.\n"
        "       Install it with:  pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(2)


# ==========================================
# 1. QUERY -> RIPGREP PATTERN
# ==========================================
STOPWORDS = {
    "a", "about", "an", "and", "any", "are", "at", "backend", "be", "by", "can",
    "code", "codebase", "did", "do", "does", "explain", "find", "for", "from",
    "get", "happens", "how", "in", "into", "is", "it", "its", "me", "of", "on",
    "or", "our", "out", "project", "repo", "see", "show", "some", "that",
    "the", "their", "then", "there", "this", "to", "up", "use", "used", "uses",
    "using", "we", "what", "when", "where", "which", "who", "why", "with",
    "work", "works", "would", "you", "your",
}

# Verbs that describe what code *does*; worth searching for even though they are common.
KEEP_ANYWAY = {"validate", "verify", "parse", "sign", "encrypt", "decrypt", "hash", "render", "cache"}


def derive_terms(query: str, extra: list[str]) -> list[str]:
    """Pull identifier-ish search terms out of a natural-language question."""
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query)
    terms: list[str] = []
    for word in words:
        lowered = word.lower()
        if lowered in STOPWORDS and lowered not in KEEP_ANYWAY:
            continue
        if lowered not in terms:
            terms.append(lowered)
    # Stem the obvious verb endings so "validated" also matches "validate"/"validating".
    for term in list(terms):
        for suffix in ("ing", "ed", "es", "s"):
            if len(term) > len(suffix) + 3 and term.endswith(suffix):
                stem = term[: -len(suffix)]
                if stem not in terms:
                    terms.append(stem)
                break
    terms.extend(t for t in extra if t not in terms)
    return terms


def build_pattern(terms: list[str]) -> str:
    """A case-insensitive alternation; ripgrep handles camelCase and snake_case for free."""
    return "(?i)(" + "|".join(re.escape(t) for t in terms) + ")"


# ==========================================
# 2. RIPGREP
# ==========================================
DEFAULT_EXCLUDES = [
    "!**/node_modules/**", "!**/.venv/**", "!**/venv/**", "!**/dist/**",
    "!**/build/**", "!**/.git/**", "!**/*.min.js", "!**/*.lock",
    "!**/package-lock.json", "!**/__pycache__/**",
]


EXCLUDED_DIRS = {
    "node_modules", ".venv", "venv", "dist", "build", ".git", "__pycache__",
    ".next", ".tox", "target", "vendor", ".mypy_cache", ".pytest_cache",
}

# Only used by the pure-Python fallback, to stay off binaries and lockfiles.
CODE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs",
    ".java", ".kt", ".rb", ".php", ".cs", ".c", ".h", ".cpp", ".hpp", ".swift",
    ".scala", ".ex", ".exs", ".sh", ".bash", ".sql", ".vue", ".svelte",
}
MAX_FILE_BYTES = 1_000_000


def run_ripgrep(pattern: str, path: str, globs: list[str], max_count: int) -> dict[str, list[int]] | None:
    """Return {file: [1-indexed match lines]}, or None when ripgrep is unavailable."""
    rg = shutil.which("rg")
    if rg is None:
        return None
    cmd = [rg, "--json", "--max-count", str(max_count), "--max-filesize", "1M"]
    for glob in globs or []:
        cmd += ["--glob", glob]
    for exclude in DEFAULT_EXCLUDES:
        cmd += ["--glob", exclude]
    cmd += ["-e", pattern, path]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    except OSError:
        return None
    if proc.returncode not in (0, 1):  # 1 == no matches, which is not an error here
        print(f"error: ripgrep failed: {proc.stderr.strip()}", file=sys.stderr)
        raise SystemExit(2)

    hits: dict[str, list[int]] = {}
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event["data"]
        file_path = data["path"].get("text")
        if not file_path:
            continue
        hits.setdefault(file_path, []).append(data["line_number"])
    return hits


def python_scan(pattern: str, path: str, globs: list[str], max_count: int) -> dict[str, list[int]]:
    """Fallback scanner for machines without a ripgrep binary on PATH.

    Same contract as `run_ripgrep`. Slower, but it keeps the plugin working
    everywhere; only `--glob` include patterns are honored here.
    """
    regex = re.compile(pattern)
    includes = [g for g in globs if not g.startswith("!")]
    root = Path(path)
    files = [root] if root.is_file() else [
        p for p in root.rglob("*")
        if p.is_file() and not EXCLUDED_DIRS.intersection(p.parts)
    ]

    hits: dict[str, list[int]] = {}
    for file_path in files:
        if includes and not any(file_path.match(g) for g in includes):
            continue
        if not includes and file_path.suffix.lower() not in CODE_SUFFIXES:
            continue
        try:
            if file_path.stat().st_size > MAX_FILE_BYTES:
                continue
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matched = [
            i for i, line in enumerate(content.splitlines(), 1) if regex.search(line)
        ][:max_count]
        if matched:
            hits[str(file_path)] = matched
    return hits


def search(pattern: str, path: str, globs: list[str], max_count: int, engine: str) -> tuple[dict[str, list[int]], str]:
    if engine in ("auto", "rg"):
        hits = run_ripgrep(pattern, path, globs, max_count)
        if hits is not None:
            return hits, "ripgrep"
        if engine == "rg":
            print("error: ripgrep (rg) was not found on PATH.", file=sys.stderr)
            raise SystemExit(2)
    return python_scan(pattern, path, globs, max_count), "python-scan"


# ==========================================
# 3. MATCH LINES -> CODE CHUNKS
# ==========================================
BLOCK_START = re.compile(
    r"^\s*(?:@|async\s+def\s|def\s|class\s|func\s|fn\s|sub\s|"
    r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s|"
    r"(?:export\s+)?(?:public|private|protected|internal|static|final)\s|"
    r"(?:export\s+)?(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*(?:async\s*)?(?:\(|function)|"
    r"(?:export\s+)?(?:type|interface|struct|impl|trait|enum)\s)"
)


@dataclass
class Chunk:
    path: str
    start: int  # 1-indexed, inclusive
    end: int  # 1-indexed, inclusive
    text: str
    match_lines: list[int]
    # Filled in by the JEV pass:
    relevance: float = 0.0
    confidence: float = 0.0
    code_type: str = "unscored"
    implements: float = 0.0
    error: str | None = None
    drop_reason: str | None = None
    probabilities: dict = field(default_factory=dict)

    @property
    def rank(self) -> float:
        """Relevance dominates; the implementation signal breaks ties."""
        return self.relevance * 0.75 + self.implements * 100.0 * 0.25

    @property
    def location(self) -> str:
        return f"{self.path}:{self.start}-{self.end}"


def find_block_start(lines: list[str], match_index: int, max_up: int) -> int | None:
    """Walk up to the enclosing def/class/function line, or None if none is close enough."""
    match_line = lines[match_index]
    indent = len(match_line) - len(match_line.lstrip())
    for i in range(match_index, max(-1, match_index - max_up), -1):
        line = lines[i]
        if not line.strip():
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= indent and BLOCK_START.match(line):
            return i
        indent = min(indent, line_indent)
    return None


def build_chunks(hits: dict[str, list[int]], context: int, max_lines: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path, match_lines in hits.items():
        try:
            lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if not lines:
            continue

        # Merge match lines that sit close enough to share one chunk.
        groups: list[list[int]] = []
        for line_no in sorted(set(match_lines)):
            if groups and line_no - groups[-1][-1] <= context:
                groups[-1].append(line_no)
            else:
                groups.append([line_no])

        for group in groups:
            first_index = min(group) - 1
            block_start = find_block_start(lines, first_index, max_up=context * 2)
            # Prefer the enclosing definition; otherwise fall back to plain context.
            start_index = block_start if block_start is not None else max(0, first_index - context)
            end_index = min(len(lines) - 1, max(group) - 1 + context)
            if end_index - start_index + 1 > max_lines:
                end_index = start_index + max_lines - 1
            body = "\n".join(lines[start_index : end_index + 1])
            chunks.append(
                Chunk(
                    path=path,
                    start=start_index + 1,
                    end=end_index + 1,
                    text=body,
                    match_lines=group,
                )
            )
    return chunks


# ==========================================
# 4. THE JEV PASS (PARALLEL)
# ==========================================
RELEVANCE_RUBRIC = [
    "Irrelevant. The chunk has nothing to do with the question; the keyword appears incidentally.",
    "Adjacent. Same general subsystem, but this chunk does not do the thing asked about.",
    "Supporting. Calls into, configures, or wraps the requested logic without implementing it.",
    "Relevant. Implements a meaningful part of the logic the question asks about.",
    "Exact. This is the definition and body of the logic the question asks about.",
]

CODE_TYPES = {
    "definition_and_logic": "A function, method, class, or block that defines behavior or performs real logic.",
    "import_statement": "Only imports, requires, re-exports, or module-level symbol wiring.",
    "test_mock": "Test code, fixtures, mocks, stubs, or sample/fake data.",
    "comment_only": "The keyword appears only inside comments, docstrings, or string literals.",
}

QUESTIONS = {
    "relevance": Score(
        instructions="How directly does this code chunk perform the logic the developer's question asks about?",
        criteria=RELEVANCE_RUBRIC,
    ),
    "code_type": Choice(
        instructions="What kind of code is this chunk, judged by its substance and not by the file it lives in?",
        criteria=CODE_TYPES,
    ),
    "implements": Noul(
        instructions="Is this chunk the actual implementation of the behavior asked about, rather than a caller, a re-export, or a configuration of it?",
    ),
}

MAX_SCORE = len(RELEVANCE_RUBRIC) - 1


async def score_chunk(client: AsyncTypeSafeClient, query: str, chunk: Chunk, sem: asyncio.Semaphore) -> None:
    state = {
        "developer_question": query,
        "file": chunk.path,
        "lines": f"{chunk.start}-{chunk.end}",
        "code": chunk.text,
    }
    async with sem:
        try:
            response = await client.system_one(state=state, questions=QUESTIONS)
        except Exception as exc:  # network, auth, rate limit - keep the chunk, flag it
            chunk.error = f"{type(exc).__name__}: {exc}"
            return
    score_answer = response.scores["relevance"]
    choice_answer = response.choices["code_type"]
    chunk.relevance = 100.0 * score_answer.score / MAX_SCORE
    chunk.confidence = score_answer.confidence
    chunk.code_type = choice_answer.choice
    chunk.implements = response.nouls["implements"].noul
    chunk.probabilities = choice_answer.probabilities


async def score_all(query: str, chunks: list[Chunk], api_key: str, concurrency: int) -> None:
    sem = asyncio.Semaphore(concurrency)
    async with AsyncTypeSafeClient(api_key=api_key) as client:
        await asyncio.gather(*(score_chunk(client, query, chunk, sem) for chunk in chunks))


# ==========================================
# 5. SELECTION & OUTPUT
# ==========================================
NOISE_TYPES = {"import_statement", "test_mock", "comment_only"}


def select(chunks: list[Chunk], top: int, floor: float, keep_tests: bool) -> tuple[list[Chunk], list[Chunk]]:
    noise_types = NOISE_TYPES - ({"test_mock"} if keep_tests else set())
    kept, dropped = [], []
    for chunk in chunks:
        if chunk.error is not None:
            kept.append(chunk)  # never silently lose a chunk to an API failure
        elif chunk.code_type in noise_types:
            chunk.drop_reason = chunk.code_type
            dropped.append(chunk)
        elif chunk.relevance < floor:
            chunk.drop_reason = f"below relevance floor ({floor:.0f})"
            dropped.append(chunk)
        else:
            kept.append(chunk)
    kept.sort(key=lambda c: c.rank, reverse=True)
    for chunk in kept[top:]:
        chunk.drop_reason = "outranked"
    return kept[:top], dropped + kept[top:]


def fence_language(path: str) -> str:
    return {
        ".py": "python", ".js": "javascript", ".jsx": "jsx", ".ts": "typescript",
        ".tsx": "tsx", ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
        ".php": "php", ".cs": "csharp", ".c": "c", ".h": "c", ".cpp": "cpp",
        ".kt": "kotlin", ".swift": "swift", ".sh": "bash", ".sql": "sql",
    }.get(Path(path).suffix.lower(), "")


def render(query: str, kept: list[Chunk], dropped: list[Chunk], elapsed: float, cwd: str) -> str:
    def rel(path: str) -> str:
        try:
            return str(Path(path).resolve().relative_to(Path(cwd).resolve())).replace("\\", "/")
        except ValueError:
            return path.replace("\\", "/")

    considered_chars = sum(len(c.text) for c in kept + dropped)
    kept_chars = sum(len(c.text) for c in kept)
    out = [f"# Context Sniper: {query}", ""]

    if not kept:
        out.append("No chunk survived filtering. Every ripgrep hit was an import, a mock, a comment, or below the relevance floor.")
    for i, chunk in enumerate(kept, 1):
        if chunk.error:
            header = f"## {i}. {rel(chunk.path)}:{chunk.start} — unscored ({chunk.error})"
        else:
            header = (
                f"## {i}. {rel(chunk.path)}:{chunk.start} — relevance {chunk.relevance:.0f}/100"
                f" · {chunk.code_type} · implements {chunk.implements:.2f}"
            )
        out += [header, "", f"```{fence_language(chunk.path)}", chunk.text, "```", ""]

    total = len(kept) + len(dropped)
    saved_tokens = (considered_chars - kept_chars) // 4
    out += [
        "---",
        f"_{total} candidate chunk{'' if total == 1 else 's'} scored in parallel in {elapsed:.1f}s; "
        f"{len(kept)} passed. ~{saved_tokens:,} tokens of noise never entered the prompt._",
    ]
    if dropped:
        by_type: dict[str, int] = {}
        for chunk in dropped:
            reason = chunk.drop_reason or chunk.code_type
            by_type[reason] = by_type.get(reason, 0) + 1
        summary = ", ".join(f"{count} {name}" for name, count in sorted(by_type.items(), key=lambda kv: -kv[1]))
        out.append(f"_Filtered out: {summary}._")
    return "\n".join(out)


# ==========================================
# 6. ENTRY POINT
# ==========================================
def load_api_key(explicit_env_file: str | None, search_from: str) -> str | None:
    """Environment first, then .env files walking up from the search path, then the plugin dir."""
    key = os.getenv("TYPESAFE_API_KEY")
    if key and key.strip():
        return key.strip()

    candidates: list[Path] = []
    if explicit_env_file:
        candidates.append(Path(explicit_env_file))
    start = Path(search_from).resolve()
    for directory in [start, *start.parents][:6]:
        candidates.append(directory / ".env")
    candidates.append(Path(__file__).resolve().parent.parent / ".env")

    for candidate in candidates:
        try:
            content = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("TYPESAFE_API_KEY="):
                value = line.split("=", 1)[1].strip().strip("'\"")
                if value:
                    return value
    return None


def main() -> int:
    # Source files routinely contain emoji and non-Latin text; the Windows console
    # default (cp1252) would blow up while printing a perfectly good snippet.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="Rank ripgrep hits with JEV and print only the best snippets.")
    parser.add_argument("query", help="The natural-language question being answered.")
    parser.add_argument("--path", default=".", help="Directory or file to search (default: .).")
    parser.add_argument("--pattern", help="Explicit ripgrep regex; overrides terms derived from the query.")
    parser.add_argument("--term", action="append", default=[], help="Extra search term (repeatable).")
    parser.add_argument("--glob", action="append", default=[], help="ripgrep --glob filter (repeatable).")
    parser.add_argument("--top", type=int, default=3, help="Snippets to emit (default: 3).")
    parser.add_argument("--context", type=int, default=12, help="Lines of context around a match (default: 12).")
    parser.add_argument("--max-lines", type=int, default=80, help="Max lines per chunk (default: 80).")
    parser.add_argument("--max-chunks", type=int, default=60, help="Max chunks sent to JEV (default: 60).")
    parser.add_argument("--max-count", type=int, default=12, help="Max ripgrep matches per file (default: 12).")
    parser.add_argument("--concurrency", type=int, default=20, help="Parallel JEV calls (default: 20).")
    parser.add_argument("--floor", type=float, default=40.0, help="Minimum relevance to keep, 0-100 (default: 40).")
    parser.add_argument("--keep-tests", action="store_true", help="Do not filter out test/mock chunks.")
    parser.add_argument("--engine", choices=("auto", "rg", "python"), default="auto",
                        help="Candidate scanner: ripgrep, the built-in Python scanner, or auto (default).")
    parser.add_argument("--env-file", help="Path to a .env file holding TYPESAFE_API_KEY.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of markdown.")
    args = parser.parse_args()

    terms = derive_terms(args.query, args.term)
    if not args.pattern and not terms:
        print("error: no usable search terms in the query; pass --pattern or --term.", file=sys.stderr)
        return 2
    pattern = args.pattern or build_pattern(terms)

    hits, engine = search(pattern, args.path, args.glob, args.max_count, args.engine)
    if not hits:
        print(f"No matches for pattern: {pattern}")
        return 0

    chunks = build_chunks(hits, args.context, args.max_lines)
    # Densest chunks first, so the cap keeps the most promising candidates.
    chunks.sort(key=lambda c: len(c.match_lines), reverse=True)
    truncated = len(chunks) - args.max_chunks
    chunks = chunks[: args.max_chunks]

    api_key = load_api_key(args.env_file, args.path)
    started = time.perf_counter()
    if api_key:
        asyncio.run(score_all(args.query, chunks, api_key, args.concurrency))
    else:
        for chunk in chunks:
            chunk.error = "no TYPESAFE_API_KEY found; returning unranked ripgrep hits"
    elapsed = time.perf_counter() - started

    kept, dropped = select(chunks, args.top, args.floor, args.keep_tests)

    if args.json:
        print(json.dumps({
            "query": args.query,
            "pattern": pattern,
            "engine": engine,
            "elapsed_seconds": round(elapsed, 3),
            "candidates": len(chunks) + max(0, truncated),
            "scored": len(chunks),
            "results": [
                {
                    "path": c.path, "start": c.start, "end": c.end,
                    "relevance": round(c.relevance, 1), "confidence": round(c.confidence, 3),
                    "code_type": c.code_type, "implements": round(c.implements, 3),
                    "rank": round(c.rank, 1), "error": c.error, "code": c.text,
                }
                for c in kept
            ],
        }, indent=2))
    else:
        print(render(args.query, kept, dropped, elapsed, args.path))
        if truncated > 0:
            print(f"_{truncated} lower-density chunks were capped before scoring; raise --max-chunks to include them._")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
