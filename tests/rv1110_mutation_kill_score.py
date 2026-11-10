"""severity: medium
finding: a mutation kill score of 1.0 does not mean "every changed line is covered by a killing test": it is also reported when the tests never passed in the first place, when no mutant could be generated for the changed lines, and mutants are applied to the wrong AST node

mutation_test.py:226-235 counts any non-zero exit (and any exception/timeout) as a kill, with no run of the
unmutated code first: a red or missing test command gives 100%. mutation_test.py:208-216 returns 1.0 for zero
mutants (lines with only calls/strings/names have no operator; statement deletion from ROADMAP.md:73 does not
exist). mutation_test.py:106-110 replaces the FIRST node of the same type on the line, not the node the mutant
was derived from, so on `x * 2 + 1` the "1 -> 2" mutant overwrites the constant 2 with 2 (no change at all).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from autopoiesis.mutation_test import DifferentialMutationTester

PY = sys.executable


def sandbox(tmp_path: Path, source: str, test_body: str) -> DifferentialMutationTester:
    (tmp_path / "calc.py").write_text(source)
    (tmp_path / "test_calc.py").write_text(
        f"import sys\nsys.path.insert(0, {str(tmp_path)!r})\nimport calc\n\n{test_body}")
    return DifferentialMutationTester(tmp_path, test_command=f"{PY} -m pytest -q -p no:cacheprovider test_calc.py")


def test_control_fully_tested_single_operator_scores_one(tmp_path: Path):
    t = sandbox(tmp_path, "def compute(x):\n    return x + 5\n", "def test_compute():\n    assert calc.compute(10) == 15\n")
    assert t.run_tests() is True
    res = t.evaluate(["calc.py"])
    assert res.total_mutants == 2 and res.mutation_kill_score == 1.0


def test_red_baseline_is_not_reported_as_a_perfect_kill_score(tmp_path: Path):
    t = sandbox(tmp_path, "def compute(x):\n    return x + 5\n", "def test_compute():\n    assert calc.compute(10) == 999\n")
    assert t.run_tests() is False                           # the suite is red before any mutant is applied
    try:
        score = t.evaluate(["calc.py"]).mutation_kill_score
    except Exception:
        return                                              # refusing to score is correct
    assert score < 1.0, f"tests fail on the unmutated code, yet mutation_kill_score == {score}"


def test_line_without_any_mutant_is_not_scored_as_fully_killed(tmp_path: Path):
    src = "def compute(x):\n    leftover = str(x).strip()\n    return abs(x)\n"
    t = sandbox(tmp_path, src, "def test_compute():\n    assert calc.compute(-3) == 3\n")
    res = t.evaluate(["calc.py"])
    assert not (res.total_mutants == 0 and res.mutation_kill_score == 1.0), (
        "3 changed lines, 0 mutants, score 1.0: the dead `leftover = ...` line was never challenged")


def test_each_mutant_changes_the_node_it_was_derived_from(tmp_path: Path):
    src = "def compute(x):\n    return x * 2 + 1\n"
    t = sandbox(tmp_path, src, "def test_compute():\n    assert [calc.compute(v) for v in (3, 4, -5)] == [7, 9, -9]\n")
    res = t.evaluate(["calc.py"])
    original = ast.unparse(ast.parse(src))
    unchanged = [m.description for m in res.mutants if m.mutated_code == original]
    assert not unchanged, f"'mutants' identical to the original program: {unchanged}"
    assert res.mutation_kill_score == 1.0                   # every true first-order mutant of this line is killed
