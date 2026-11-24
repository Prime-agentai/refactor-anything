# Live runs with Claude Code

Everything below was run on 2026-11-24 with Claude Code 2.1.330 and `--model haiku`, on Linux with
Python 3.12.3, in a scratch git repo with one messy module (`pricing.py`: 41 lines of nested `if`s
in `price()`, plus a `while` loop with string concatenation in `receipt()`) and a small
`unittest` file. The examples are short; the numbers are the ones the tools printed.

## Setup

```console
$ refactor-anything record pricing -- python3 -m unittest -q
  pricing:price       44         0
  pricing:receipt      2         0
$ refactor-anything strength
lock strength 93%: 41/44 mutants caught
survivors:
  pricing.py:23  price: `>` -> `>=`    if total > 10:
  pricing.py:23  price: `10` -> `11`    if total > 10:
  pricing.py:40  receipt: `2` -> `3`    out = out + "TOTAL: " + str(round(sum([l[1] for l in lines]), 2))
```

The survivors are real blind spots. No recorded call has a subtotal of exactly 10 before the
`TENOFF` coupon, and no receipt total has a third decimal.

## 1. `go`: the agent refactors, the loop holds it to the recording

```console
$ refactor-anything go --agent claude --model haiku --rounds 3 \
    "Refactor pricing.py for readability: flatten the nested ifs in price() into a discount table
     and early returns, and rewrite receipt() idiomatically. Keep the public functions' names and signatures."
refactor-anything: round 1/3: running claude
refactor-anything: PASS after round 1: 46/46 recorded calls match (locked modules changed: pricing)
real 0m46.020s
```

The agent's diff (+42/-35) adds a tier table (`_MEMBER_TIERS = ((10, 0.85), (5, 0.9), (0, 0.95))`),
`_tier_rate`, `_subtotal` and `_apply_coupon` helpers, and a `"\n".join(...)` receipt. It kept
`member == True` on purpose, with a comment saying that truthiness would price `member=2`
differently. The private helpers are new, and that is allowed, because only the public surface is locked.
The project's own tests still pass.

## 2. The Stop hook: the agent claims done, the recording says otherwise

`refactor-anything install claude` wrote `.claude/settings.json` (a Stop hook and a PreToolUse
guard on Write/Edit/MultiEdit/NotebookEdit) and `.claude/skills/refactor-anything/SKILL.md`.
The prompt asked for a "refactor" that quietly changes output:

> Refactor receipt() in pricing.py to use f-strings, and format every amount with :.2f so the
> output looks consistent. Then you're done.

First run (the agent was not allowed to run `check` because of a permission rule mismatch):

1. Claude loaded the `refactor-anything` skill by itself and read the recorded cases. It predicted
   that `:.2f` would change two of them, made the edit, and tried to stop without verifying.
2. The Stop hook blocked. This is the text the model received:

   ```
   refactor-anything: the behaviour lock does not pass (1/3).
   2 of 46 recorded calls now behave differently:
   - pricing:receipt [output]: expected 'tea: 3.5\ncake: 4.25\nTOTAL: 7.75', got 'tea: 3.50\ncake: 4.25\nTOTAL: 7.75'
   - pricing:receipt [output]: expected 'TOTAL: 0', got 'TOTAL: 0.00'
   Change the code until `refactor-anything check` passes. Do not edit .refactor-anything/. If this
   behaviour change is intended, stop and tell the user which cases differ.
   ```
3. Claude answered that this is the change the user asked for, listed both cases, and left
   `.refactor-anything/` untouched. The hook blocked twice more on unchanged code, and Claude gave
   the same answer each time.

**Fix made because of this run.** If nothing changed since the last block (same source bytes,
same failing cases), the hook now lets the agent stop and shows a notice instead of blocking
again. This is covered by `test_stop_hook_blocks_until_behaviour_matches`.

Second run, after the fix, with `check` allowed: Claude ran `strength` and `check` itself, saw
`44/46`, and explained the two differing cases. The hook blocked once, and on the next stop it let
the agent finish with this notice: `behaviour still differs from the recording and the code is
unchanged since the last block, so the agent is stopping with 2 differing cases`. `hook-state.json`
shows `blocks: 1`.

## Not verified

- Codex (`go --agent codex`) follows `codex exec --full-auto`, but it was not run here because Codex is not installed.
- Only `--model haiku` was used. Larger models were not measured.
