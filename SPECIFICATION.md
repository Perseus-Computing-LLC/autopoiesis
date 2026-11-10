# Autopoiesis: Recursive Self-Evolution Architecture for Software Suites

**From Prompt-Level Patching to True Autogenous Software Evolution**

*Author: Thomas Connally & Hermes Agent / Perseus Computing LLC*  
*License: MIT*

---

## 1. Executive Summary & Core Paradigm Shift

In foundational AI research, **Recursive Self-Improvement (RSI)** refers to models creating superior models (e.g., AlphaZero self-play, STaR reasoning self-distillation, neural architecture search, and constitutional self-alignment). In these systems, the model's *intrinsic structure* (weights, policy, latent representations) is modified and selected based on empirical loss against generated or ground-truth curricula.

Early autonomous agent self-improvement loops (including earlier iterations of TCI) operated primarily on **ephemeral prompt-level distillation**:
1. Software encounters an error or performance bottleneck.
2. The error is clustered and analyzed.
3. An advice string is synthesized and injected into the agent's prompt (e.g., *"terminal: confirm the binary is on PATH before invoking it"*).
4. The system prompt expands, consuming finite context tokens.

### The Failure Modes of Prompt-Only RSI
As identified across literature and empirical sweeps (arXiv:2609.24972, arXiv:2610.14224, arXiv:2610.22984):
* **Context Budget Inflation & Decay**: Prompt guardrails consume context tokens (knapsack congestion in Harpe). As rules accumulate, older or lower-priority rules are pruned, causing regression cycles.
* **Invariant Drift & Decay**: Prompt advice degrades as context length increases or when competing rules conflict.
* **Superficial Workarounds**: The underlying codebase defect or architectural inefficiency remains untouched; every agent turn wastes tokens repeatedly working around an unpatched root cause.
* **Model-Judge Arbitrage**: Relying on LLM judges to evaluate self-improvements results in ~14% verdict flips, preference leakage, and estimator sign inversions.

### The Autogenous Software RSI Paradigm
**Autopoiesis** translates the foundational AI-model evolutionary loop into software engineering:
> **The Codebase is the Genome.**  
> An autonomous agent system must not merely instruct itself to avoid bugs; it must **mutate, benchmark, verify, and promote new generations of its own software suite**, continuously shrinking prompt overhead as the underlying software permanently cures defects and expands capabilities.

```
+-----------------------------------------------------------------------------------+
|                            THE AUTOPOIESIS CLOSED LOOP                            |
|                                                                                   |
|  [ 1. SENSE ] --------> [ 2. HYPOTHESIZE & MUTATE ] ------> [ 3. CURRICULUM GEN ] |
|  Argus Ledger           Branch Git Worktree                 Synthesize Fuzz Tests |
|  & Telemetry Clusters   AST/Code Patches & Optimizations    & Stress Benchmarks   |
|         ^                                                               |         |
|         |                                                               v         |
|  [ 5. BOOTSTRAP ] <---- [ 4. PARETO SELECTION GATE ] <------------------+         |
|  Self-Hosting Deploy    - 100% Pass on Golden Suite (Zero Regression)             |
|  Argus Release Stamp    - Empirically Verified Defect Cured (P < Target)          |
|  Evict Prompt Rule      - Non-Degraded Latency / Memory / Throughput              |
|  (Free Context Tokens)  - OpSec / Cryptographic Integrity Verified                |
+-----------------------------------------------------------------------------------+
```

---

## 2. Structural Mapping: AI Models Creating Models vs. Software Suites Creating Suites

| Dimension | AI Models Creating AI Models (STaR, AlphaZero, AutoML) | Autogenous Software Suite RSI (Autopoiesis) |
| :--- | :--- | :--- |
| **Genome / Weight Space** | Model weights $\theta \in \mathbb{R}^d$, network architecture topology. | Software codebase (ASTs, algorithms, native extensions, configuration, schemas). |
| **Mutation Operator** | Gradient descent on synthetic rollouts, LoRA weight merging, architecture mutation. | LLM-guided code refactoring, native kernel synthesis, auto-recovering wrappers in isolated Git worktrees. |
| **Curriculum & Self-Play** | Synthetic problem generation, self-play games, rejection sampling. | Synthetic test synthesis, metamorphic fuzzing, stress-test generation, property-based tests (Hypothesis/proptest). |
| **Fitness Function** | Validation cross-entropy, reward model score, win-rate against anchor checkpoints. | Multi-dimensional Pareto frontier: zero regression on test suite, defect elimination, latency profiling, memory footprint. |
| **Selection Principle** | Checkpoint selection, Pareto frontier filtering, KL-divergence penalties. | Strict Pareto Domination: Candidate $C \succ P \iff \forall m \in M, C_m \ge P_m \land \exists m, C_m > P_m$. |
| **Bootstrap & Inheritance** | Selected checkpoint $\theta_{N+1}$ becomes the active generator for Generation $N+2$. | Merged release is compiled, deployed to live runtime; subsequent cycles execute on the evolved suite. |
| **Distillation Dividend** | Model compression / quantization / pruning of redundant capacity. | **Prompt Distillation**: Cured defects evict their temporary prompt invariants from Harpe, freeing context tokens. |

