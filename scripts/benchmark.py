"""Head-to-head: answering a locate-and-explain question with and without the JEV filter.

Two arms, same question, same corpus, same answering model:

  baseline  ripgrep -> read every matching file in full -> answer
  sniper    ripgrep -> chunk -> JEV scores in parallel -> top N snippets -> answer

Both arms are timed end to end. The answering stage is a local Ollama model standing in
for Claude, so the comparison measures what actually differs: how many tokens the
answering model has to ingest before it can say anything.

Ground truth is checked too - an arm that answers fast but names the wrong file has not
won anything.

Usage:
    python scripts/benchmark.py --path ../python-kasa
    python scripts/benchmark.py --path ../python-kasa --no-answer   # context sizes only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sniper  # noqa: E402

try:
    import urllib.request
except ImportError:  # pragma: no cover
    urllib = None  # type: ignore


# ==========================================
# BENCHMARK QUERIES
# ==========================================
# `expect` lists the path fragments that count as a correct answer, used to check that
# speed did not come at the cost of landing on the wrong code. Several entries mean the
# behavior genuinely lives in more than one defensible place.
DEFAULT_QUERIES = [
    {
        "query": "Where is the KLAP handshake authentication performed?",
        "expect": ["klaptransport.py"],
    },
    {
        # KLAP derives the AES key outright (sha256(b"lsk" + seeds + hash)[:16]);
        # AesTransport establishes an AES session from an RSA-wrapped device key.
        "query": "How is the AES session key derived during the handshake?",
        "expect": ["klaptransport.py", "aestransport.py"],
    },
    {
        "query": "Where does device discovery parse the UDP datagram response?",
        "expect": ["discover.py"],
    },
    {
        # The conversion lives in _handle_response_error_code (several transports and
        # protocols) and in SmartErrorCode.from_int; any of them is a correct answer.
        "query": "How is an error code in a device response turned into an exception?",
        "expect": ["exceptions.py", "smartprotocol.py", "smartcamprotocol.py",
                   "aestransport.py", "sslaestransport.py", "ssltransport.py"],
    },
    {
        # credentials.py is only a dataclass. The hashing is generate_auth_hash (KLAP)
        # and hash_credentials (the AES and SSL transports) - all verified by reading them.
        "query": "Where are the device credentials hashed for authentication?",
        "expect": ["klaptransport.py", "aestransport.py", "ssltransport.py",
                   "sslaestransport.py"],
    },
]


@dataclass
class ArmResult:
    name: str
    context_chars: int
    context_units: int  # files for baseline, chunks for sniper
    prep_seconds: float  # search + read, or search + chunk + JEV
    answer_seconds: float
    prompt_tokens: int | None
    answer: str
    context_hit: bool  # the right file was in the context at all
    answer_hit: bool  # the answering model actually named it

    @property
    def total_seconds(self) -> float:
        return self.prep_seconds + self.answer_seconds

    @property
    def est_tokens(self) -> int:
        return self.prompt_tokens if self.prompt_tokens else self.context_chars // 4


# ==========================================
# ARM A: NO FILTER (READ THE MATCHING FILES)
# ==========================================
def baseline_context(hits: dict[str, list[int]], max_files: int) -> tuple[str, int]:
    """What a naive grep-then-read loop puts in the prompt: whole files, densest first."""
    ranked = sorted(hits.items(), key=lambda kv: len(kv[1]), reverse=True)[:max_files]
    parts = []
    for path, _ in ranked:
        try:
            body = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parts.append(f"### {path}\n{body}")
    return "\n\n".join(parts), len(ranked)


# ==========================================
# ARM B: THE JEV FILTER
# ==========================================
def sniper_context(
    query: str,
    terms: list[str],
    hits: dict[str, list[int]],
    api_key: str | None,
    top: int,
    concurrency: int,
    max_chunks: int,
) -> tuple[str, int]:
    chunks = sniper.build_chunks(hits, context=12, max_lines=80)
    chunks = sniper.prioritize(chunks, terms)[:max_chunks]
    if api_key:
        asyncio.run(sniper.score_all(query, chunks, api_key, concurrency))
    kept, _ = sniper.select(chunks, top=top, floor=40.0, keep_tests=False)
    parts = [f"### {c.path}:{c.start}-{c.end}\n{c.text}" for c in kept]
    return "\n\n".join(parts), len(kept)


# ==========================================
# THE ANSWERING MODEL (STANDS IN FOR CLAUDE)
# ==========================================
SYSTEM_PROMPT = (
    "You are a code navigation assistant. Using ONLY the provided code, answer where the "
    "behavior is implemented. Name the file and function. Be brief: three sentences at most."
)


def ask_ollama(url: str, model: str, query: str, context: str, timeout: float) -> tuple[str, float, int | None]:
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Question: {query}\n\nCode:\n{context}"},
        ],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 200},
    }).encode()

    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except Exception as exc:
        return f"<answering model failed: {type(exc).__name__}: {exc}>", time.perf_counter() - start, None
    elapsed = time.perf_counter() - start
    return payload.get("message", {}).get("content", ""), elapsed, payload.get("prompt_eval_count")


# ==========================================
# RUNNER
# ==========================================
def run_query(case: dict, args: argparse.Namespace, api_key: str | None) -> tuple[ArmResult, ArmResult]:
    query = case["query"]
    expect = case["expect"] if isinstance(case["expect"], list) else [case["expect"]]

    # The search itself is shared by both arms; time it once and charge it to both.
    search_start = time.perf_counter()
    terms = sniper.derive_terms(query, [])
    pattern = sniper.build_pattern(terms)
    hits, _engine, terms = sniper.search(
        pattern, args.path, [], args.max_count, args.engine, terms=terms
    )
    search_seconds = time.perf_counter() - search_start

    results = []
    for name in ("baseline", "sniper"):
        prep_start = time.perf_counter()
        if name == "baseline":
            context, units = baseline_context(hits, args.baseline_files)
        else:
            context, units = sniper_context(
                query, terms, hits, api_key, args.top, args.concurrency, args.max_chunks
            )
        prep_seconds = search_seconds + (time.perf_counter() - prep_start)

        answer, answer_seconds, prompt_tokens = ("", 0.0, None)
        if args.answer and context:
            answer, answer_seconds, prompt_tokens = ask_ollama(
                args.ollama_url, args.model, query, context, args.answer_timeout
            )

        results.append(ArmResult(
            name=name,
            context_chars=len(context),
            context_units=units,
            prep_seconds=prep_seconds,
            answer_seconds=answer_seconds,
            prompt_tokens=prompt_tokens,
            answer=answer,
            context_hit=any(fragment in context for fragment in expect),
            answer_hit=any(fragment in answer for fragment in expect),
        ))
    return results[0], results[1]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="Compare answering speed with and without the JEV filter.")
    parser.add_argument("--path", required=True, help="Repository to benchmark against.")
    parser.add_argument("--queries", help="JSON file of [{query, expect}] cases; defaults to the built-in set.")
    parser.add_argument("--runs", type=int, default=1, help="Repeats per query; the median is reported.")
    parser.add_argument("--top", type=int, default=3, help="Snippets the sniper arm keeps (default: 3).")
    parser.add_argument("--baseline-files", type=int, default=10,
                        help="Whole files the baseline arm reads (default: 10). This is generous to the "
                             "baseline: an uncapped grep-and-read would pull in far more.")
    parser.add_argument("--max-chunks", type=int, default=60)
    parser.add_argument("--max-count", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--engine", choices=("auto", "rg", "python"), default="auto")
    parser.add_argument("--answer", dest="answer", action="store_true", default=True,
                        help="Run the answering model (default).")
    parser.add_argument("--no-answer", dest="answer", action="store_false",
                        help="Measure context size and filter time only.")
    parser.add_argument("--ollama-url", default=os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat"))
    parser.add_argument("--model", default=os.getenv("OLLAMA_MODEL", "llama3.1:latest"))
    parser.add_argument("--answer-timeout", type=float, default=600.0)
    parser.add_argument("--json", dest="as_json", action="store_true", help="Emit JSON instead of markdown.")
    args = parser.parse_args()

    # The benchmark's own .env lookup mirrors the tool's.
    api_key = sniper.load_api_key(None, args.path)
    if not api_key:
        print("warning: no TYPESAFE_API_KEY found; the sniper arm will be unranked.", file=sys.stderr)

    cases = json.loads(Path(args.queries).read_text(encoding="utf-8")) if args.queries else DEFAULT_QUERIES

    rows = []
    for case in cases:
        print(f"[bench] {case['query']}", file=sys.stderr)
        runs = [run_query(case, args, api_key) for _ in range(args.runs)]

        def median_of(arm_index: int, attr: str) -> float:
            return statistics.median(getattr(run[arm_index], attr) for run in runs)

        baseline, snipe = runs[-1]
        rows.append({
            "query": case["query"],
            "expect": case["expect"],
            "baseline": {
                "files": baseline.context_units,
                "tokens": baseline.est_tokens,
                "prep_s": round(median_of(0, "prep_seconds"), 2),
                "answer_s": round(median_of(0, "answer_seconds"), 2),
                "total_s": round(median_of(0, "total_seconds"), 2),
                "context_hit": baseline.context_hit,
                "answer_hit": baseline.answer_hit,
                "answer": baseline.answer.strip(),
            },
            "sniper": {
                "chunks": snipe.context_units,
                "tokens": snipe.est_tokens,
                "prep_s": round(median_of(1, "prep_seconds"), 2),
                "answer_s": round(median_of(1, "answer_seconds"), 2),
                "total_s": round(median_of(1, "total_seconds"), 2),
                "context_hit": snipe.context_hit,
                "answer_hit": snipe.answer_hit,
                "answer": snipe.answer.strip(),
            },
        })

    if args.as_json:
        print(json.dumps({"path": args.path, "model": args.model, "runs": args.runs, "results": rows}, indent=2))
        return 0

    # --- markdown report ---
    def total(arm: str, key: str) -> float:
        return sum(row[arm][key] for row in rows)

    print(f"\n# JEV filter benchmark\n")
    print(f"Corpus: `{args.path}` · answering model: `{args.model}` · runs per query: {args.runs}")
    print(f"Baseline reads up to {args.baseline_files} whole files; sniper keeps {args.top} snippets.\n")
    mark = lambda ok: "✅" if ok else "❌"
    print("| Query | Arm | Context | Tokens in | Prep | Answer | Total | Code present | Answer correct |")
    print("|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        short = row["query"][:44] + ("…" if len(row["query"]) > 44 else "")
        b, s = row["baseline"], row["sniper"]
        answered = args.answer
        for label, arm, unit in (("baseline", b, "files"), ("sniper", s, "chunks")):
            head = f"| {short} " if label == "baseline" else "| "
            print(f"{head}| {label} | {arm[unit]} {unit} | {arm['tokens']:,} | {arm['prep_s']}s | "
                  f"{arm['answer_s']}s | **{arm['total_s']}s** | {mark(arm['context_hit'])} | "
                  f"{mark(arm['answer_hit']) if answered else 'n/a'} |")

    b_tokens, s_tokens = total("baseline", "tokens"), total("sniper", "tokens")
    b_total, s_total = total("baseline", "total_s"), total("sniper", "total_s")
    b_hits = sum(1 for row in rows if row["baseline"]["answer_hit"])
    s_hits = sum(1 for row in rows if row["sniper"]["answer_hit"])
    b_ctx = sum(1 for row in rows if row["baseline"]["context_hit"])
    s_ctx = sum(1 for row in rows if row["sniper"]["context_hit"])

    print(f"\n**Totals across {len(rows)} queries**\n")
    print("| | Baseline | Sniper | Delta |")
    print("|---|---|---|---|")
    print(f"| Tokens into the answering model | {b_tokens:,} | {s_tokens:,} | "
          f"{b_tokens / s_tokens:.1f}x less |" if s_tokens else "")
    print(f"| Wall-clock to answer | {b_total:.1f}s | {s_total:.1f}s | "
          f"{b_total / s_total:.1f}x faster |" if s_total else "")
    print(f"| Right code present in context | {b_ctx}/{len(rows)} | {s_ctx}/{len(rows)} | |")
    if args.answer:
        print(f"| Answers naming the right file | {b_hits}/{len(rows)} | {s_hits}/{len(rows)} | |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
