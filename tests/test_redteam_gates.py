"""Regression tests for the review-loop gates beyond the cases the rv1110 finding files use.

The agents are not trusted; these tests play a reviewer or a fixer that tries a different door from the one a
finding file tried: another way to drop a test, another way to carry unreviewed files, another call order.
"""
from __future__ import annotations

import importlib.abc
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from autopoiesis import redteam

PY = sys.executable
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
BUGGY = "def add(a, b):\n    return a - b  # bug\n\ndef one():\n    return 1\n"
FIXED = "def add(a, b):\n    return a + b\n\ndef one():\n    return 1\n"
FINDING_ADD = '"""severity: high\nfinding: add\n"""\nfrom pkg import add\n\ndef test_add():\n    assert add(2, 3) == 5\n'


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def make(tmp_path: Path, **kw):
    repo = tmp_path / "target"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "pkg" / "__init__.py").write_text(BUGGY)
    (repo / "tests" / "test_green.py").write_text(
        "from pkg import one\n\ndef test_one():\n    assert one() == 1\n\ndef test_one_again():\n    assert one() + 1 == 2\n")
    (repo / "tests" / "test_keep.py").write_text("from pkg import one\n\ndef test_kept():\n    assert one() == 1\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    base = dict(repo_path=str(repo), test_command=[PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                python_path="src", cuts=["a", "b"], rotation_path=str(tmp_path / "rotation.json"),
                jobs_root=str(tmp_path / "jobs"), ledger_path=None, max_rounds=2)
    base.update(kw)
    return repo, redteam.RedTeamConfig(**base)


def state(job) -> dict:
    return json.loads(Path(job.path, "job.json").read_text())


def gated(tmp_path: Path, job_id: str, **kw):
    """A job with round 1 gated on one real finding and the fixer's worktree open."""
    repo, cfg = make(tmp_path, **kw)
    job = redteam.open_job(cfg, job_id)
    (Path(job.review) / "tests" / "review1_add.py").write_text(FINDING_ADD)
    assert [f["name"] for f in redteam.gate_findings(cfg, job, 1)["accepted"]] == ["add"]
    return repo, cfg, job, Path(redteam.open_fix(cfg, job, 1))


def integration_untouched(job) -> bool:
    integ = Path(job.integration)
    return (git(integ, "rev-parse", "HEAD") == state(job)["rounds"][0]["evidence_head"]
            and git(integ, "status", "--porcelain") == "")


# ------------------------------------------------------------------------------------------- gate_fix
def test_uncommitted_work_in_the_fix_worktree_is_not_a_fix(tmp_path: Path):
    _, cfg, job, fix = gated(tmp_path, "u1")
    (fix / "src" / "pkg" / "__init__.py").write_text(FIXED)            # right change, never committed
    v = redteam.gate_fix(cfg, job, 1)
    assert (v["verdict"], v["reason"], v["uncommitted"]) == ("rejected", "no_commits", 1)
    assert integration_untouched(job)
    assert (fix / "src" / "pkg" / "__init__.py").read_text() == FIXED   # the fixer's file is left as they had it
    assert git(fix, "status", "--porcelain") != ""                      # and the gate did not commit on their behalf


def test_only_the_committed_part_of_a_fix_is_tested_and_merged(tmp_path: Path):
    _, cfg, job, fix = gated(tmp_path, "u2")
    (fix / "src" / "pkg" / "__init__.py").write_text(FIXED)
    git(fix, "commit", "-qam", "fix add")
    (fix / "src" / "pkg" / "__init__.py").write_text(FIXED + "\nBACKDOOR = True\n")     # edited after the commit
    (fix / "src" / "pkg" / "extra.py").write_text("x = 1\n")                             # untracked
    v = redteam.gate_fix(cfg, job, 1)
    assert v["verdict"] == "merged", v
    integ = Path(job.integration)
    assert "BACKDOOR" not in (integ / "src" / "pkg" / "__init__.py").read_text()
    assert not (integ / "src" / "pkg" / "extra.py").exists()
    assert git(integ, "rev-parse", "HEAD^{tree}") == git(fix, "rev-parse", "HEAD^{tree}")   # merged == tested


