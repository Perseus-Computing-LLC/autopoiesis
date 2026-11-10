# Autopoiesis 🧬

**Early research on software that repairs its own recurring failures**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> *Autopoiesis* (noun): The property of a system that can continuously reproduce, maintain, and evolve itself.

> [!NOTE]
> **Status: early research release (0.1.0).** This repository holds the frame for the loop described below; it does not repair code on its own yet. Built today: JSON Lines and Argus telemetry readers, error clustering, Git worktree sandboxes, a test-command evaluator, the promotion gate, promotion by commit plus a fast-forward of the release branch, the CLI (`init`, `status`, `cycle`, `redteam`), and the red-first review loop gates (`autopoiesis redteam`, see [below](#red-first-review-loop)). Still placeholders: the patch step (without a caller-supplied `mutator_fn` it proposes no code change, and the gate rejects that as a no-op), the default challenge tests (`RedFirstCurriculumGenerator` writes a test that cannot pass unless you supply a `test_synthesizer`; `FileBasedCurriculumGenerator` writes `assert True`, which the gate rejects as trivial), the security and audit-chain checks (constants), the memory figure (a constant), and the latency comparison (no baseline is measured, so it never runs in a cycle). `autopoiesis cycle` runs the engine from `autopoiesis.yaml`; **without `--dry-run` and with `deployment.auto_merge: true` it commits to and fast-forwards your release branch.** The [Quickstart](#quickstart) shows a cycle you can run from Python today.

Traditional AI coding agents act as external, interactive ticket solvers: a human files an issue, the agent produces a pull request, and a human reviews it. Meanwhile, earlier "self-improving" agent architectures merely inject prompt warnings to work around their own bugs—inflating system prompts, wasting context tokens, and leaving underlying defects untouched.

**Autopoiesis** brings the foundational machine learning paradigm of **AI models creating AI models** (AlphaZero self-play, STaR self-distillation, evolutionary architecture search) directly to **software systems**:

1. **The Codebase is the Genome**: The system continuously monitors production execution telemetry, isolates recurring failure clusters and performance bottlenecks, and proposes code mutations in sandboxed worktrees.
2. **Synthetic Curriculum Generation**: The design calls for generated metamorphic fuzz tests and stress benchmarks that challenge a proposed mutation before it touches production. *0.1.0 generates neither:* it writes one pytest file per failure cluster (a caller-supplied synthesizer, or a placeholder), and can check that this file fails on the unpatched head (`enforce_red_first`). The evaluator's `benchmark_cmd` is stored and never run.
3. **Multi-Objective Pareto Selection**: The design promotes a candidate only if it strictly Pareto-dominates the baseline. *What 0.1.0 checks:* the existing test command exits 0 (one run, pass/fail), the challenge test passes and is not trivial, and the mutation is not empty. There is no statistical test, latency is not compared (no baseline is measured), and the memory figure and the security/audit flags are constants.
4. **Self-Hosting Bootstrap**: Winning candidates are committed and the configured release branch is fast-forwarded to that commit (only when `deployment.auto_merge` is true and the run is not a dry run); if the fast-forward is not possible the promotion fails and nothing is attested. Building and deploying the result stays with your existing pipeline. Later cycles run against the updated code.
5. **The Prompt Distillation Dividend**: The design evicts temporary prompt guardrails once code permanently cures a defect. *0.1.0 evicts nothing:* a promoted candidate is credited a fixed estimate (35 tokens by default) and the receipt carries a label `invariant.<cluster id>`; no prompt or rule file is read or changed.

---

## Architecture Overview

```
                      +-----------------------------+
                      |   Production Execution      |
                      |   & Telemetry Stream        |
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      |  1. Failure & Bottleneck    |
                      |     Clustering (Telemetry)  |
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      |  2. Hypothesis & Mutation   |
                      |     Synthesizer (Worktree)  |
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      |  3. Synthetic Curriculum    |
                      |     & Fuzz Generator        |
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      |  4. Multi-Objective Pareto  |
                      |     Evaluation Gate         |
                      +--------------+--------------+
                                     |
                      [Passes Pareto Domination?]
                                     |
                         +-----------+-----------+
                         | Yes                   | No
                         v                       v
          +-----------------------------+  +---------------+
          |  5. Self-Hosting Promotion  |  | Discard / Log |
          |     - Merge & Build Wheel   |  | Hypothesis    |
          |     - Evict Prompt Rules    |  +---------------+
          |     - Argus Release Audit   |
          +-----------------------------+
```

---

## Core Principles (Avoiding RSI Failure Modes)

Drawing from recent empirical literature on self-improving agents (arXiv:2609.24972, arXiv:2610.14224):

* **No Unanchored Model Judges**: Evaluation does not rely on subjective LLM scoring (which suffers from ~14% verdict flips and preference leakage). All fitness criteria are grounded in deterministic test passes, latency profiling, and statistical error-rate bounds.
* **Pareto Domination Gate**: A candidate mutation $C$ replaces parent baseline $P$ if and only if $C$ is strictly non-inferior to $P$ across all metrics and strictly superior in at least one ($C \succ P$).
* **Prompt Distillation Dividend**: Instead of accumulating permanent prompt bloat, prompt rules serve only as transient containment while code mutations are synthesized and verified. Once verified, prompt rules are retired.
* **Cryptographic Tamper-Evidence**: Every mutation, benchmark report, and release promotion is to be logged to a hash-chained audit ledger such as Argus (planned; not in 0.1.0).

---

## Quickstart

### Installation

The name `autopoiesis` on PyPI belongs to an unrelated project, so install from GitHub (Python 3.11 or later):

```bash
pip install "git+https://github.com/Perseus-Computing-LLC/autopoiesis"
```

### Run one cycle from Python

In 0.1.0 the engine is used from Python. This runs one dry-run cycle in a Git repository that has a pytest suite and a `telemetry.jsonl` file:

```python
from pathlib import Path
from autopoiesis import (AutopoiesisEngine, JsonlTelemetrySource, ClusterEngine, WorktreeManager,
                         PatchApplicatorMutationEngine, FileBasedCurriculumGenerator,
                         SubprocessFitnessEvaluator, ParetoGate, GitPromotionManager)

repo = Path(".").resolve()
engine = AutopoiesisEngine(
    telemetry_source=JsonlTelemetrySource(repo / "telemetry.jsonl"),
    cluster_engine=ClusterEngine(),
    worktree_manager=WorktreeManager(repo, repo / ".autopoiesis" / "sandboxes"),
    mutation_engine=PatchApplicatorMutationEngine("my-service"),  # 0.1.0: proposes no code change
    curriculum_generator=FileBasedCurriculumGenerator(),
    fitness_evaluator=SubprocessFitnessEvaluator(golden_test_cmd="python -m pytest -q"),
    pareto_gate=ParetoGate(),
    deployment_manager=GitPromotionManager(repo, release_branch="main"),
)
print(engine.run_cycle(dry_run=True))
```

With two telemetry lines such as

```json
{"sequence": 1, "tool": "search", "status": "error", "error_message": "timeout after 30 seconds for request 1041"}
{"sequence": 2, "tool": "search", "status": "error", "error_message": "timeout after 31 seconds for request 2210"}
```

it prints (the mutation ID is random):

```text
{'status': 'dry_run_evaluated', 'clusters_discovered': 1, 'selected_cluster': {'cluster_id': '2c182cdb7a95', 'tool': 'search', 'occurrences': 2, 'template': 'timeout after <N> seconds for request <N>', 'is_transient': False}, 'mutation_proposed': 'mut_565036ce', 'verdict': {'is_pareto_dominant': False, 'zero_regression': True, 'defect_eliminated': False, 'rejection_reasons': ['No-op mutation: candidate diff and target_files are empty.', 'Trivial curriculum: synthetic challenge test contains only constant assert True.'], 'reclaimed_tokens': 0}, 'promoted': False, 'reclaimed_tokens': 0}
```

The two errors form one cluster because numbers are replaced with `<N>` (long hex IDs and quoted strings are replaced too). The candidate is evaluated in a temporary worktree on the branch `autopoiesis-eval-<cluster id>`; both the worktree and the branch are removed when the cycle ends, whatever the outcome. The verdict above is a rejection, and that is the expected result of this example: the patch step proposed no change and the placeholder challenge is `assert True`, and the gate refuses both. A candidate is accepted only when the existing tests pass, the mutation is not empty and a non-trivial challenge test passes; to see that, pass a `mutator_fn` to `PatchApplicatorMutationEngine` and a `test_synthesizer` to `RedFirstCurriculumGenerator`. With `dry_run=True` nothing is merged. With `dry_run=False` an accepted candidate (including its generated challenge test) is committed and your release branch is fast-forwarded to it, unless the engine was built with `auto_merge=False`.

### Command line

`autopoiesis init` writes an example `autopoiesis.yaml` (its `deployment.auto_merge` is `true`). The other commands read that file (`--config PATH` to use another):

- `autopoiesis status [--json]` reads the telemetry and prints the failure clusters. It changes nothing.
- `autopoiesis cycle [--dry-run] [--json]` runs one engine cycle. Without `--dry-run`, an accepted candidate is committed and the release branch (`deployment.branch`) is fast-forwarded to it. Set `deployment.auto_merge: false` to evaluate without ever promoting (status `dominant_not_merged`); a fast-forward that cannot be done ends in status `promotion_failed` and exit code 1.
- `autopoiesis redteam --target NAME <open|gate-findings|open-fix|gate-fix|continue|close|status>` drives the review loop described next.

### Red-first review loop

`autopoiesis redteam` (module `autopoiesis.redteam`) is the mechanical part of an adversarial review loop run by two agents that are not trusted: a reviewer that writes failing tests and a fixer that makes them pass. The harness only admits or refuses what they produce; it is configured under `redteam:` in `autopoiesis.yaml` and needs a pytest-compatible test command.

- `open` creates a job: an integration branch `redteam/<job>` and a review worktree, both at the head of the base branch, or nothing at all if any step fails.
- `gate-findings` copies each `tests/review<N>_*.py` the reviewer wrote, alone, into a fresh checkout of the head and runs it there. A file is kept only if it fails by assertion, imports the code under review and is not made of constant assertions; kept files are committed as evidence. Rounds are gated once each, in order, up to `max_rounds`.
- `open-fix` gives the fixer a branch from the evidence commit.
- `gate-fix` judges the commit at the tip of that branch from a fresh checkout (not the fixer's working directory): the evidence of every round must be byte-identical and green, the whole suite green, no previously running test dropped or skipped, at least one file changed outside the test and notes directories, and no change to `conftest.py` or pytest configuration. Only then is that commit merged into the integration branch.
- `close` pushes the integration branch and opens a pull request for a human to merge. The GitHub token is passed to git through the environment only.

Limits: the tests are agent-written code run without a sandbox; an existing test whose body the fixer rewrote keeps its name and is only listed in the pull request, not refused; severity is the reviewer's own declaration. With a ledger configured each gate appends a receipt through `argus_ledger`, which must be installed or located with `redteam.argus_src` / `AUTOPOIESIS_ARGUS_SRC`.

---

## Roadmap

- [x] Architectural specification and Pareto selection math
- [x] Telemetry ingestion: JSON Lines and Argus ledgers
- [x] Error clustering with normalized message templates
- [x] Git worktree sandboxes
- [x] Promotion gate (existing tests, non-trivial challenge test, non-empty mutation) and fast-forward promotion to the release branch
- [ ] Latency comparison against a measured baseline (the check exists in the gate; the engine supplies no baseline)
- [x] Prompt-token accounting (a fixed estimate per promoted candidate; no rule is actually retired)
- [ ] Patch generation (LLM coding agent or AST refactorer)
- [ ] Real challenge tests (metamorphic and fuzz)
- [ ] Security and audit-chain checks; memory measurement
- [ ] OpenTelemetry ingestion
- [x] CLI `cycle` and `status` wired to the engine and to `autopoiesis.yaml`
- [x] Red-first review loop gates (`autopoiesis redteam`)
- [ ] Integration with Rust `cargo test` and Go test harnesses
- [ ] Distributed tournament evaluation across multiple GPU/CPU workers

---

## License

MIT License © 2026 Perseus Computing LLC. Contributions welcome!
