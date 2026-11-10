"""severity: high
finding: GitPromotionManager.promote_mutation ignores the result of `git merge --ff-only` and ignores `release_branch`: it returns a PromotionReceipt and writes an attestation card for a commit that is not on the release branch (and the engine then deletes the only branch holding it)

deployment.py:147-150 runs the merge with no `check` and no look at `returncode`, in whatever branch happens to
be checked out in `repo_path`; `self.release_branch` (deployment.py:53) is never read. deployment.py:152-172
then records `merged_at`, the Argus event and `.autopoiesis/attestations/<release>.json` unconditionally.
engine.py:176-177 reports "promoted" and engine.py:189-191 force-removes the sandbox and its branch.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from autopoiesis.deployment import GitPromotionManager
from autopoiesis.models import MutationProposal, ParetoVerdict


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True, text=True)


def setup(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@example.com")
    (repo / "app.txt").write_text("v1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "candidate", str(wt), "main")
    (wt / "app.txt").write_text("v2\n")
    mutation = MutationProposal(proposal_id="mut_rv1110", cluster_id="c1", target_repo="repo", target_files=["app.txt"],
                                diff="x", branch_name="candidate", worktree_path=str(wt), rationale="r")
    verdict = ParetoVerdict(candidate_id="mut_rv1110", is_pareto_dominant=True, zero_regression=True,
                            defect_eliminated=True, performance_acceptable=True, security_verified=True)
    return repo, mutation, verdict


def on_main(repo: Path, sha: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", sha, "main", check=False).returncode == 0


def cards(repo: Path) -> list:
    return sorted(p.name for p in (repo / ".autopoiesis" / "attestations").glob("*.json"))


def test_control_fast_forward_promotion_lands_on_main(tmp_path: Path):
    repo, mutation, verdict = setup(tmp_path)
    receipt = GitPromotionManager(repo, release_branch="main").promote_mutation(mutation, verdict)
    assert on_main(repo, receipt.commit_sha) and cards(repo) == [f"{receipt.release_id}.json"]


def test_no_receipt_when_the_fast_forward_merge_fails(tmp_path: Path):
    repo, mutation, verdict = setup(tmp_path)
    (repo / "other.txt").write_text("someone else committed meanwhile\n")      # main moved: --ff-only must fail
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "concurrent")
    try:
        receipt = GitPromotionManager(repo, release_branch="main").promote_mutation(mutation, verdict)
    except Exception:
        receipt = None                                      # raising is the correct outcome
    if receipt is not None:
        assert on_main(repo, receipt.commit_sha), (
            f"receipt {receipt.release_id} merged_at={receipt.merged_at} for a commit that is not on main")
    assert cards(repo) == [], f"attestation card written for a promotion that did not happen: {cards(repo)}"


def test_promotion_goes_to_the_configured_release_branch(tmp_path: Path):
    repo, mutation, verdict = setup(tmp_path)
    git(repo, "checkout", "-q", "-b", "dev")                # the operator's checkout is on another branch
    try:
        receipt = GitPromotionManager(repo, release_branch="main").promote_mutation(mutation, verdict)
    except Exception:
        return                                              # refusing is correct
    dev_moved = git(repo, "rev-parse", "dev").stdout.strip() == receipt.commit_sha
    assert on_main(repo, receipt.commit_sha), f"release_branch='main' but main is untouched (dev moved: {dev_moved})"
