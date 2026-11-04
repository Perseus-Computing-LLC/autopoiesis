# Autopoiesis 🧬

**Recursive Self-Evolution Engine for Software Suites**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> *Autopoiesis* (noun): The property of a system that can continuously reproduce, maintain, and evolve itself.

Traditional AI coding agents act as external, interactive ticket solvers: a human files an issue, the agent produces a pull request, and a human reviews it. Meanwhile, earlier "self-improving" agent architectures merely inject prompt warnings to work around their own bugs—inflating system prompts, wasting context tokens, and leaving underlying defects untouched.

**Autopoiesis** brings the foundational machine learning paradigm of **AI models creating AI models** (AlphaZero self-play, STaR self-distillation, evolutionary architecture search) directly to **software systems**:

1. **The Codebase is the Genome**: The system continuously monitors production execution telemetry, isolates recurring failure clusters and performance bottlenecks, and proposes code mutations in sandboxed worktrees.
2. **Synthetic Curriculum Generation**: Automatically generates metamorphic fuzz tests and stress benchmarks to rigorously challenge proposed mutations before touching production.
3. **Multi-Objective Pareto Selection**: Candidate builds are promoted only if they strictly Pareto-dominate the baseline: 100% test pass on existing suites (zero regression), statistically proven defect eradication, non-degraded latency/memory, and cryptographic audit compliance.
4. **Self-Hosting Bootstrap**: Winning candidates are atomically merged, compiled, and deployed to live runtime. Subsequent improvement cycles execute directly on the newly evolved generation.
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
* **Cryptographic Tamper-Evidence**: Every mutation, benchmark report, and release promotion is logged to an immutable hash-chained audit ledger.

---

## Quickstart

### Installation

```bash
pip install autopoiesis
```

### Initializing a Project

```bash
cd your-software-suite
autopoiesis init
```

This generates `autopoiesis.yaml` configuring your telemetry source, test runners, benchmark targets, and promotion criteria:

```yaml
version: 1
project:
  name: "my-service"
  repo_path: "."

telemetry:
  adapter: "jsonl"
  path: "logs/execution.jsonl"
  lookback_days: 7

mutation:
  sandbox_dir: ".autopoiesis/sandboxes"
  model: "claude-3-7-sonnet"

evaluation:
  test_command: "pytest -q"
  benchmark_command: "pytest --benchmark-only"
  metrics:
    - name: "test_pass_rate"
      type: "maximize"
      threshold: 1.0  # Zero regressions allowed
    - name: "target_defect_rate"
      type: "minimize"
    - name: "p95_latency_ms"
      type: "minimize"
      max_degradation_pct: 2.0

deployment:
  branch: "main"
  tag_prefix: "v"
  auto_merge: true
```

### Running a Self-Evolution Cycle

```bash
# Run a single evaluation cycle in dry-run mode
autopoiesis cycle --dry-run

# Run full evolutionary cycle with worktree mutation and Pareto evaluation
autopoiesis cycle --run

# View status of active candidates, Pareto frontier, and reclaimed prompt tokens
autopoiesis status
```

---

## Roadmap

- [x] Architectural Specification & Pareto selection math
- [x] Pluggable telemetry ingestion (Argus, OpenTelemetry, JSONL)
- [x] Sandboxed Git worktree mutation engine
- [x] Multi-objective Pareto evaluation gate
- [x] Prompt-to-code distillation accounting
- [ ] Integration with Rust `cargo test` and Go test harnesses
- [ ] Distributed tournament evaluation across multiple GPU/CPU workers

---

## License

MIT License © 2026 Perseus Computing LLC. Contributions welcome!
