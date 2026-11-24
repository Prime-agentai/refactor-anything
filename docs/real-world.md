# Real world: more-itertools

[more-itertools](https://github.com/more-itertools/more-itertools) (MIT) at commit `aa99776`
(2026-11-23), `more_itertools/recipes.py` (1,617 lines), recorded from its own unittest suite.
The run was on Linux, Python 3.12.3, with 6 CPUs, on 2026-11-24.

```console
$ refactor-anything record more_itertools.recipes -- python3 -m unittest discover -s tests -t .
recorded 1717 new calls (51 unstable, left out)      # wall time 59.3 s; the suite alone runs 936 tests in 52.0 s
$ refactor-anything check
PASS  1666/1666 recorded calls match in 0.94s         # wall time 1.56 s
```

What the recording found on its own:

| | |
|---|---|
| Functions locked | 55 public recipes. `tabulate` is not locked: every test calls it with a lambda, and lambdas cannot be pickled. |
| Nondeterministic, left out | `random_permutation`, `random_product` and `random_combination_with_replacement` differed on every replay. One case each of `random_combination` and `random_derangement` was left out as well. |
| Unstable cases | 42 of 200 `iter_index` cases. The test mutates its `bytearray` input *while* iterating, so the argument state after the call depends on the caller, not on the function. |
| Generators | Recorded lazily: only the items the caller consumed, in order, plus whether the generator stopped. |
| Returned iterators | `grouper`, `unique_justseen`, `reshape`, `transpose` and others return `zip`/`map`/`islice` objects. Their items were filled in by re-running the original on the recorded input, up to 1,000 items. `grouper(..., incomplete="strict")` raises part-way through, and that is locked too. |
| Unpicklable inputs | These are listed per function in the record output (lambdas, generators, test-local classes). Those calls are skipped and every other call is kept. |

Lock strength for seven recipes (`strength --function ...`, 83 mutants, 50.8 s wall on 6 CPUs):

```
   71%  factor          12/17
  100%  grouper          9/9
   91%  running_median  10/11
   95%  sieve           20/21
   81%  sliding_window  13/16
  100%  totient          3/3
  100%  unique_justseen  6/6
lock strength 88%: 73/83 mutants caught
```

Of the ten survivors, six are *equivalent mutants*: the change cannot alter what the function returns.
One is a branch the tests never reach. Three are real blind spots, and the upstream test suite
shares them.

- `factor`, line 1098: `if n < 211**2 or is_prime(n)`. **`<` → `<=`, `211` → `212` and `211**2` →
  `211**3` are real bugs** that nothing recorded and no upstream test notices. With the first
  two, `factor(44521)` returns `[44521]` instead of `[211, 211]`. With the third, `factor(47053)`
  (211 × 223) returns `[47053]`. This was checked by executing each mutant against the original for
  n = 2..2999 and for those two inputs. The tests factor numbers whose remainder after trial
  division is below 211² or far above 211³, and nothing in between. The fix for the lock is one
  `record` of a few such numbers.
- `factor`, lines 1096/1098: `n > 1` → `n > 2` and `211**2` → `211*2` are equivalent. After trial
  division by every prime below 211, the remainder is 1 or at least 211, so neither change matters.
- `sliding_window`, lines 817/819: `if n > 20` → `>=` / `21`, and `elif n > 2` → `>=`. These are equivalent:
  `n` only picks between three algorithms (deque, islice, pairwise) that produce the same windows.
- `sieve`, line 925, column 57: `stop=isqrt(n) + 1` → `+ 2`. This is equivalent: sieving one prime past √n removes nothing.
- `running_median`, line 1422: `return _running_median_minheap_and_maxheap(iterator)  # pragma: no cover`.
  The tests never reach this branch, and upstream marks it the same way (unreached, not equivalent).

Two features of the recorder were added because of this run:

- **Iterator fill.** At first `unique_justseen` scored 2/6, because the lock saw only that it returns a
  `map`, not the items in it.
- **Locking exceptions raised during fill.** `grouper(..., incomplete="strict")` raises part-way
  through the `zip`. Until that exception was locked, `strict=True` → `False` survived.
