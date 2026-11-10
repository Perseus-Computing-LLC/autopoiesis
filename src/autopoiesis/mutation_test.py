"""Differential Mutation Testing for verified causal minimality of candidate patches."""

from __future__ import annotations

import ast
import copy
import dataclasses
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


@dataclasses.dataclass
class Mutant:
    mutant_id: str
    file_path: Path
    lineno: int
    description: str
    original_code: str
    mutated_code: str
    killed: bool = False
    killer_test_output: str = ""


@dataclasses.dataclass
class MutationKillResult:
    """Outcome of differential mutation testing.

    `measured` is False when no score could be established: the tests did not pass on the unmutated code, or no
    mutant could be generated for the changed lines. `reason` then says which, and `mutation_kill_score` is 0.0
    (never 1.0): an unmeasured patch must not read as a fully covered one, and a gate with a minimum kill score
    refuses it. With `measured` True the score is killed / total over real, non-identical mutants.
    """

    total_mutants: int
    killed_mutants: int
    survived_mutants: int
    mutation_kill_score: float
    mutants: List[Mutant]
    measured: bool = True
    reason: str = ""


def _not_measured(reason: str) -> MutationKillResult:
    return MutationKillResult(total_mutants=0, killed_mutants=0, survived_mutants=0, mutation_kill_score=0.0,
                              mutants=[], measured=False, reason=reason)


def _position(node: ast.AST) -> Tuple:
    return (type(node), getattr(node, "lineno", None), getattr(node, "col_offset", None),
            getattr(node, "end_lineno", None), getattr(node, "end_col_offset", None))


class ASTMutator(ast.NodeTransformer):
    """Generates first-order mutants restricted strictly to target line numbers."""

    def __init__(self, target_lines: Set[int]):
        self.target_lines = target_lines
        self.generated_mutants: List[Tuple[ast.AST, int, str]] = []

    def visit_BinOp(self, node: ast.BinOp):
        if node.lineno in self.target_lines:
            op_swaps = {
                ast.Add: ast.Sub,
                ast.Sub: ast.Add,
                ast.Mult: ast.FloorDiv,
                ast.FloorDiv: ast.Mult,
            }
            op_type = type(node.op)
            if op_type in op_swaps:
                new_op = op_swaps[op_type]()
                mutated_node = copy.deepcopy(node)
                mutated_node.op = new_op
                self.generated_mutants.append(
                    (mutated_node, node.lineno, f"Swap {op_type.__name__} with {type(new_op).__name__}")
                )
        return self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare):
        if node.lineno in self.target_lines and len(node.ops) == 1:
            cmp_swaps = {
                ast.Eq: ast.NotEq,
                ast.NotEq: ast.Eq,
                ast.Lt: ast.GtE,
                ast.LtE: ast.Gt,
                ast.Gt: ast.LtE,
                ast.GtE: ast.Lt,
            }
            op_type = type(node.ops[0])
            if op_type in cmp_swaps:
                new_op = cmp_swaps[op_type]()
                mutated_node = copy.deepcopy(node)
                mutated_node.ops = [new_op]
                self.generated_mutants.append(
                    (mutated_node, node.lineno, f"Invert comparison {op_type.__name__} to {type(new_op).__name__}")
                )
        return self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant):
        if node.lineno in self.target_lines:
            if isinstance(node.value, bool):
                mutated_node = copy.deepcopy(node)
                mutated_node.value = not node.value
                self.generated_mutants.append(
                    (mutated_node, node.lineno, f"Invert boolean to {mutated_node.value}")
                )
            elif isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                mutated_node = copy.deepcopy(node)
                mutated_node.value = node.value + 1
                self.generated_mutants.append(
                    (mutated_node, node.lineno, f"Shift constant value {node.value} -> {mutated_node.value}")
                )
        return self.generic_visit(node)


class SingleNodeReplacer(ast.NodeTransformer):
    """Replaces exactly the node the mutant was derived from, identified by type and source span.

    Several nodes of one type can share a line (`x * 2 + 1` has two BinOps and two constants); the line number
    alone picks the first of them, which may not be the one that was mutated.
    """

    def __init__(self, target_lineno: int, replacement_node: ast.AST):
        self.target_lineno = target_lineno
        self.replacement_node = replacement_node
        self.target = _position(replacement_node)       # the mutant is a copy: it carries the original's span
        self.replaced = False

    def visit(self, node: ast.AST):
        if not self.replaced and _position(node) == self.target:
            self.replaced = True
            return self.replacement_node
        return self.generic_visit(node)


