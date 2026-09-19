---
name: context-sniper
description: Locate where a behavior is implemented in a codebase without reading whole files. Use when the user asks "where is X", "how does X work", "what handles X", "find the code that does X", or asks you to explain a mechanism you have not opened yet — especially in a large or unfamiliar repo. Runs ripgrep, scores every hit with JEV (TypeSafe System One) in parallel, and returns only the 2-3 snippets that actually implement the behavior.
---

# Context Sniper

Plain grep answers "which files mention this word." That is the wrong question — it
drags imports, mocks and comment hits into the prompt. This skill answers "which code
*does* this thing" by scoring every candidate chunk before any of it reaches you.

## When to use it

Use it for **locate-then-explain** questions: "where is the auth token validated",
"how do we retry failed webhooks", "what decides when an alert escalates".

Do **not** use it when:

- You already know the file — just read it.
- The user wants an exhaustive list of every occurrence (use Grep; the sniper
  deliberately throws most matches away).
- The change is a rename or mechanical edit across many files (again, Grep).

## How to run it

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sniff.sh" "<the user's question, verbatim>"
```

Pass the question as written. The script derives its own search terms from it; the
phrasing is also what JEV scores each chunk against, so rewording it into keywords
makes the ranking *worse*, not better.

Useful flags:

| Flag | Use it when |
|---|---|
| `--path src/api` | The user named a subtree, or the repo is large. |
| `--term jwt --term bearer` | You know a domain identifier the question does not contain. |
| `--pattern '<regex>'` | You want to control the ripgrep search exactly. |
| `--glob '*.ts'` | The answer is certainly in one language. |
| `--top 5` | The behavior is likely spread across several call sites. |
| `--floor 25` | A first run returned nothing and you want to see weaker candidates. |
| `--keep-tests` | The question is about tests, fixtures, or mocks. |
| `--json` | You want the scores as data rather than as markdown. |

## Reading the output

Each snippet is labelled `relevance N/100 · code_type · implements P`:

- **relevance** — how directly the chunk performs the asked-about logic (JEV `Score`).
- **code_type** — `definition_and_logic` survives; `import_statement`, `test_mock` and
  `comment_only` are filtered out before you see them (JEV `Choice`).
- **implements** — probability that this is the implementation rather than a caller or
  re-export (JEV `Noul`).

The footer reports how many candidates were scored and roughly how many tokens of noise
were dropped.

## What to do with it

1. Answer directly from the returned snippets when they are sufficient — that is the
   point of the tool, and each snippet carries `file:line` so the user can jump to it.
2. Open a file with Read only when a snippet is genuinely truncated at an important
   boundary, or when you need to make an edit.
3. If nothing survives filtering, do not immediately fall back to reading files. Retry
   once with `--floor 20`, a `--term` you learned from the first attempt, or a wider
   `--path`. Then fall back to Grep.
4. Cite the scores when the ranking is close or the top hit is weak (under ~60), so the
   user knows how confident the filter was.

## Failure modes worth knowing

- **No API key** — the script still runs and returns unranked ripgrep chunks, each
  marked `unscored`. Say so rather than presenting them as ranked results.
- **A chunk marked `unscored (…)`** — that one JEV call failed; the chunk is kept
  rather than silently dropped, but its position is not meaningful.
- **`python-scan` engine in `--json` output** — ripgrep was not on PATH and the slower
  built-in scanner ran instead. Results are the same; large repos just take longer.
