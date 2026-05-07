"""
Issue classification and Devin prompt construction.

This file is what makes the project a "remediation control plane" rather
than a thin API wrapper. It encodes the policy: which kind of bug is
this, and what does Devin need to know to fix it without going off the
rails?

Classification is intentionally a tiny keyword matcher. It is good
enough for the take-home demo and easy to extend.
"""

from __future__ import annotations

from .models import IssueKind


# Keyword sets per kind. First match wins; order matters because some
# keywords (like "cache") are deliberately broad.
_CLASSIFIERS: list[tuple[IssueKind, tuple[str, ...]]] = [
    (
        IssueKind.IMPORT_STALE_CHART_REFS,
        ("timed_refresh_immune_slices", "expanded_slices", "stale chart"),
    ),
    (
        IssueKind.IMPORT_MISSING_DATASET,
        ("datasetuuid", "dataset_info", "missing native filter dataset"),
    ),
    (
        IssueKind.FRONTEND_CACHE_BOUND,
        ("drilldetailpane", "samples_row_limit", "cachepagelimit", "cache"),
    ),
]


def classify_issue(title: str, body: str) -> str:
    """Return the IssueKind value for an issue.

    Matching is case-insensitive against the concatenated title and body.
    """
    text = f"{title or ''}\n{body or ''}".lower()
    for kind, keywords in _CLASSIFIERS:
        if any(keyword in text for keyword in keywords):
            return kind.value
    return IssueKind.GENERAL.value


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #

_BASE_TEMPLATE = """You are working in my copied Apache Superset repository:

{repo_url}

Please remediate GitHub issue #{issue_number}:

{issue_url}

Issue title:
{title}

Issue body:
{body}

Repository instructions:
- Branch from the target base branch: {base_branch}
- Create a new branch named: devin/api-issue-{issue_number}
- Open the pull request against: {base_branch}
- Do not use or depend on any existing open or closed Devin PR branches.

Core instructions:
1. Read the issue and inspect the referenced files before changing code.
2. Implement the smallest safe fix that satisfies the acceptance criteria.
3. Add or update targeted regression tests where feasible.
4. Run the most relevant targeted tests/checks you can reasonably run.
5. Open a pull request.
6. In the PR body, include:
   - Summary of changes
   - Tests/checks run
   - Risk notes
   - Link to the issue

Constraints:
- Do not make broad refactors.
- Do not include unrelated changes.
- Preserve existing valid behavior.
- If full repository CI is too expensive or unavailable, run targeted validation and explain what was run.
- If blocked, explain exactly what blocked you and what a human should do next.
"""


_ACCEPTANCE_CRITERIA: dict[str, str] = {
    IssueKind.IMPORT_STALE_CHART_REFS.value: """Issue-specific acceptance criteria:
- `timed_refresh_immune_slices` containing [1, 999] with id_map {1: 101} becomes [101].
- `expanded_slices` containing {"1": true, "999": true} with id_map {1: 101} becomes {"101": true}.
- No KeyError is raised for stale chart IDs.
- Existing valid chart remapping behavior is preserved.
- Prefer nearby defensive remapping patterns already used in the importer.
""",
    IssueKind.IMPORT_MISSING_DATASET.value: """Issue-specific acceptance criteria:
- A native filter target with a datasetUuid missing from dataset_info does not produce a bare KeyError.
- The raised error is import-specific and includes the missing dataset UUID.
- Existing valid datasetUuid remapping behavior remains unchanged.
- Do not silently drop dataset-backed filter targets unless existing importer policy clearly requires that.
""",
    IssueKind.FRONTEND_CACHE_BOUND.value: """Issue-specific acceptance criteria:
- DrillDetailPane cachePageLimit remains finite when SAMPLES_ROW_LIMIT is undefined.
- cachePageLimit is at least 1.
- Existing configured SAMPLES_ROW_LIMIT behavior remains unchanged.
- Cache trimming remains bounded.
- Keep the change narrowly scoped to cachePageLimit calculation and tests.
""",
}


def build_prompt(
    repo_url: str,
    base_branch: str,
    issue_number: int,
    issue_url: str,
    title: str,
    body: str,
) -> tuple[str, str]:
    """Build the full Devin prompt for an issue.

    Returns (issue_kind, full_prompt). `issue_kind` is exposed so the
    caller can persist it to the tasks table without re-classifying.
    """
    issue_kind = classify_issue(title, body)
    base = _BASE_TEMPLATE.format(
        repo_url=repo_url,
        base_branch=base_branch or "main",
        issue_number=issue_number,
        issue_url=issue_url,
        title=title or "",
        body=body or "",
    )
    extra = _ACCEPTANCE_CRITERIA.get(issue_kind, "")
    full_prompt = f"{base}\n{extra}" if extra else base
    return issue_kind, full_prompt