@pytest.mark.parametrize("how", ["skip_marker", "delete_file", "rename_test", "skip_at_import"])
def test_a_fix_may_not_drop_or_skip_a_test_that_ran_before(tmp_path: Path, how: str):
    _, cfg, job, fix = gated(tmp_path, f"d-{how}")
    (fix / "src" / "pkg" / "__init__.py").write_text(FIXED)
    green = fix / "tests" / "test_green.py"
    if how == "skip_marker":
        green.write_text("import pytest\nfrom pkg import one\n\n@pytest.mark.skip\ndef test_one():\n    assert one() == 1\n\n"
                         "def test_one_again():\n    assert one() + 1 == 2\n")
        expected = "tests_skipped"
    elif how == "delete_file":
        green.unlink()
        expected = "tests_removed"
    elif how == "rename_test":
        green.write_text(green.read_text().replace("def test_one_again", "def helper_one_again"))
        expected = "tests_removed"
    else:
        green.write_text("import pytest\npytest.skip('later', allow_module_level=True)\n")
        expected = "tests_removed"                                       # a module-level skip reports no test ids
    git(fix, "add", "-A")
    git(fix, "commit", "-qm", "fix add, tidy tests")
    v = redteam.gate_fix(cfg, job, 1)
    assert (v["verdict"], v["reason"]) == ("rejected", expected), v
    assert any("test_green" in t for t in v["tests"]), v
    assert integration_untouched(job)


def test_a_fix_that_leaves_no_test_to_run_is_refused(tmp_path: Path):
    _, cfg, job, fix = gated(tmp_path, "d-all")
    (fix / "src" / "pkg" / "__init__.py").write_text(FIXED)
    (fix / "tests" / "test_green.py").unlink()
    (fix / "tests" / "test_keep.py").unlink()
    git(fix, "add", "-A")
    git(fix, "commit", "-qm", "fix add, remove the suite")
    v = redteam.gate_fix(cfg, job, 1)
    assert (v["verdict"], v["reason"]) == ("rejected", "suite_red"), v      # pytest: no tests collected
    assert integration_untouched(job)


@pytest.mark.parametrize("name,content", [
    ("tests/conftest.py", "def pytest_collection_modifyitems(items):\n    items[:] = [i for i in items if 'one' not in i.name]\n"),
    ("conftest.py", "collect_ignore = ['tests/test_green.py']\n"),
    ("pytest.ini", "[pytest]\naddopts = --deselect tests/test_green.py::test_one\n"),
    ("pyproject.toml", "[tool.pytest.ini_options]\naddopts = '-k add'\n"),
    ("setup.cfg", "[tool:pytest]\naddopts = -k add\n"),
    ("src/sitecustomize.py", "import os\n"),
])
def test_a_fix_may_not_change_what_steers_the_test_run(tmp_path: Path, name: str, content: str):
    _, cfg, job, fix = gated(tmp_path, "c1")
    (fix / "src" / "pkg" / "__init__.py").write_text(FIXED)
    (fix / name).write_text(content)
    git(fix, "add", "-A")
    git(fix, "commit", "-qm", "fix add")
    v = redteam.gate_fix(cfg, job, 1)
    assert (v["verdict"], v["reason"], v["files"]) == ("rejected", "test_infrastructure_modified", [name]), v
    assert integration_untouched(job)


def test_a_shared_config_file_may_change_outside_its_pytest_section(tmp_path: Path):
    _, cfg, job, fix = gated(tmp_path, "c2")
    (fix / "src" / "pkg" / "__init__.py").write_text(FIXED)
    (fix / "pyproject.toml").write_text('[project]\nname = "pkg"\nversion = "0.0.1"\n')
    git(fix, "add", "-A")
    git(fix, "commit", "-qm", "fix add, add metadata")
    assert redteam.gate_fix(cfg, job, 1)["verdict"] == "merged"


def test_existing_tests_a_fix_edited_are_disclosed(tmp_path: Path):
    _, cfg, job, fix = gated(tmp_path, "e1")
    (fix / "src" / "pkg" / "__init__.py").write_text(FIXED)
    green = fix / "tests" / "test_green.py"
    green.write_text(green.read_text().replace("assert one() == 1", "assert one()"))     # weakened, same id
    git(fix, "commit", "-qam", "fix add")
    v = redteam.gate_fix(cfg, job, 1)
    # Not refused (the gate cannot judge the strength of an assertion) but never silent either:
    assert v["verdict"] == "merged" and v["existing_tests_changed"] == ["tests/test_green.py"]
    assert state(job)["rounds"][0]["fix_diff"]["existing_tests_changed"] == ["tests/test_green.py"]
    assert "tests/test_green.py" in redteam.pr_body(cfg, job)


