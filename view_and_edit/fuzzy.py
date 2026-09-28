"""Rank paths against a typed query, like VS Code's Ctrl+P."""

from __future__ import annotations

from collections.abc import Iterable

# Only the best matches are listed (and stat'ed); the header gives the full count.
MAX_RESULTS = 200
# Score weights: a match inside the file name beats one spread over folders, and
# consecutive characters beat scattered ones. Path length only breaks ties.
_NAME_MATCH = 1000
_NAME_SUBSTRING = 500
_NAME_PREFIX = 300
_RUN_BONUS = 10
_LENGTH_PENALTY = 0.01


def _subsequence_runs(query: str, text: str) -> int | None:
    """How many query characters directly follow the previous match, or None if the
    query's characters do not all appear in `text` in order."""
    runs = 0
    start = 0
    previous: int | None = None
    for ch in query:
        found = text.find(ch, start)
        if found < 0:
            return None
        if previous is not None and found == previous + 1:
            runs += 1
        previous = found
        start = found + 1
    return runs


def fuzzy_score(query: str, path: str) -> float | None:
    """How well `path` matches `query` (higher is better), or None when it does not.

    Every character of the query must appear in the path in order, case-insensitively.
    Spaces in the query are ignored so `app test` finds `app_test.py`.
    """
    needle = "".join(query.lower().split())
    if not needle:
        return None
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    penalty = len(path) * _LENGTH_PENALTY
    if "/" not in needle:
        name_runs = _subsequence_runs(needle, name)
        if name_runs is not None:
            score = _NAME_MATCH + name_runs * _RUN_BONUS - penalty
            if needle in name:
                score += _NAME_SUBSTRING
                if name.startswith(needle):
                    score += _NAME_PREFIX
            return score
    runs = _subsequence_runs(needle, lowered)
    if runs is None:
        return None
    return runs * _RUN_BONUS - penalty


def rank(query: str, paths: Iterable[str], limit: int = MAX_RESULTS) -> tuple[list[str], int]:
    """The best `limit` matches, best first, and how many paths matched in total.

    With an empty query, the shallowest paths come first (the top of the project).
    """
    if not query.strip():
        ordered = sorted(paths, key=lambda path: (path.count("/"), path))
        return ordered[:limit], len(ordered)
    scored = []
    for path in paths:
        score = fuzzy_score(query, path)
        if score is not None:
            scored.append((-score, path))
    scored.sort()
    return [path for _, path in scored[:limit]], len(scored)