---

## 3. The 5 Evolutionary Stages

> **Design versus code.** This section describes the intended architecture. Each stage ends with an
> *Implemented in 0.1.0* note that says what the code in `src/autopoiesis/` does today; where the two differ,
> the note is the accurate one.

### Stage 1: Telemetry & Fitness Sensing
* **Pluggable Ingestion**: Reads structured execution traces from immutable audit ledgers (Argus), OpenTelemetry, or JSONL logs.
* **Failure & Latency Clustering**: Clusters events by tool, failure class, and semantic error template.
* **Statistical Defect Tracking**: Computes baseline error rates and Wilson confidence intervals for each cluster.

*Implemented in 0.1.0:* `JsonlTelemetrySource` and `ArgusTelemetrySource` only; there is no OpenTelemetry reader.
Events are clustered by tool and normalized error template, with a transient/deterministic flag. The per-cluster
figure is a plain ratio; no confidence interval is computed.

### Stage 2: Hypothesis & Mutation Engine
* **Isolation Guarantee**: All mutations occur in ephemeral, sandboxed Git worktrees (under `<repo>/<mutation.sandbox_dir>`, by default `<repo>/.autopoiesis/sandboxes`).
* **Multi-Tier Mutation**:
  1. *Defensive Wrapping*: Automatic input normalization, path resolution, and resilient retry logic.
  2. *Algorithmic Replacement*: Refactoring slow or fragile pure-Python routines into optimized implementations or native extensions.
  3. *Interface Hardening*: Upgrading tool schemas and contracts to eliminate ambiguity.
* **Proposer Memory**: Tracks mutation history and rejected hypotheses to prevent cyclical repairs.

*Implemented in 0.1.0:* `WorktreeManager` (in `mutation.py`) creates and removes the worktree and its branch.
`PatchApplicatorMutationEngine` runs a caller-supplied `mutator_fn(cluster, sandbox_path)` and records the
resulting diff; without one it proposes no change. None of the three mutation tiers is built in, and no mutation
history is kept.

### Stage 3: Synthetic Curriculum & Challenge Generation
* **Metamorphic Testing**: Generates test cases that assert invariant properties across varied inputs (e.g., path traversal edge cases, unicode paths, concurrency deadlocks).
* **Adversarial Fuzzing**: Automatically creates property-based test suites targeting the modified module.
* **Stress Benchmarks**: Measures execution latency, memory allocation, and CPU throughput under load.

*Implemented in 0.1.0:* none of the three. A curriculum generator writes one pytest file per cluster: a
caller-supplied synthesizer's output, or a placeholder (`RedFirstCurriculumGenerator`: a test that cannot pass;
`FileBasedCurriculumGenerator`: `assert True`). With `enforce_red_first` the engine checks that the file fails on
the unpatched head before mutating. No benchmark is run (`benchmark_cmd` is stored and unused).

### Stage 4: Multi-Objective Pareto Selection Gate
A candidate mutation $C$ is rejected if it fails any dimension of the Pareto gate:
1. **Zero Regression ($M_1$)**: 100% pass rate on all existing golden integration and unit tests (`pytest`, `cargo test`).
2. **Defect Elimination ($M_2$)**: Statistically significant drop in target defect frequency on both synthetic challenge suites and historical replay buffers.
3. **Performance Envelope ($M_3$)**: Wall-clock latency and resident memory within noise threshold ($\le 2\%$ delta) or strictly superior.
4. **OpSec & Invariant Safety ($M_4$)**: Zero secret leakage, clean cryptographic hash chain continuity in Argus, and compliance with outbound gates.

*Implemented in 0.1.0:* $M_1$ is one run of the configured test command (a shell command; exit 0 = pass; a
timeout is a failure); nothing runs `cargo test` unless that is the command. $M_2$ is: the challenge file passes,
is not trivial (a test made only of `assert True` / `pass`) and the mutation is not empty; there is no statistics
and no replay buffer. $M_3$: the wall-clock time of the test command is recorded, but the engine supplies no
baseline, so the latency comparison never runs; the memory figure is a constant. $M_4$: both flags are constants
(`True`); no secret scan and no hash-chain check is performed. A mutation kill score, when a caller supplies one,
is compared with `min_mutation_kill_score` (default 0.0); the engine does not compute it.

