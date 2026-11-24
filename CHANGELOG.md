# Changelog

## 0.1.0 (2026-11-24)

First release.

- `record`: a sitecustomize and import hook wrap the public functions, methods, static/class
  methods, property getters and generators of the target modules, in the recorded process and in
  child Python processes. Each case stores the input pickled before the call, the output or
  exception, and the argument state after the call.
- Generators are recorded lazily: only consumed items are kept, and `send()` values are replayed.
  Returned `map`/`zip`/`islice` iterators are filled by re-running the original, up to 1,000 items,
  including an exception raised part-way through.
- Stabilisation: two fresh-process replays drop nondeterministic cases before they can cause a
  false alarm.
- `check`: one-subprocess replay with canonical comparison (Python `==` order semantics, type-exact,
  objects by state, stdlib objects by repr). Prints text, `--json` or `--markdown`. Exit 0/1/2/3, where
  3 means tampered.
- `strength`: AST mutants (comparisons, arithmetic, boolean ops, `not`, `if` negation, constants,
  strings, dropped method calls, `return None`, `break`/`continue`), run in parallel processes with a
  timeout and a memory cap. Survivors are listed with `file:line:col`.
- Claude Code: a Stop hook (blocks with exact diffs, stops re-blocking on unchanged code, at most 3
  blocks per session), a PreToolUse guard for `.refactor-anything/`, and a skill. `install claude` merges
  into existing settings.
- `go`: an agent loop for Claude Code, Codex or any command, with diff feedback and rejection of
  runs that edit the recording.
- Under `uvx`/`uv tool run`, the recorded command uses the user's interpreter and PATH, not the
  tool's, and hooks are pinned to a durable command.
