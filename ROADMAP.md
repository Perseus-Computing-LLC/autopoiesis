# Autopoiesis: Development & Research Roadmap

**Repository:** `Perseus-Computing-LLC/autopoiesis`  
**License:** MIT  
**Current Baseline:** `0.1.0` (`0ce2276`)

---

## 1. Vision & Architectural Thesis

Autopoiesis is designed to advance automated program repair (APR) and recursive software improvement from heuristic prompt-patching to **causally verified, mathematically grounded autogenous self-evolution**. 

Classical APR and naive LLM code-fix loops suffer from two crippling pathologies:
1. **Overfitting to Static Test Suites:** Generating trivial branch-avoidance hacks (`try...except: pass` or `if x == bad_val: return default`) that turn tests green without fixing logic.
2. **Hallucinated Bloat:** Inserting speculative, unexercised code that passes existing tests but degrades long-term maintainability and introduces latent bugs.

Autopoiesis eliminates these pathologies through an integrated framework of **Red-First Challenge Synthesis**, **Predator-Prey Minimax Co-Evolution**, **Differential Mutation Testing (100% Mutation Kill Score)**, and **Cryptographically Attested Evolution Chains**.

---

## 2. Release & Milestone Register

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            AUTOPOIESIS MILESTONES                           │
├─────────────────────────────────────────────────────────────────────────────┤
│  v0.1.0 [COMPLETED] ──► Invariant frame, worktrees, telemetry clustering    │
│  │                      Hardening: No-op diff rejection & worktree cleanup  │
│  │                                                                          │
│  v0.2.0 [SPRINT 2]  ──► Core Loop: Red-First Challenge Synthesis (AP-3)     │
│  │                      CLI & Config Wiring (AP-4), TCI Error Normalization │
│  │                                                                          │
│  v0.3.0 [SPRINT 3]  ──► Mathematical Guardrails: Ochiai SBFL Localization   │
│  │                      Differential Mutation Testing (100% Kill Score)     │
│  │                      Co-Evolutionary Minimax Adversarial Turn            │
│  │                                                                          │
│  v0.4.0 [SPRINTS 4-5]► Attested Computation Chains (Argus SHA-256 Binding) │
│                         End-to-End Autonomous Repair on Real Telemetry      │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Version 0.1.0 — Foundational Frame & Sandboxing (Completed)
- **Telemetry Ingestion & Clustering (`telemetry.py`):** Ingestion of JSONL and Argus audit logs with integer, hex, and string normalization.
- **Git Worktree Isolation (`deployment.py`):** Ephemeral sandboxed evaluation branches (`autopoiesis-eval-<cluster>`).
- **Subprocess Test Execution & Gating (`evaluator.py`, `gate.py`):** Baseline and challenge test execution against `ParetoGate` (latency, regression, and correctness).
- **Hardened Invariants (`0ce2276`):**
  - Hard rejection in `ParetoGate` and `GitPromotionManager` if candidate patch diff is empty or challenge test AST contains only trivial `assert True`.
  - Automated teardown and cleanup of ephemeral worktrees and evaluation branches upon exit.

---

### Version 0.2.0 — Red-First Challenge & Core Operational Loop (Sprint 2)
- **AP-3: Deterministic Red-First Challenge Synthesis (`curriculum.py`):**
  - Extract failure stack traces, arguments, and invariants directly from failure clusters.
  - Synthesize deterministic reproducing unit tests.
  - **Invariant:** A challenge test must be verified to **FAIL (`RED`) on unpatched `HEAD`** before candidate patch generation commences. Challenge tests that pass on `HEAD` are discarded immediately.
- **AP-4: CLI & Configuration Wiring (`cli.py`, `autopoiesis.yaml`):**
  - Connect `autopoiesis init`, `status`, and `cycle` directly to YAML configuration parsing and `AutopoiesisEngine.run_cycle()`.
- **AP-5: Production Telemetry Normalization Port:**
  - Port battle-tested normalization rules from Telarch `ops/tci`:
    - Normalization of duration tokens (`150ms`, `30s`), memory units, and line suffixes (`script.py:142`).
    - Transient-retry deconvolution filter: separating environmental transients from deterministic logic bugs.

---

### Version 0.3.0 — Mathematical Anti-Overfitting & Formal Guardrails (Sprint 3)
- **Sub-Second Spectrum-Based Fault Localization (Ochiai SBFL):**
  - Profile test coverage across passing vs. failing challenge executions.
  - Compute the Ochiai coefficient for every basic block:
    $$S(b) = \frac{n_{\text{failed}}(b)}{\sqrt{N_{\text{total\_failed}} \cdot (n_{\text{failed}}(b) + n_{\text{passed}}(b))}}$$
  - Narrow down suspicious code to the top 1–3 basic blocks in $<1.0\text{s}$ ($0 token spend) before model prompting.
- **Causal Minimality via Differential Mutation Testing (100% Mutation Kill Score):**
  - Apply mutation testing *strictly* to the lines modified by the proposed patch $\Delta$ (inverting operators, toggling boundary conditions, deleting statements).
  - **Gate Invariant:** Every modified line in $\Delta$ must cause either the regression test suite or the challenge test to fail when mutated. If mutating a line inside $\Delta$ does not cause a failure, that line is provably dead/hallucinated and the patch is refused.
- **Predator-Prey Minimax Co-Evolution:**
  - Model Curriculum and Mutator as a zero-sum game:
    $$\min_{\theta_{\text{mut}}} \max_{\phi_{\text{curr}}} \mathbb{E}_{x \sim \mathcal{D}_{\text{fail}}} \left[ \mathcal{L}_{\text{test}}(P_{\theta}(x)) - \mathcal{L}_{\text{perturb}}(C_{\phi}(P_{\theta})) \right]$$
  - After the Mutator produces a patch turning the challenge test green, the Curriculum executes an adversarial turn: applying metamorphic property-based perturbations around input boundaries to break the patch. Only patches surviving the adversarial perturbation pass `ParetoGate`.

---

### Version 0.4.0 — Attested Autonomous Evolution (Sprints 4–5)
- **Cryptographic Attestation Binding (Argus Ledger):**
  - Embed SHA-256 Attested Computation Cards (Telarch P15/P16 standard) into autonomous commit metadata.
  - Every promoted commit binds:
    $$\text{Receipt} = \mathcal{H}\Big(\text{FailureTelemetry}_{\text{SHA256}} \mathbin{\Vert} \text{FailingTest}_{\text{AST}} \mathbin{\Vert} \text{SBFL}_{\text{Rank}} \mathbin{\Vert} \text{Diff}_{\Delta} \mathbin{\Vert} \text{MutationKill}_{\text{100\%}} \mathbin{\Vert} \text{ParetoGate}_{\text{Deltas}}\Big)$$
- **Closed-Loop Telarch Dogfooding:**
  - Ingest real-world telemetry failures from the live Telarch audit ledger.
  - Demonstrate fully autonomous, end-to-end self-repair across Perseus tool plugins.

---

## 3. Verification & Acceptance Criteria

1. **Zero Overfitting:** 0% promotion of trivial branch-avoidance or empty-diff mutations.
2. **Zero Hallucinated Lines:** 100% Mutation Kill Score verified across all lines in promoted diffs.
3. **Execution Efficiency:** SBFL localization completes in $<1.0\text{s}$ per cluster.
4. **Safety Invariant:** All autonomous branch promotions must pass green CI with signed cryptographic receipts before merge.