### Stage 5: Autogenous Promotion & Prompt Distillation
* **Self-Hosting Promotion**:
  1. Merge worktree to release branch via explicit SHA.
  2. Rebuild and install package wheels or native modules.
  3. Append an immutable release receipt to Argus ledger.
  4. Trigger zero-downtime hot-swap/restart of supervised runtime daemons.
* **The Distillation Dividend**:
  - The corresponding temporary invariant in Harpe/Kibisis is marked as `graduated_to_code` and permanently removed from prompt assembly.
  - Context tokens are immediately reclaimed for reasoning and user domain context.

*Implemented in 0.1.0:* promotion happens only when `deployment.auto_merge` is true and the cycle is not a dry
run. `GitPromotionManager` commits the candidate in its worktree and fast-forwards the configured release branch
to that commit SHA (in the worktree that has the branch checked out, otherwise by a compare-and-swap ref update);
if that is not possible it raises `PromotionError` and writes nothing. After a successful fast-forward it appends
an unsigned event to the Argus ledger when a ledger path was given and `argus_ledger` is importable, and writes a
JSON card under `.autopoiesis/attestations/`; a failure of either write is ignored. Nothing is rebuilt,
installed, restarted or hot-swapped. No invariant is marked or removed in Harpe/Kibisis: the receipt carries the
label `invariant.<cluster id>` and a fixed token estimate.

### Separate from the five stages: the red-first review loop (`redteam.py`)
`autopoiesis redteam` implements the gates of an adversarial review loop between two untrusted agents (a
reviewer writing failing tests, a fixer making them pass). It is not part of `AutopoiesisEngine.run_cycle()`.
Its module docstring states what each gate checks and what it does not establish; the README has a summary.

---

## 4. Open Source Architecture: The `autopoiesis` Framework

The framework is decoupled into a modular, language-agnostic open-source architecture:

```
autopoiesis/
├── pyproject.toml              # Modern build system (uv / hatchling)
├── README.md                   # Open source manifesto, quickstart, docs
├── SPECIFICATION.md            # Formal mathematical & architectural spec
├── src/
│   └── autopoiesis/
│       ├── __init__.py         # Package exports & versioning
│       ├── core/
│       │   ├── models.py       # Event, Cluster, Mutation, Fitness, Release models
│       │   ├── engine.py       # Closed-loop orchestrator & lifecycle driver
│       │   └── state.py        # Persistent state & Pareto frontier store
│       ├── telemetry/
│       │   ├── base.py         # Abstract TelemetrySource interface
│       │   ├── argus.py        # Tamper-evident Argus ledger reader
│       │   ├── jsonl.py        # Generic JSONL / OTel execution log reader
│       │   └── cluster.py      # Failure & bottleneck clustering engine
│       ├── mutation/
│       │   ├── base.py         # Abstract MutationEngine interface
│       │   ├── worktree.py     # Git worktree sandbox manager
│       │   └── synthesizer.py  # LLM code patch synthesizer
│       ├── curriculum/
│       │   ├── base.py         # Abstract CurriculumGenerator interface
│       │   └── test_gen.py     # Metamorphic & property-based test generator
│       ├── evaluation/
│       │   ├── base.py         # Abstract FitnessEvaluator interface
│       │   ├── pareto.py       # Pareto dominance multi-objective evaluator
│       │   └── runner.py       # Test & benchmark execution harness
│       ├── deployment/
│       │   ├── base.py         # Abstract DeploymentManager interface
│       │   └── git_deploy.py   # Atomic commit, merge, tag & release manager
│       └── cli/
│           └── main.py         # CLI commands (run, evaluate, status, init)
└── tests/
    ├── test_engine.py          # Closed-loop self-evolution test
    ├── test_pareto.py          # Pareto dominance mathematical tests
    └── test_clustering.py      # Telemetry clustering tests
```

---

## 5. Transition & Integration Plan for Telarch Suite

1. **Phase 1: Sandboxed Proposer-to-Code Pipeline**
   - Upgrade `tci_self_improvement_cycle.py` so that diagnostic Kanban cards produce verified code worktrees rather than prompt rule proposals.
2. **Phase 2: Automated Multi-Objective Balance Gate**
   - Bind `telarch_balance.py` directly into the promotion gate so candidate worktrees must pass the full 5-section balance battery before release.
3. **Phase 3: Prompt Distillation Engine**
   - Enable automatic eviction of active invariants in `active_invariants.json` once a candidate code mutation has zero occurrences across a 7-day lookback window.
4. **Phase 4: Open Source Extraction**
   - Extract the core interfaces into `autopoiesis`, release under MIT license, and publish with complete reproducible benchmarks.
