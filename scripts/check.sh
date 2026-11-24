#!/usr/bin/env bash
# The one gate: types, tests, then the CLI end to end on a scratch project.
set -euo pipefail
trap 'echo "check.sh: FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR

PY="${PYTHON:-python3}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

echo "== mypy --strict"
"$PY" -m mypy --strict "$ROOT/src"

echo "== pytest"
"$PY" -m pytest -q "$ROOT/tests"

echo "== CLI end to end"
WORK="$(mktemp -d)"
cd "$WORK"
ra() { "$PY" -m refactor_anything "$@"; }
cat > invoice.py <<'PY'
def total(lines, tax=0.2):
    subtotal = 0
    for qty, price in lines:
        if qty < 0:
            raise ValueError("negative quantity")
        subtotal += qty * price
    return round(subtotal * (1 + tax), 2)


def label(n):
    if n == 1:
        return "1 item"
    return f"{n} items"
PY
cat > test_invoice.py <<'PY'
import unittest
import invoice


class T(unittest.TestCase):
    def test_total(self):
        self.assertEqual(invoice.total([(2, 10.0), (1, 5.5)]), 30.6)
        self.assertEqual(invoice.total([], tax=0), 0)
        with self.assertRaises(ValueError):
            invoice.total([(-1, 1.0)])

    def test_label(self):
        for n in (0, 1, 2, 10):
            invoice.label(n)
PY

ra record invoice -- "$PY" -m unittest -q 2> record.err
ra check | tee check.out
grep -q "^PASS  7/7" check.out
ra strength | tee strength.out
grep -q "lock strength" strength.out

# a refactor that keeps behaviour passes
"$PY" - <<'PY'
import pathlib
p = pathlib.Path("invoice.py")
p.write_text(p.read_text().replace('''    if n == 1:
        return "1 item"
    return f"{n} items"''', '''    return f"{n} item" + ("" if n == 1 else "s")'''))
PY
ra check > /dev/null

# strength named a blind spot: `qty < 0` -> `qty <= 0` survives, no recorded call has qty == 0.
grep -q 'invoice.py:4  total: `<` -> `<=`' strength.out
sed -i.bak 's/if qty < 0:/if qty <= 0:/' invoice.py && rm invoice.py.bak
ra check > /dev/null   # ...and indeed the lock cannot see that change yet
git_restore() { sed -i.bak 's/if qty <= 0:/if qty < 0:/' invoice.py && rm invoice.py.bak; }
git_restore

# record one more input that reaches the line, and the blind spot closes
printf 'import invoice\ninvoice.total([(0, 9.99), (3, 1.25)])\n' > more.py
ra record invoice -- "$PY" more.py > /dev/null
ra strength --json > strength.json
if grep -q '"line": 4,' strength.json && grep -q '`<` -> `<=`' strength.json; then echo "blind spot still open" >&2; exit 1; fi

# now the same behaviour change fails, with the case that shows it
sed -i.bak 's/if qty < 0:/if qty <= 0:/' invoice.py && rm invoice.py.bak
if ra check > diff.out; then echo "expected a behaviour diff" >&2; exit 1; fi
grep -q "DIFF invoice:total" diff.out
grep -q "raised ValueError: negative quantity" diff.out

# the Stop hook says the same thing to Claude Code
printf '{"session_id":"x","cwd":"%s"}' "$("$PY" -c 'import os; print(os.getcwd().replace(chr(92), "/"))')" \
  | ra hook stop > hook.out
grep -q '"decision": "block"' hook.out
echo "check.sh: all green"
