# Autopoiesis review — 2026-11-10 (head f0aa940)

Task: correctness/robustness review of the Autopoiesis harness, centred on the AP-3 gates in
`src/autopoiesis/redteam.py` (agents untrusted, harness is the trust anchor); confirm or refute 10 suspected
defects with failing unit tests, review the other modules, and list doc claims the code does not implement.
Deliverables: this file, plus `tests/rv1110_<name>.py` (each asserts the CORRECT behaviour, so exits 1 on this head).

Assumptions not in the packet:
- `rv1110_*.py` is not collected by a directory run (pytest default `python_files` = `test_*.py`, `*_test.py`) as long
  as the name does not end in `_test.py`; verified: `pytest tests` = 25 passed, rc 0, with all 17 finding files present.
  (One file was first named `..._trivial_test.py`, got collected and turned the suite red; renamed.)
- "Refuse" for a gate means: verdict `rejected` (or an exception) and no merge into the integration branch.
- Tests build throwaway git repos under `tmp_path`, `ledger_path=None`, no network, fake tokens only.

## Findings (appended as confirmed)

### F1 (high) — earlier rounds' evidence is neither pinned nor run — `tests/rv1110_fix_edits_prior_evidence.py` — exit 1 (2 failed, 1 control passed)
- `src/autopoiesis/redteam.py:284`, `:296-301`, `:303`. `gate_fix` hashes and runs only `st["rounds"][round_n-1]["accepted"]`.
  Earlier evidence is left to the suite run `pytest <test_dir>`, but `review<N>_*.py` does not match pytest's default
  `python_files` (`test_*.py`, `*_test.py`), so a directory run never collects it. Verified both ways: a round-2 fixer
  (a) rewrites `review1_add.py` to `assert True` and re-breaks `add` -> merged; (b) re-breaks `add` without touching any
  evidence file -> merged. Same reason the committed evidence gives the target repo no regression protection after the PR
  merges unless its pytest config collects `review*.py`. (Suspect 1, first half: confirmed, and wider than suspected.)

### F2 (high) — a fix with no product change is merged — `tests/rv1110_fix_neutralises_tests.py` — exit 1 (2 failed, 1 control passed)
- `src/autopoiesis/redteam.py:296-304`. The gate trusts pytest's exit code; everything else that decides it is
  fixer-controlled. (a) `tests/conftest.py` with a `pytest_collection_modifyitems` hook that marks every item skip:
  finding run exits 0 (1 skipped), suite exits 0 -> verdict `merged`, defect still in the tree. (b) fix `add`, break
  `one()`, `git rm tests/test_green.py` -> `merged`. Not tested but same root: `addopts`/`-k`/`testpaths`/`python_files`
  in pyproject.toml, pytest.ini, tox.ini, setup.cfg; `sitecustomize.py` under the PYTHONPATH dir. Nothing is refused and
  nothing about the diff outside `src` is recorded in job.json or the receipt. (Suspect 1, second half: confirmed.)

### F3 (high) — push credential in the exception — `tests/rv1110_push_token_leak.py` — exit 1 (1 failed, 1 control passed)
- `src/autopoiesis/redteam.py:343-344` with `:78-79` (`check=True`). `str(CalledProcessError)` is
  `Command '['git', ..., 'push', '-q', 'https://x-access-token:<token>@github.com/...', ...]' returned non-zero exit
  status 128.` The CLI (`redteam.cli`, `cli.main`) does not catch it, so it lands in the traceback on stderr. The token is
  also in argv (`/proc/<pid>/cmdline`, `ps`) while the push runs. Test uses a fake token, `GIT_ALLOW_PROTOCOL=file`, and
  `url.<missing local path>.insteadOf` so the push fails locally; `urlopen` is patched to fail if reached. (Suspect 2: confirmed.)

### F4 (high) — no round sequencing — `tests/rv1110_findings_round_sequence.py` — exit 1 (2 failed, 1 control passed)
- `src/autopoiesis/redteam.py:255-257` appends unconditionally; `:215`, `:284` index `rounds[round_n-1]`; `:324` counts
  `len(rounds)`. (a) gate round 1, reviewer adds a file, gate round 1 again: rounds == [1, 1]; the second evidence file is
  committed on the integration branch but `gate_fix(round 1)` hashes only the first entry's files, so it is unpinned;
  `should_continue` is False after one round with a high. (b) `gate_findings(round_n=2)` on a fresh job is stored at
  index 0 with `round: 2`. By reading, not tested: an identical repeat (nothing new to commit) would die in `git commit`
  (`:252`, check=True) instead of being recognised as a repeat. (Suspect 3, first half: confirmed.)