def test_gate_fix_refuses_unknown_merged_and_closed_rounds(tmp_path: Path):
    _, cfg, job, fix = gated(tmp_path, "o1")
    with pytest.raises(RuntimeError, match="not been gated"):
        redteam.gate_fix(cfg, job, 2)
    with pytest.raises(RuntimeError, match="not been gated"):
        redteam.gate_fix(cfg, job, 0)                                    # rounds[-1] must not be reachable
    (fix / "src" / "pkg" / "__init__.py").write_text(FIXED)
    git(fix, "commit", "-qam", "fix add")
    assert redteam.gate_fix(cfg, job, 1)["verdict"] == "merged"
    with pytest.raises(RuntimeError, match="already merged"):
        redteam.gate_fix(cfg, job, 1)
    redteam.close_job(cfg, job, open_pr=False)
    for call in (lambda: redteam.gate_fix(cfg, job, 1), lambda: redteam.gate_findings(cfg, job, 2),
                 lambda: redteam.open_fix(cfg, job, 1)):
        with pytest.raises(RuntimeError, match="closed"):
            call()


# ------------------------------------------------------------------------------------------- gate_findings
def test_findings_cannot_lean_on_files_left_in_the_review_worktree(tmp_path: Path):
    _, cfg = make(tmp_path)
    job = redteam.open_job(cfg, "f1")
    review = Path(job.review)
    (review / "tests" / "conftest.py").write_text("import pkg\npkg.one = lambda: 2\n")          # breaks the product
    (review / "tests" / "helper_mod.py").write_text("VALUE = 2\n")
    (review / "tests" / "review1_conftest.py").write_text(
        '"""severity: high\nfinding: one\n"""\nfrom pkg import one\n\ndef test_one():\n    assert one() == 1\n')
    (review / "tests" / "review1_helper.py").write_text(
        '"""severity: high\nfinding: helper\n"""\nfrom pkg import one\nfrom helper_mod import VALUE\n\n'
        "def test_one():\n    assert one() == VALUE\n")
    g = redteam.gate_findings(cfg, job, 1)
    assert g["accepted"] == []
    assert {r["name"]: r["reason"] for r in g["rejected"]} == {"conftest": "passes_on_head", "helper": "does_not_run"}
    integ = Path(job.integration)
    assert git(integ, "status", "--porcelain") == "" and git(integ, "rev-parse", "HEAD") == job.base_head


def test_symlinks_and_files_already_in_the_head_are_not_evidence(tmp_path: Path):
    repo, cfg = make(tmp_path)
    (repo / "tests" / "review1_old.py").write_text(FINDING_ADD)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "a file that only looks like evidence")
    job = redteam.open_job(cfg, "f2")
    review = Path(job.review)
    outside = tmp_path / "outside.py"
    outside.write_text(FINDING_ADD)
    os.symlink(outside, review / "tests" / "review1_link.py")
    (review / "tests" / "review1_old.py").write_text(FINDING_ADD + "\n# edited by the reviewer\n")
    g = redteam.gate_findings(cfg, job, 1)
    assert g["accepted"] == []
    assert {r["name"]: r["reason"] for r in g["rejected"]} == {"link": "not_a_regular_file", "old": "already_in_head"}


def test_an_undeclared_severity_counts_as_high_and_says_so(tmp_path: Path):
    _, cfg = make(tmp_path)
    job = redteam.open_job(cfg, "s1")
    (Path(job.review) / "tests" / "review1_add.py").write_text(
        '"""finding: add (the author forgot the severity line)\n"""\nfrom pkg import add\n\n'
        "def test_add():\n    assert add(2, 3) == 5\n")
    (Path(job.review) / "tests" / "review1_low.py").write_text(
        '"""Severity : LOW - cosmetic\nfinding: add again\n"""\nfrom pkg import add\n\n'
        "def test_add():\n    assert add(1, 1) == 2\n")
    g = redteam.gate_findings(cfg, job, 1)
    got = {f["name"]: (f["severity"], f["severity_declared"]) for f in g["accepted"]}
    assert got == {"add": ("high", False), "low": ("low", True)} and g["highs"] == 1


