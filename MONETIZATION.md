# Monetization thesis (open core)

**Viability: 7/10.** Score and pricing are a founder's estimate; **no customer evidence yet (unverified).**

## The pain

Teams are handing refactors and modernisation to coding agents. The code that most needs it is
the code with the weakest tests, so "the tests pass" means little. Two things are missing:
a behaviour baseline made from what the code *really does*, and a number that says how much of that
behaviour the baseline can see. Reviewers carry the risk ("agents shifted the bottleneck to code
review" was a 331-point, 471-comment Hacker News thread in November 2026).

## The open-core boundary

| Open source (MIT, this repo) | Paid (hosted / enterprise) |
|---|---|
| `record`, `check`, `strength`, `go`, `status` | **Production and staging recorder**: sampled recording from live traffic, with PII redaction and allow-lists before anything leaves the process |
| local lock in `.refactor-anything/` | **Lock registry**: versioned, signed recordings per service and branch, so pickles never travel through PRs and the untrusted-fork risk goes away |
| Claude Code Stop hook, edit guard, skill | **PR app**: a check run with `check` plus diff-aware `strength` on every agent PR, survivors as review comments, and a required minimum strength per path |
| `check --markdown` for CI summaries | **Org dashboard**: lock strength over time per repo and module, which agent changed what, and how often agents hit the lock |
| | **Policy and audit**: "agent PRs touching `billing/` need ≥ 85% strength", an SSO-scoped audit log, and a SOC 2 change-management export |

The seam already exists in the code. The lock format (`lock.json` + `cases/*.jsonl`) is the unit
the paid tier stores, signs and serves, and `check --json` / `strength --json` are what the PR
app consumes.

## Why someone would pay

- The production recorder is the one thing a solo developer cannot do safely: PII handling,
  sampling and retention are compliance work.
- The comparable at the HTTP layer is Keploy: an open-source record/replay platform with about 20k GitHub
  stars, measured with `gh search repos keploy` on 2026-11-24. This tool locks *functions*, which
  is where agent refactors happen.
- Pricing sketch (unverified): free for public repos; about $20 per active committer per month for the
  PR app and registry; enterprise for the production recorder and the policy and audit tier.

## Risks

- Python-only today. A JS/TS front end with the same lock format is on the roadmap and needed
  for the larger market.
- Pickled inputs limit cross-version portability of locks. The registry would need a
  version-tolerant encoding for long-lived locks.
- Coding-agent vendors may build "verify before done" in. The lock and its strength score are
  agent-neutral, which is the hedge.