class DifferentialMutationTester:
    """Applies mutation testing specifically to candidate diff delta lines."""

    def __init__(self, sandbox_path: Path, test_command: str = "pytest -q"):
        self.sandbox_path = sandbox_path
        self.test_command = test_command

    def get_diff_lines(self, target_files: List[str]) -> Dict[Path, Set[int]]:
        """Parse git diff to extract modified line numbers per file."""
        diff_lines: Dict[Path, Set[int]] = {}
        for rel_file in target_files:
            file_path = self.sandbox_path / rel_file
            if not file_path.is_file():
                continue

            res = subprocess.run(
                ["git", "-C", str(self.sandbox_path), "diff", "-U0", "HEAD", "--", rel_file],
                capture_output=True,
                text=True,
            )
            lines: Set[int] = set()
            for line in res.stdout.splitlines():
                if line.startswith("@@"):
                    # Format: @@ -old,count +new,count @@
                    parts = line.split("+")
                    if len(parts) > 1:
                        chunk = parts[1].split("@@")[0].strip()
                        if "," in chunk:
                            start_str, count_str = chunk.split(",")
                            start = int(start_str)
                            count = int(count_str)
                            for i in range(start, start + count):
                                lines.add(i)
                        else:
                            lines.add(int(chunk))
            if not lines:
                # Fallback: if untracked or diff empty, include all non-empty lines
                content_lines = file_path.read_text(encoding="utf-8").splitlines()
                lines = {idx + 1 for idx, l in enumerate(content_lines) if l.strip()}
            diff_lines[file_path] = lines
        return diff_lines

    def run_tests(self, timeout_seconds: int = 45) -> bool:
        """Run test command in sandbox. Returns True if tests pass (exit 0)."""
        env = os.environ.copy()
        # no bytecode cache: a mutant has the size of the original and may be written within the same second,
        # which is all a cached .pyc is checked against
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        res = subprocess.run(
            self.test_command,
            shell=True,
            cwd=str(self.sandbox_path),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
        return res.returncode == 0

    def evaluate(self, target_files: List[str], max_mutants: int = 50) -> MutationKillResult:
        # A mutant is "killed" when the tests fail on it. That only means something if they pass without it.
        try:
            baseline_green = self.run_tests()
        except subprocess.TimeoutExpired:
            return _not_measured("the tests timed out on the unmutated code")
        if not baseline_green:
            return _not_measured("the tests do not pass on the unmutated code")

        diff_lines = self.get_diff_lines(target_files)
        all_mutants: List[Mutant] = []

        for file_path, lines in diff_lines.items():
            if not lines or not file_path.suffix == ".py":
                continue

            orig_source = file_path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(orig_source, filename=str(file_path))
            except SyntaxError:
                continue
            orig_unparsed = ast.unparse(tree)

            collector = ASTMutator(lines)
            collector.visit(tree)

            for idx, (mut_node, lineno, desc) in enumerate(collector.generated_mutants[:max_mutants]):
                mid = f"{file_path.stem}_L{lineno}_m{idx}"
                # Clone original tree and replace single node
                fresh_tree = ast.parse(orig_source, filename=str(file_path))
                replacer = SingleNodeReplacer(lineno, mut_node)
                mutated_tree = replacer.visit(fresh_tree)
                ast.fix_missing_locations(mutated_tree)

                try:
                    mutated_code = ast.unparse(mutated_tree)
                except Exception:
                    continue
                if not replacer.replaced or mutated_code == orig_unparsed:
                    continue                                # not a mutant: the program is unchanged

                all_mutants.append(
                    Mutant(
                        mutant_id=mid,
                        file_path=file_path,
                        lineno=lineno,
                        description=desc,
                        original_code=orig_source,
                        mutated_code=mutated_code,
                    )
                )

        if not all_mutants:
            # Nothing was challenged (no arithmetic/comparison operator or numeric/boolean constant on the
            # changed lines, or no changed Python file): that is "unknown", not "every mutant killed".
            return _not_measured("no mutant could be generated for the changed lines")

        killed_count = 0
        survived_count = 0

        for mutant in all_mutants:
            # Apply mutant
            mutant.file_path.write_text(mutant.mutated_code, encoding="utf-8")
            try:
                passed = self.run_tests()
                if not passed:
                    mutant.killed = True
                    killed_count += 1
                else:
                    mutant.killed = False
                    survived_count += 1
            except subprocess.TimeoutExpired:
                # the tests passed in time without the mutant (baseline above), so not finishing is its doing
                mutant.killed = True
                mutant.killer_test_output = "timeout"
                killed_count += 1
            finally:
                # Restore original source
                mutant.file_path.write_text(mutant.original_code, encoding="utf-8")

        score = float(killed_count) / float(len(all_mutants))
        return MutationKillResult(
            total_mutants=len(all_mutants),
            killed_mutants=killed_count,
            survived_mutants=survived_count,
            mutation_kill_score=round(score, 4),
            mutants=all_mutants,
        )
