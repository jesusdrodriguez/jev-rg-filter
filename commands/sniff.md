---
description: Find where something is implemented — ripgrep hits ranked by JEV, only the best snippets loaded.
argument-hint: <question, e.g. where is the auth token validated>
allowed-tools: Bash(bash:*), Read
---

Find and explain the code that answers this question: **$ARGUMENTS**

Run the Context Sniper rather than grepping and reading files yourself:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sniff.sh" "$ARGUMENTS"
```

Then:

1. Answer the question from the returned snippets. Cite each one as `file:line`.
2. Note the relevance scores when the top hit is weak (under ~60) or the top two are
   close — the user should know how confident the filter was.
3. Only use Read if a snippet is cut off at a boundary that actually matters. Do not
   re-read files the sniper already summarized.
4. If nothing survived filtering, retry once with `--floor 20` or a `--term` you
   picked up from the first run before falling back to a plain Grep.