### F5 (high) — the "unpatched head" is whatever the reviewer left in the review worktree — `tests/rv1110_findings_dirty_review_tree.py` — exit 1 (2 failed, 1 control passed)
- `src/autopoiesis/redteam.py:236-245`. NEW (not in the packet). `gate_findings` runs each test in `job.review` without
  checking `git status` / HEAD there, copies only `review<N>_*.py` + notes, and never re-runs the evidence on the
  integration tree. A reviewer that edits tracked source (or adds conftest.py/helpers) gets a finding admitted that passes
  on the true head. Then `gate_fix` accepts a fix branch with zero commits (`git merge` says "Already up to date",
  `fix_head == merged_head == evidence_head`) and close_job would title the PR "1 findings fixed".

### F6 (high) — the merged tree is not what was tested — `tests/rv1110_fix_ignored_files.py` — exit 1 (1 failed, 1 control passed)
- `src/autopoiesis/redteam.py:287-306`. Tests run in the fixer's directory; `git status --porcelain`/`git add -A` do not
  see ignored files. Fix commits `from pkg._generated import add`; `_generated.py` (in the target's .gitignore) exists only
  in the worktree. Verdict `merged`; on the integration head the finding test exits 2 (ImportError). (Suspect 4: confirmed.)

### F7 (medium) — `git add -A` sweeps scratch files into the PR — `tests/rv1110_fix_sweeps_untracked.py` — exit 1 (1 failed, 1 control passed)
- `src/autopoiesis/redteam.py:287-288`. An untracked `scratch/debug_env.txt` next to a properly committed fix is committed
  by the harness ("fix round 1: uncommitted work") and merged; close_job would push it. Would be high if the stray file
  holds a credential. (Suspect 5: confirmed.)

### F8 (medium) — open_job is not atomic — `tests/rv1110_open_job_partial.py` — exit 1 (2 failed, 1 control passed)
- `src/autopoiesis/redteam.py:168-176`; `next_cut` `:149-155`. Job dir, two worktrees and branch `redteam/<id>` exist
  before `next_cut` runs and before job.json is written. A truncated rotation file (JSONDecodeError) or `cuts: []`
  (ZeroDivisionError at `:151`) leaves `{'job_dir': True, 'worktrees': 2, 'branch': True}`; a retry raises "job exists",
  and `close` cannot help because there is no job.json. `next_cut` writes the rotation file with a plain `write_text`
  (`:155`), which is how a truncated file comes to exist; `_save` (`:94-97`) already shows the tmp+replace pattern.
  Also: the cut is consumed from the rotation even when the open fails later (brief write, receipt). (Suspect 6: confirmed;
  the non-atomic write itself is by reading, not by test.)

### F9 (low) — failed evidence commit leaves staged files and no state — `tests/rv1110_findings_commit_failure.py` — exit 1 (1 failed, 1 control passed)
- `src/autopoiesis/redteam.py:243-253` then `:255-259`. With a failing pre-commit hook in the target (hooks are shared with
  linked worktrees) `gate_findings` raises CalledProcessError, the integration tree shows `A  tests/review1_add.py`,
  `rounds == []`. A plain re-run does recover once the hook passes, hence low. Related: a hook that reformats and
  re-stages would commit bytes that differ from the recorded sha256, after which every fix is refused as
  `finding_tests_modified` (by reading, not tested). (Suspect 3, second half: confirmed.)

### F10 (medium) — close_job not re-runnable, API calls without timeout — `tests/rv1110_close_job_rerun.py` — exit 1 (2 failed, 1 control passed)
- `src/autopoiesis/redteam.py:345`, `:357` call `urlopen(req)` with no timeout (recorded by a fake urlopen: both GET and
  POST have `timeout=None`). `:331-333` + `:366-369`: a second `close` raises FileNotFoundError on the removed
  integration worktree; status is never consulted, so had the worktree survived it would overwrite `closed_at`/`final_head`
  and append a second `job_closed` receipt. Partly REFUTED: "PR POST fails after push -> stuck" — a retry re-pushes the
  same SHA (no-op) and POSTs again, so a plain failure is recoverable; what is not handled is a POST that succeeded
  server-side but whose response was lost (GitHub then answers 422 "already exists" forever) and the state never records
  that the push happened. That part is by reading only. Also `:345-349` verifies the commit exists on the remote, not that
  the branch ref points at it. (Suspect 7: confirmed in part.)

### F11 (medium) — severity parsing and the unreviewed last round — `tests/rv1110_severity_and_last_round.py` — exit 1 (2 failed, 1 control passed)
- `src/autopoiesis/redteam.py:38`, `:132-134`: `severity: high (silent data loss)` -> recorded "medium", `highs == 0`,
  `should_continue` False. No match at all also yields "medium" with no trace. `:321-326` + `:373-384`: with
  `max_rounds=1` a high finding's fix is merged, `should_continue` is False, and `pr_body` contains nothing about the fix
  not having been re-reviewed. Severity itself is self-declared by the reviewer; there is no second opinion in the
  harness. (Suspect 8: confirmed.)

### F12 (medium) — unfixable/irrelevant findings are admitted — `tests/rv1110_findings_unrelated_failure.py` — exit 1 (2 failed, 1 control passed)
- `src/autopoiesis/redteam.py:238-240` admits on `rc == 1` only. `assert False` and a test whose body dies on
  `import <module that is not installed>` are both committed as high-severity evidence. `evaluator.py:17-48`
  `_is_trivial_test_file` is not used by redteam, and would not catch these anyway (it flags only `pass` /
  `assert True`; `assert 1`, `assert not False`, `assert 1 == 1` all count as non-trivial). Consequence: the fixer cannot
  make such a file green without editing it, so the round dead-locks on `findings_still_red`. (Suspect 10: confirmed.)

### F13 (high) — `deployment.auto_merge: false` is ignored — `tests/rv1110_engine_auto_merge.py` — exit 1 (1 failed, 1 control passed)
- `src/autopoiesis/config.py:46`, `:89` parse the key; `src/autopoiesis/engine.py:78-81` does not pass it on and
  `engine.py:174-175` promotes on `verdict.is_pareto_dominant and not dry_run`. The shipped `autopoiesis.yaml:22` says
  `auto_merge: false`; `autopoiesis cycle` without `--dry-run` (`cli.py:166-168`) commits and fast-forwards the checked-out
  branch of the target repo. Test: full `AutopoiesisEngine.from_config(...).run_cycle(dry_run=False)` on a tmp repo with a
  genuine red-then-green challenge; `main` moves, status `promoted`. NEW (not in the packet).

### F14 (high) — false promotion receipt / wrong branch — `tests/rv1110_promotion_false_receipt.py` — exit 1 (2 failed, 1 control passed)
- `src/autopoiesis/deployment.py:147-150`: `git merge --ff-only` without `check` and without reading `returncode`;
  `self.release_branch` (`:53`) is never used, the merge goes into whatever is checked out in `repo_path`. (a) main moved
  concurrently -> ff refused -> a PromotionReceipt with `merged_at`, an Argus event and an attestation card are still
  produced for a commit that is not on main; `engine.py:176-177` reports `promoted` and `engine.py:189-191` removes the
  sandbox, and `mutation.py:54-58` (`git branch -D`) deletes the only ref to that commit: the fix is lost while the
  ledger says it was promoted.
  (b) operator checkout on `dev`, `release_branch="main"` -> `dev` is fast-forwarded, `main` untouched. NEW.
  Also by reading: `:131` `git add -A` in the sandbox sweeps everything the mutator or the test run left there
  (`.pytest_cache`, `__pycache__` if not ignored, the synthetic challenge test) into the promoted commit.

### F15 (medium) — mutation kill score 1.0 is not evidence of anything — `tests/rv1110_mutation_kill_score.py` — exit 1 (3 failed, 1 control passed)
- `src/autopoiesis/mutation_test.py:221-235`: no baseline run; any non-zero exit, exception or timeout is a "kill", so a
  red suite (or a mistyped test command) scores 1.0. `:208-216`: zero mutants -> 1.0 (lines consisting of calls, names,
  strings, attribute access have no operator). `:106-110`: the replacer swaps the FIRST node of the same type on the line,
  so for `x * 2 + 1` the mutant "Shift constant value 1 -> 2" rewrites the constant 2 to 2 — byte-identical to the
  original, an unkillable "survivor" that lowers the score of a perfectly tested line (score 0.75 here). Measured on
  that line: 4 mutants, 3 killed, 1 survivor (the no-op); the "Swap Mult with FloorDiv" mutant replaced the outer `+`
  node and produced `x // 2`. Operators are arithmetic (`:46-49`: + - * //), comparison (`:64-69`) and constant shifts
  (`:81`); no boolean, `/`, `%`, return-value or statement-deletion operator (ROADMAP.md:73 names deletion).
  Syntax-error mutants: not reachable through `ast.unparse`, so that suspicion is REFUTED; equivalent mutants are not
  detected (confirmed by the 2 -> 2 case). The tester is not called by `engine.py` or `evaluator.py` at all, and
  `gate.py:17` defaults `min_mutation_kill_score` to 0.0, so the gate's check 5 (`gate.py:75-82`) can never reject.

### F16 (medium) — evaluator crashes on a golden-suite timeout — `tests/rv1110_evaluator_timeout_crash.py` — exit 1 (1 failed, 1 control passed)
- `src/autopoiesis/evaluator.py:93-107` swallows the exception, `:147` then reads the unbound `res_golden`
  (`total_golden > 0` is always true). UnboundLocalError propagates out of `run_cycle`; the `finally` at
  `engine.py:189-191` still removes the sandbox, so no leak, but the cycle reports a traceback instead of
  "regression/timeout". NEW.

### F17 (low) — hard-coded machine path in `_receipt` — `tests/rv1110_receipt_hardcoded_path.py` — exit 1 (1 failed, 1 control passed)
- `src/autopoiesis/redteam.py:104-110`. With `argus_ledger` not importable the fallback probes
  `/opt/data/work/telarch-suite/argus/src` and prepends it to `sys.path`. Not configurable; on other machines a bare
  ImportError; on this one an unpinned working copy becomes the ledger implementation. The test intercepts `Path.is_dir`
  (records the probe, answers False), so that directory is never touched. (Suspect 9: confirmed.) This is one file over
  the 16-file budget; kept because it is one of the ten listed suspects.

## Verdict on the ten suspects
1. confirmed — F1 (`rv1110_fix_edits_prior_evidence.py`) and F2 (`rv1110_fix_neutralises_tests.py`). Wider than suspected:
   earlier evidence is not even executed by the suite run, because `review<N>_*.py` is not a default pytest file pattern.
2. confirmed — F3 (`rv1110_push_token_leak.py`).
3. confirmed — F4 (`rv1110_findings_round_sequence.py`) for the sequencing; F9 (`rv1110_findings_commit_failure.py`) for the
   dirty tree / unsaved state.
4. confirmed — F6 (`rv1110_fix_ignored_files.py`). The untracked half of the suspicion is REFUTED as stated: untracked
   files are swept into a commit by `git add -A` (`redteam.py:287-288`), so they are part of what is merged (that is F7);
   only git-ignored files escape.
5. confirmed — F7 (`rv1110_fix_sweeps_untracked.py`).
6. confirmed — F8 (`rv1110_open_job_partial.py`). The non-atomic rotation write is by reading (`redteam.py:155`).
7. confirmed in part — F10 (`rv1110_close_job_rerun.py`): no timeout, and re-running a completed close crashes. "PR POST
   fails after push -> job stuck" is refuted for a plain failure (retry re-pushes the same SHA and POSTs again); the
   lost-response case (PR exists, state says open) is unhandled, by reading only.
8. confirmed — F11 (`rv1110_severity_and_last_round.py`).
9. confirmed — F17 (`rv1110_receipt_hardcoded_path.py`).
10. confirmed — F12 (`rv1110_findings_unrelated_failure.py`). gate.py has no AST check of its own; it reads
   `details["trivial_challenge"]` computed by `evaluator._is_trivial_test_file` (`evaluator.py:17-48`, `gate.py:32-36`);
   redteam.py uses neither.

## Read, not tested (no test file; treat as unconfirmed)
- `redteam.py:128` `_run_tests` has no timeout: one agent-written test with an endless loop blocks the gate for good.
- `redteam.py:116-121` + `:39`: agent-written tests run with the harness's whole environment minus four names
  (`KIBISIS_PASSPHRASE`, `KIBISIS_PASSPHRASE_FILE`, `KIBISIS_SRC`, `CI`). Any credential exported in the harness's
  shell is readable by reviewer/fixer test code, and output goes to `logs/` which later agents may read. No sandbox.
- `redteam.py:296-306`: no check that the fix head descends from the evidence head, and the round's notes file
  (`review_notes/R<n>.md`) is not pinned; a fixer can rewrite the notes that the PR body points to.
- `redteam.py:345-349`: the post-push check asks whether the commit exists in the remote repo, not whether
  `refs/heads/<branch>` points at it. `:339` the remote regex drops everything after the first dot in a repo name
  (`org/my.repo` -> `my`).
- `redteam.py:62` `RedTeamConfig.from_dict` drops unknown keys silently (a typo such as `max_round` keeps the default);
  `config.py:60-91` likewise; `config.py:89` `bool("false")` is True; a YAML document that is a list, or `project:` with
  no value, raises AttributeError (`config.py:61-70`, `:104`).
- `engine.py:131-155`: the challenge is generated and red-checked before the patch (so that suspicion is refuted), but the
  challenge file is not pinned afterwards: the mutator callable works in the same sandbox and can rewrite
  `tests/autopoiesis_synthetic/test_cluster_<id>.py` or the golden tests before `evaluate` (same class as F1/F2).
- `curriculum.py:47-66`: "red" is any non-zero exit of any ONE challenge file, including collection errors, usage errors
  and timeouts; the other files may already be green on head.
- `curriculum.py:114-123`: the default challenge asserts `autopoiesis.remediated is True`, an attribute of the harness
  package that does not exist (`__init__.py`), so it is red on every head and can only go green if the patched tree
  ships an `autopoiesis` package defining it. It tests nothing about the clustered failure.
- `mutation.py:103-107` `git diff` omits new untracked files, and the no-op guard (`evaluator.py:78`) accepts a
  self-reported non-empty `target_files` with an empty diff. `mutation.py:96` records branch `autopoiesis/<cluster>_<id>`
  in the proposal while the real sandbox branch is `autopoiesis-eval-<cluster>` (`engine.py:127`).
- `mutation.py:36` sandboxes start from `HEAD` of the checkout, not from `deployment.branch`.
- `gate.py:57-68`: the latency check needs a baseline report; `engine.py:164` never passes one, so it never runs in a
  cycle. `evaluator.py:109,142` "p95" is the wall time of one suite run; `:143` memory is the constant 128.0;
  `:144-145` `opsec_passed`/`audit_chain_valid` are the constant True, so `gate.py:70-73` cannot fail.
  `evaluator.py:103-104` the golden suite counts as one test, so `test_pass_rate` is 0.0 or 1.0.
- `deployment.py:91`, `:117-118` swallow every exception of the ledger append and of the card write; `:94-116` the card
  is written into the release checkout as an untracked file after the merge and "attests" with an unkeyed SHA-256 of
  itself; `:170` `distilled_prompt_invariants` is a made-up label, nothing is evicted from any prompt.
- `telemetry.py:170-171` `recent_24h_occurrences` always equals `occurrences` (no time window); `:106` the hex rule also
  swallows 8+ digit decimals and words made of a-f. `engine.py:116` takes `clusters[0]` even when it is transient.
- `localization.py:85-117`: formula, zero denominators (`:100-103`) and tie order (`:116`) are correct — REFUTED as a
  defect. Limits: line-level not block-level, current thread only (`sys.settrace`), in-process callables only; not called
  by the engine.

## Doc claims the code does not implement (or contradicts)
| doc line | claim | code |
|---|---|---|
| README.md:11, :134, :150 | CLI `cycle`/`status` "print fixed text instead of running the engine"; "nothing reads that file yet" | `cli.py:166-168` loads `autopoiesis.yaml` and runs `engine.run_cycle(dry_run=args.dry_run)`; without `--dry-run` it promotes (F13). The doc understates, in the unsafe direction. |
| README.md:130 | evaluation branch "is left in place afterwards" | `engine.py:189-191`, `mutation.py:43-58` remove worktree and branch (ROADMAP.md:48 says the opposite of the README). |
| README.md:11 | generated challenge tests "always pass" | `curriculum.py:114-123` default challenge always fails (see above). |
| README.md:130 | "the gate accepts any candidate whose existing tests pass" | `gate.py:30-55` now also needs a non-trivial green challenge and a non-empty mutation. |
| README.md:144 `[x]`, ROADMAP.md:45 (v0.1.0 COMPLETED) | gate checks "latency against a baseline" | `engine.py:164` passes no baseline; check never executes in a cycle; no p95 is measured. |
| README.md:18 | "Automatically generates metamorphic fuzz tests and stress benchmarks" (present tense) | none; `evaluator.py:67` `benchmark_cmd` is stored and never used. Qualified by the NOTE at README.md:11 and unchecked at :147. |
| README.md:19 | "statistically proven defect eradication, non-degraded latency/memory, and cryptographic audit compliance" | one pass/fail run; memory constant; security flags constant (`evaluator.py:141-145`). |
| README.md:21, :145 `[x]` | prompt guardrails "are evicted, reclaiming context tokens"; token accounting | `gate.py:88` returns the constant 35; `deployment.py:170` invents a label; nothing is removed anywhere. :145 does say "fixed estimate". |
| ROADMAP.md:44 (COMPLETED) | worktree isolation in `deployment.py` | it is `mutation.py:WorktreeManager`. |
| ROADMAP.md:47 (COMPLETED) | "Hard rejection in ParetoGate and GitPromotionManager if candidate patch diff is empty or challenge ... trivial" | `deployment.py:125-126` checks only `is_pareto_dominant`; the trivial check accepts `assert 1`, `assert False`, `assert 1 == 1` (`evaluator.py:42`). |
| ROADMAP.md:72-74, :96 | every modified line must be killed; statement deletion; "100% Mutation Kill Score verified" | F15; tester not wired into engine/evaluator; `gate.py:17` threshold 0.0. (Listed under Sprint 3, i.e. as planned, but the module ships.) |
| ROADMAP.md:67-71 | SBFL on basic blocks, feeding the mutator | `localization.py` is line-level and unused by `engine.py`. |
| ROADMAP.md:98 | promotions need "signed cryptographic receipts before merge" | receipt is written after the merge attempt, unsigned, failures swallowed (`deployment.py:147-173`, `:91`, `:117`). |
| SPECIFICATION.md:68 | reads OpenTelemetry | only `JsonlTelemetrySource`, `ArgusTelemetrySource` (`telemetry.py:24,57`). |
| SPECIFICATION.md:70 | Wilson confidence intervals per cluster | none (`telemetry.py:176` is a plain ratio). |
| SPECIFICATION.md:73 | sandboxes under `/opt/data/kanban/workspaces/...` | `engine.py:55` uses `<repo>/<mutation.sandbox_dir>`; also a machine-specific path in a public spec. |
| SPECIFICATION.md:74-78 | mutation categories, "Proposer Memory" | `mutation.py:82-118` runs a caller-supplied callable; no history. |
| SPECIFICATION.md:81-83 | metamorphic tests, fuzzing, stress benchmarks | none. |
| SPECIFICATION.md:87-90 | `cargo test`; statistical defect drop / replay buffers; resident memory; secret-leak and hash-chain checks | none (constants, see above). |
| SPECIFICATION.md:94-99 | merge "via explicit SHA" to the release branch; rebuild wheels; hot-swap daemons; mark invariant `graduated_to_code` | merge goes to the checked-out branch and its failure is ignored (F14); the rest absent. |
| redteam.py:10-16 (module docstring) | admitted only if it fails "on the unpatched head"; fix admitted only if evidence untouched, findings green, suite green | F5, F1, F2, F6. |
| (absence) | README/SPECIFICATION/ROADMAP do not mention `redteam.py` or the `redteam` CLI at all; ROADMAP.md:53-56 uses "AP-3" for challenge synthesis in `curriculum.py`, while `redteam.py:1` calls itself AP-3. |

## Final state
- `pytest tests` (existing suite): 25 passed, rc 0. All 17 `tests/rv1110_*.py`: rc 1 each, every file has a passing control.
- No existing file edited; nothing committed.