@pytest.mark.parametrize("body,reason", [
    ("def test_x():\n    assert 1 == 2\n", "trivial_test"),
    ("def test_x():\n    raise AssertionError('always')\n", "trivial_test"),
    ("import os\n\ndef test_x():\n    assert os.sep == '?'\n", "does_not_import_product"),
    ("from pkg import add\n\ndef test_x():\n    import yaml_that_is_not_there\n    assert add(1, 1) == 2\n", "missing_dependency"),
    ("import pytest\nfrom pkg import add\n\n@pytest.fixture\ndef boom():\n    raise RuntimeError('setup')\n\n"
     "def test_x(boom):\n    assert add(1, 1) == 2\n", "does_not_run"),
])
def test_red_for_a_reason_that_is_not_the_product_is_refused(tmp_path: Path, body: str, reason: str):
    _, cfg = make(tmp_path)
    job = redteam.open_job(cfg, "n1")
    (Path(job.review) / "tests" / "review1_x.py").write_text(f'"""severity: high\nfinding: x\n"""\n{body}')
    g = redteam.gate_findings(cfg, job, 1)
    assert g["accepted"] == [] and [r["reason"] for r in g["rejected"]] == [reason], g


def test_a_missing_product_name_is_a_finding_not_a_missing_dependency(tmp_path: Path):
    _, cfg = make(tmp_path)
    job = redteam.open_job(cfg, "n2")
    (Path(job.review) / "tests" / "review1_api.py").write_text(
        '"""severity: medium\nfinding: pkg.double is documented and absent\n"""\n\n'
        "def test_double():\n    from pkg import double\n    assert double(2) == 4\n")
    assert [f["name"] for f in redteam.gate_findings(cfg, job, 1)["accepted"]] == ["api"]


def test_an_interrupted_evidence_commit_is_settled_on_the_next_call(tmp_path: Path):
    _, cfg = make(tmp_path)
    job = redteam.open_job(cfg, "p1")
    (Path(job.review) / "tests" / "review1_add.py").write_text(FINDING_ADD)
    redteam.gate_findings(cfg, job, 1)
    done = state(job)
    integ = Path(job.integration)

    # (a) killed after `git commit`, before the round was written: the commit is adopted, once
    crashed = dict(done, rounds=[], status="open", pending={"parent": job.base_head, "record": done["rounds"][0]})
    Path(job.path, "job.json").write_text(json.dumps(crashed))
    g = redteam.gate_findings(cfg, job, 1)
    assert g.get("recovered") is True and [f["name"] for f in g["accepted"]] == ["add"]
    after = state(job)
    assert [r["round"] for r in after["rounds"]] == [1] and "pending" not in after
    assert after["rounds"][0]["evidence_head"] == git(integ, "rev-parse", "HEAD")

    # (b) killed after the files were written, before the commit: the tree is put back and the round re-gated
    git(integ, "reset", "-q", "--hard", job.base_head)
    (integ / "tests" / "review1_add.py").write_text("half written")
    crashed["pending"]["record"] = dict(done["rounds"][0], evidence_head=job.base_head)
    Path(job.path, "job.json").write_text(json.dumps(crashed))
    with pytest.raises(RuntimeError, match="unfinished"):
        redteam.open_fix(cfg, job, 1)
    g = redteam.gate_findings(cfg, job, 1)
    assert "recovered" not in g and [f["name"] for f in g["accepted"]] == ["add"]
    assert git(integ, "status", "--porcelain") == "" and (integ / "tests" / "review1_add.py").read_text() == FINDING_ADD


