# Autopoiesis 🧬

**Early research on software that repairs its own recurring failures**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> *Autopoiesis* (noun): The property of a system that can continuously reproduce, maintain, and evolve itself.

> [!NOTE]
> **Status: early research release (0.1.0).** This repository holds the frame for the loop described below; it does not repair code on its own yet. Built today: JSON Lines and Argus telemetry readers, error clustering, Git worktree sandboxes, a test-command evaluator, the promotion gate, and promotion by commit plus `git merge --ff-only`. Still placeholders: the patch step (it proposes no code change), the generated challenge tests (they always pass), the security and audit-chain checks, the memory figure, and the CLI `cycle` and `status` commands, which print fixed text instead of running the engine. The [Quickstart](#quickstart) shows a cycle you can run from Python today.

Traditional AI coding agents act as external, interactive ticket solvers: a human files an issue, the agent produces a pull request, and a human reviews it. Meanwhile, earlier "self-improving" agent architectures merely inject prompt warnings to work around their own bugs—inflating system prompts, wasting context tokens, and leaving underlying defects untouched.

**Autopoiesis** brings the foundational machine learning paradigm of **AI models creating AI models** (AlphaZero self-play, STaR self-distillation, evolutionary architecture search) directly to **software systems**:

1. **The Codebase is the Genome**: The system continuously monitors production execution telemetry, isolates recurring failure clusters and performance bottlenecks, and proposes code mutations in sandboxed worktrees.
2. **Synthetic Curriculum Generation**: Automatically generates metamorphic fuzz tests and stress benchmarks to rigorously challenge proposed mutations before touching production.
3. **Multi-Objective Pareto Selection**: Candidate builds are promoted only if they strictly Pareto-dominate the baseline: 100% test pass on existing suites (zero regression), statistically proven defect eradication, non-degraded latency/memory, and cryptographic audit compliance.
4. **Self-Hosting Bootstrap**: Winning candidates are committed and merged into the release branch with `git merge --ff-only`; building and deploying the result stays with your existing pipeline. Later cycles run against the updated code.
5. **The Prompt Distillation Dividend**: When underlying code permanently cures a defect, temporary prompt guardrails are evicted, reclaiming precious context tokens for user reasoning.

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
{'status': 'dry_run_evaluated', 'clusters_discovered': 1, 'selected_cluster': {'cluster_id': '2c182cdb7a95', 'tool': 'search', 'occurrences': 2, 'template': 'timeout after <N> seconds for request <N>'}, 'mutation_proposed': 'mut_58954335', 'verdict': {'is_pareto_dominant': True, 'zero_regression': True, 'defect_eliminated': True, 'rejection_reasons': [], 'reclaimed_tokens': 35}, 'promoted': False, 'reclaimed_tokens': 0}
```

The two errors form one cluster because numbers are replaced with `<N>` (long hex IDs and quoted strings are replaced too). The candidate is evaluated in a temporary worktree on the branch `autopoiesis-eval-<cluster id>`, which is left in place afterwards; with `dry_run=True` nothing is merged. Because the patch step and the challenge tests are placeholders in 0.1.0, the gate accepts any candidate whose existing tests pass, and a run with `dry_run=False` would commit the generated placeholder test to your release branch. Keep `dry_run=True` until a real patch source is plugged in.

### Command line

`autopoiesis init` writes an example `autopoiesis.yaml`. In 0.1.0 nothing reads that file yet, and `autopoiesis cycle` and `autopoiesis status` print placeholder text without running the engine.

---

## Roadmap

- [x] Architectural specification and Pareto selection math
- [x] Telemetry ingestion: JSON Lines and Argus ledgers
- [x] Error clustering with normalized message templates
- [x] Git worktree sandboxes
- [x] Promotion gate (existing tests, challenge tests, latency against a baseline) and `git merge --ff-only` promotion
- [x] Prompt-token accounting (a fixed estimate per retired rule)
- [ ] Patch generation (LLM coding agent or AST refactorer)
- [ ] Real challenge tests (metamorphic and fuzz)
- [ ] Security and audit-chain checks; memory measurement
- [ ] OpenTelemetry ingestion
- [ ] CLI `cycle` and `status` wired to the engine and to `autopoiesis.yaml`
- [ ] Integration with Rust `cargo test` and Go test harnesses
- [ ] Distributed tournament evaluation across multiple GPU/CPU workers

---

## License

MIT License © 2026 Perseus Computing LLC. Contributions welcome!
