# jev-rg-filter — JEV Context Sniper

A Claude Code JEV plugin that answers *"where is X implemented?"* without letting Claude
read fifteen files to find two functions.

## The problem

Ask Claude "where is the auth token validated in our backend?" and it greps, gets 40
hits, and reads whole files — most of which only mention `token` in an import, a mock,
or a comment. That is thousands of tokens of boilerplate spent locating three lines.

## The fix

Put a filter between ripgrep and the prompt:

```
question ──► ripgrep ──► candidate chunks ──► JEV (parallel) ──► top 2-3 snippets ──► Claude
                                                    │
                                        imports · mocks · comments  ──► dropped
```

Every candidate chunk is scored concurrently by [TypeSafe System One](https://typesafe.ai)
with three typed questions:

| Primitive | Question | Used for |
|---|---|---|
| `Score` | How directly does this chunk perform the asked-about logic? (5-level rubric, reported 0-100) | Ranking |
| `Choice` | `definition_and_logic` / `import_statement` / `test_mock` / `comment_only` | Filtering |
| `Noul` | Is this the implementation, not a caller or re-export? | Tie-breaking |

Only the survivors are printed. In practice a 60-chunk search resolves in about a
second, because every chunk is one concurrent call rather than one more file in the
context window.

## Install

```bash
/plugin marketplace add /path/to/jev-rg-filter
/plugin install jev-rg-filter@jev-rg-filter
```

Then set your key — either export it or drop it in a `.env` in the repo you're
searching (or in this plugin's directory):

```
TYPESAFE_API_KEY=apikey_...
```

The first run bootstraps a `.venv` in the plugin directory and installs
`typesafe-sdk`. Python 3.10+ required; ripgrep is used when present and a built-in
Python scanner takes over when it isn't.

## Use

Ask naturally — the `context-sniper` skill triggers on locate-and-explain questions:

> Where do we decide that an alert goes to a human analyst?

Or call it explicitly:

```
/jev-rg-filter:sniff where is the auth token validated
```

Or run it directly:

```bash
bash scripts/sniff.sh "where is the auth token validated" --path src/api --top 3
```

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--path` | `.` | Directory or file to search. |
| `--top` | `3` | Snippets to emit. |
| `--floor` | `40` | Minimum relevance (0-100) to keep a chunk. |
| `--term` | — | Extra search term, repeatable. |
| `--pattern` | — | Explicit ripgrep regex; overrides derived terms. |
| `--glob` | — | ripgrep glob filter, repeatable. |
| `--context` | `12` | Lines of context around a match. |
| `--max-lines` | `80` | Max lines per chunk. |
| `--max-chunks` | `60` | Max chunks sent to JEV. |
| `--concurrency` | `20` | Parallel JEV calls. |
| `--keep-tests` | off | Keep test/mock chunks. |
| `--engine` | `auto` | `rg`, `python`, or auto-detect. |
| `--json` | off | Machine-readable output with all scores. |
| `--env-file` | — | Explicit `.env` holding `TYPESAFE_API_KEY`. |

### Example

```
$ bash scripts/sniff.sh "Where is the phishing alert routed to a human analyst?" --path ../systemone-sec --top 2

## 1. soc_pipeline.py:66 — relevance 82/100 · definition_and_logic · implements 0.83
    ... probabilistic routing block ...

## 2. soc_pipeline.py:10 — relevance 85/100 · definition_and_logic · implements 0.64
    def action_route_to_analyst(alert_id: str, score: float): ...

---
11 candidate chunks scored in parallel in 0.7s; 2 passed.
~2,485 tokens of noise never entered the prompt.
```

## Performance

Measured by `scripts/benchmark.py`, which answers the same five questions over the same
repo twice — once reading whole matching files, once through the JEV filter — with the
same model answering both arms. Corpus: [python-kasa](https://github.com/python-kasa/python-kasa)
(268 Python files). Answering model: `llama3.1` on a local GPU. Methodology and how to
re-run it on your own repo: [BENCHMARK.md](BENCHMARK.md).

| Across 5 queries | Baseline (read the files) | Sniper (JEV filter) | Delta |
|---|---|---|---|
| Tokens into the answering model | 254,317 | 4,890 | **52× less** |
| Wall-clock to an answer | 910.3s | 29.0s | **31× faster** |
| Answers naming the right file | 4/5 | 4/5 | unchanged |
| Right code present in context | 5/5 | 4/5 | one miss |

Per query:

| Query | Baseline | Sniper |
|---|---|---|
| KLAP handshake authentication | 47,244 tok · 134.0s | 801 tok · 4.5s |
| AES session key derivation | 47,668 tok · 161.1s | 1,144 tok · 6.3s |
| Discovery UDP datagram parsing | 60,312 tok · 249.9s | 803 tok · 5.6s |
| Error code → exception | 60,014 tok · 249.8s | 1,327 tok · 6.6s |
| Credential hashing | 39,079 tok · 115.6s | 815 tok · 5.9s |

The baseline is capped at the 10 densest matching files, which is **generous** — an
uncapped grep-and-read would pull in 30-90 files on these queries.

### Where the time goes

The sniper is *slower* to prepare context (~3.1s vs ~2.0s) because it waits on a JEV
round trip. It wins downstream: the answering model reads ~1,000 tokens instead of
~50,000. Scoring 60 chunks concurrently costs about a second, because they are 60
parallel calls rather than 60 more things in the context window.

### Recall versus budget

`--top` trades retrieval against context size:

| `--top` | Tokens vs baseline | Right code retrieved |
|---|---|---|
| 3 (default) | 57× less | 4/5 |
| 5 | 37× less | **5/5 — matches the baseline** |
| 8 | 24× less | 5/5 |

If you would rather not lose a hit, `--top 5` matches the baseline's retrieval exactly
and still cuts context by 37×.

### The miss, in detail

One query fails honestly: *"How is an error code in a device response turned into an
exception?"*. JEV ranks `IotDevice._verify_emeter` — whose docstring reads "Raise an
exception if there is no emeter" — above the real `_handle_response_error_code`
conversions, which land at ranks 4 and 5. The relevance rubric was sharpened to reject
exactly this kind of surface match; scores dropped, but the ordering did not change.
Chunks that *describe themselves* in the question's vocabulary are the filter's
current weak spot.

Two honest caveats on the accuracy columns. First, "answer correct" is a literal
filename check, so a correct answer phrased without the filename scores as a miss.
Second, writing ground truth for "where is X?" is harder than it looks — three of these
five queries had their expected files corrected after reading the implementations
(`credentials.py` is only a dataclass; the AES session key is derived in KLAP, not
`aestransport.py`). Treat one run of five queries as an indication, not a law.

## Layout

```
.claude-plugin/plugin.json        plugin manifest
.claude-plugin/marketplace.json   local marketplace entry
commands/sniff.md                 /jev-rg-filter:sniff
skills/context-sniper/SKILL.md    auto-triggering skill
scripts/sniper.py                 the pipeline
scripts/sniff.sh                  interpreter resolver + venv bootstrap
scripts/benchmark.py              with/without-JEV speed comparison
BENCHMARK.md                      benchmark methodology
```

## Degradation

- **No API key** — chunks come back unranked and marked `unscored`; nothing crashes.
- **A failed JEV call** — that chunk is kept and flagged rather than silently dropped.
- **No ripgrep** — the built-in Python scanner runs instead.