def test_two_rounds_end_to_end_and_the_budget_is_enforced(tmp_path: Path):
    _, cfg, job, fix = gated(tmp_path, "t1", max_rounds=2)
    with pytest.raises(RuntimeError, match="no merged fix"):
        redteam.gate_findings(cfg, job, 2)                               # round 1 is still open
    (fix / "src" / "pkg" / "__init__.py").write_text(FIXED)
    git(fix, "commit", "-qam", "fix add")
    assert redteam.gate_fix(cfg, job, 1)["verdict"] == "merged"
    review = Path(job.review)
    merged = state(job)["rounds"][0]["merged_head"]
    assert git(review, "rev-parse", "HEAD") == merged and git(review, "status", "--porcelain") == ""
    (review / "tests" / "review2_one.py").write_text(
        '"""severity: high\nfinding: one() should be an int subclass check\n"""\nfrom pkg import one\n\n'
        "def test_one_is_two():\n    assert one() == 2\n")
    assert redteam.gate_findings(cfg, job, 2)["highs"] == 1
    fix2 = Path(redteam.open_fix(cfg, job, 2))
    assert git(fix2, "rev-parse", "--abbrev-ref", "HEAD") == "redteam/t1-fix2"
    # this "fix" satisfies round 2 by breaking what the suite pins: refused, and both rounds' evidence was run
    (fix2 / "src" / "pkg" / "__init__.py").write_text(FIXED.replace("return 1", "return 2"))
    git(fix2, "commit", "-qam", "one is two")
    v = redteam.gate_fix(cfg, job, 2)
    assert (v["verdict"], v["reason"]) == ("rejected", "suite_red")
    ran = Path(job.path, "logs", "r2_fix_findings.xml").read_text()
    assert "review1_add" in ran and "review2_one" in ran               # round 1's evidence ran again in round 2
    with pytest.raises(RuntimeError):
        redteam.gate_findings(cfg, job, 3)                               # out of order and over max_rounds
    assert [r["round"] for r in state(job)["rounds"]] == [1, 2]
    assert redteam.should_continue(cfg, job) is False
    body = redteam.pr_body(cfg, job)
    assert "**Open:** round 2" in body                                   # an unfixed high is not presented as done


# ------------------------------------------------------------------------------------------- open / close
@pytest.mark.parametrize("job_id", ["../escape", "a/b", "", ".hidden", "x" * 80, "-rf"])
def test_job_ids_that_are_not_plain_names_are_refused(tmp_path: Path, job_id: str):
    repo, cfg = make(tmp_path)
    with pytest.raises(RuntimeError, match="job id"):
        redteam.open_job(cfg, job_id)
    with pytest.raises(RuntimeError, match="job id"):
        redteam.load_job(cfg, job_id)
    assert not Path(cfg.jobs_root).exists() and git(repo, "branch", "--list", "redteam/*") == ""


def test_open_job_does_not_take_over_an_existing_branch(tmp_path: Path):
    repo, cfg = make(tmp_path)
    git(repo, "branch", "redteam/taken")
    with pytest.raises(RuntimeError, match="already exists"):
        redteam.open_job(cfg, "taken")
    assert git(repo, "branch", "--list", "redteam/taken") != ""          # a failed open deletes only what it made
    assert not (Path(cfg.jobs_root) / "taken").exists()
    assert not Path(cfg.rotation_path).exists()                          # and it used up no cut


