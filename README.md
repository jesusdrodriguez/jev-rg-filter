# jev-rg-filter — Context Sniper

A Claude Code plugin that answers *"where is X implemented?"* without letting Claude
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

## Layout

```
.claude-plugin/plugin.json        plugin manifest
.claude-plugin/marketplace.json   local marketplace entry
commands/sniff.md                 /jev-rg-filter:sniff
skills/context-sniper/SKILL.md    auto-triggering skill
scripts/sniper.py                 the pipeline
scripts/sniff.sh                  interpreter resolver + venv bootstrap
```

## Degradation

- **No API key** — chunks come back unranked and marked `unscored`; nothing crashes.
- **A failed JEV call** — that chunk is kept and flagged rather than silently dropped.
- **No ripgrep** — the built-in Python scanner runs instead.
