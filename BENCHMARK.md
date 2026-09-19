# Benchmark methodology

`scripts/benchmark.py` answers the same question twice, over the same corpus, with the
same answering model — once with the JEV filter and once without — and times both end to
end.

## The two arms

| | Baseline ("no JEV") | Sniper ("with JEV") |
|---|---|---|
| Search | ripgrep | ripgrep (identical, timed once and charged to both) |
| Context | the **10 densest matching files, in full** | chunk → score every chunk with JEV in parallel → keep the top 3 |
| Answer | local Ollama model | the same local Ollama model |

The baseline is a stand-in for what Claude does unaided: grep, then open the files that
matched. Capping it at 10 files is **generous** — an uncapped grep-and-read on these
queries would pull in 30-90 files.

## Why a local model answers

The comparison needs an answering step to be honest — context reduction only matters if
it does not cost you the answer. A local Ollama model (`llama3.1` by default) plays the
part of Claude for both arms. Its absolute latency is not the point; the *ratio* is,
because both arms pay the same per-token cost and differ only in how many tokens they
feed it.

## Accuracy is measured, not assumed

An arm that answers instantly from the wrong file has not won. Every query carries an
`expect` list of path fragments that count as correct, and the report tracks two things:

- **Code present** — the right code was in the context at all (measures retrieval).
- **Answer correct** — the answering model actually named it (measures the whole pipeline).

Some behaviors genuinely live in several places, so `expect` takes a list. Getting this
right matters: an early version of this benchmark marked two queries wrong because the
expected file was a dataclass rather than the code doing the work.

## Running it

```bash
# Full run, with the answering model
OLLAMA_URL=http://host:11434/api/chat \
  python scripts/benchmark.py --path ../python-kasa --model llama3.1:latest

# Context sizes and filter latency only - no answering model needed
python scripts/benchmark.py --path ../python-kasa --no-answer

# Your own questions
python scripts/benchmark.py --path . --queries my-queries.json --runs 3
```

`my-queries.json` is a list of `{"query": "...", "expect": ["file.py"]}` objects.

Useful flags: `--runs N` reports the median of N repeats, `--baseline-files N` changes
how generous the baseline is, `--top N` changes how many snippets the sniper keeps, and
`--json` emits the raw numbers including each arm's full answer text.

## Sweeping the snippet budget

`--top` trades recall against context size, and the sweep is worth running on your own
corpus before settling on a default:

```bash
for t in 3 5 8; do
  python scripts/benchmark.py --path ../python-kasa --no-answer --top $t \
    | grep -E "Right code|Tokens into"
done
```

## Caveats

- One corpus and five queries is an indication, not a law. Re-run it on your own repo.
- Ollama prefill speed depends on the host GPU; the token counts are hardware-independent,
  the seconds are not.
- The sniper arm's prep time includes the JEV round trip, so it is *slower* to prepare
  context. It wins because the answering model then has ~50x less to read.