def test_a_failed_open_does_not_consume_a_cut(tmp_path: Path, monkeypatch):
    repo, cfg = make(tmp_path)
    monkeypatch.setattr(redteam, "reviewer_brief", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        redteam.open_job(cfg, "j1")
    monkeypatch.undo()
    assert not Path(cfg.rotation_path).exists()
    assert redteam.open_job(cfg, "j1").cut == "a"


def test_close_without_a_pull_request_is_idempotent_and_removes_the_worktrees(tmp_path: Path):
    repo, cfg, job, fix = gated(tmp_path, "z1")
    (fix / "src" / "pkg" / "__init__.py").write_text(FIXED)
    git(fix, "commit", "-qam", "fix add")
    redteam.gate_fix(cfg, job, 1)
    first = redteam.close_job(cfg, job, open_pr=False)
    again = redteam.close_job(cfg, job, open_pr=False)
    assert first["pr"] is None and again["already_closed"] is True and again["head"] == first["head"]
    assert state(job)["status"] == "closed"
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert git(repo, "rev-parse", "redteam/z1") == first["head"]          # the branch stays for the human


def test_scrub_and_git_env_keep_a_secret_out_of_text_and_argv():
    secret = "s3cr3t/with+chars"
    env = redteam._git_env([("credential.helper", redteam._CREDENTIAL_HELPER)], AUTOPOIESIS_GH_TOKEN=secret)
    carried = [k for k, v in env.items() if secret in v]
    assert carried == ["AUTOPOIESIS_GH_TOKEN"]                           # one variable, no config value, no argv
    assert secret not in redteam._CREDENTIAL_HELPER
    assert redteam._scrub(f"fatal: unable to access 'https://x:{secret}@host/'", [secret, ""]) == \
        "fatal: unable to access 'https://x:***@host/'"


# ------------------------------------------------------------------------------------------- receipts
class _HideArgus(importlib.abc.MetaPathFinder):
    """Makes `argus_ledger` unimportable unless `allowed` is on sys.path (whether or not it is installed)."""

    def __init__(self, allowed: Path):
        self.allowed = str(allowed)

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == "argus_ledger" and self.allowed not in sys.path:
            raise ImportError("argus_ledger hidden by the test")
        return None


def _fake_argus(tmp_path: Path) -> Path:
    src = tmp_path / "argus-src"
    (src / "argus_ledger").mkdir(parents=True)
    (src / "argus_ledger" / "__init__.py").write_text("")
    (src / "argus_ledger" / "ledger.py").write_text(
        "import json, types\n\n"
        "class AppendOnlyLedger:\n"
        "    def __init__(self, path):\n        self.path = path\n"
        "    def append(self, event_type, actor, payload):\n"
        "        with open(self.path, 'a') as fh:\n"
        "            fh.write(json.dumps({'event_type': event_type, 'actor': actor, 'payload': payload}) + '\\n')\n"
        "        return types.SimpleNamespace(sequence=sum(1 for _ in open(self.path)))\n")
    return src


@pytest.fixture
def no_argus(tmp_path: Path, monkeypatch):
    src = _fake_argus(tmp_path)
    for name in [m for m in sys.modules if m.split(".")[0] == "argus_ledger"]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "meta_path", [_HideArgus(src), *sys.meta_path])
    monkeypatch.delenv(redteam.ARGUS_SRC_ENV, raising=False)
    yield src
    for name in [m for m in sys.modules if m.split(".")[0] == "argus_ledger"]:
        sys.modules.pop(name, None)                                      # the fake must not outlive the test


def _receipt_cfg(tmp_path: Path, **kw) -> redteam.RedTeamConfig:
    return redteam.RedTeamConfig(repo_path=str(tmp_path), test_command=["true"], cuts=["a"],
                                 rotation_path=str(tmp_path / "r.json"), jobs_root=str(tmp_path / "jobs"),
                                 ledger_path=str(tmp_path / "audit.jsonl"), **kw)


def test_a_missing_ledger_package_is_an_error_that_names_the_setting(tmp_path: Path, no_argus):
    with pytest.raises(ImportError, match="argus_src.*AUTOPOIESIS_ARGUS_SRC"):
        redteam._receipt(_receipt_cfg(tmp_path), "t.event", {"n": 1})
    with pytest.raises(ImportError, match="not a directory"):
        redteam._receipt(_receipt_cfg(tmp_path, argus_src=str(tmp_path / "nowhere")), "t.event", {"n": 1})
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.parametrize("via", ["config", "environment"])
def test_the_ledger_package_is_found_where_configuration_says(tmp_path: Path, no_argus, monkeypatch, via: str):
    if via == "config":
        cfg = _receipt_cfg(tmp_path, argus_src=str(no_argus))
    else:
        monkeypatch.setenv(redteam.ARGUS_SRC_ENV, str(no_argus))
        cfg = _receipt_cfg(tmp_path)
    assert redteam._receipt(cfg, "t.event", {"n": 1, "flag": True, "name": "x"}) == 1
    line = json.loads((tmp_path / "audit.jsonl").read_text())
    assert line["payload"] == {"n": 1, "flag": "True", "name": "x"}      # ints and strings only


def test_the_shipped_config_names_the_argus_location_and_the_loader_passes_it_on():
    import yaml
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "autopoiesis.yaml").read_text())
    section = raw["redteam"]
    assert section["argus_src"] == "/opt/data/work/telarch-suite/argus/src"
    cfg_dict = {k: v for k, v in section.items() if k != "targets"}
    for target in section["targets"].values():                           # what cli.py builds for `redteam --target`
        merged = dict(cfg_dict, **target)
        assert redteam.RedTeamConfig.from_dict(merged).argus_src == section["argus_src"]
