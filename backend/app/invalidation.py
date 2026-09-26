"""Dependency-invalidation engine implementing architecture section 15.

When an earlier operation changes data used by a later stage, dependent
results are marked 'needs_recalculation'. Stale results are never silently
retained or presented as current.
"""

from .opspec import Stage

# stage that changed -> dependent results invalidated
DEPENDENCY_MAP: dict[str, set[str]] = {
    Stage.SPECIAL_CHARS: {
        Stage.MISSINGNESS,
        Stage.UNITS,
        Stage.TYPES,
        Stage.NORMALIZATION,
        Stage.INVALID_VALUES,
        Stage.PATTERNS,
        Stage.DUPLICATES,
        Stage.KEYS,
        Stage.STATISTICS,
        Stage.UNIVARIATE,
        Stage.BIVARIATE,
        Stage.VALIDATION,
    },
    Stage.MISSINGNESS: {
        Stage.UNITS,
        Stage.TYPES,
        Stage.NORMALIZATION,
        Stage.PATTERNS,
        Stage.DUPLICATES,
        Stage.KEYS,
        Stage.STATISTICS,
        Stage.UNIVARIATE,
        Stage.BIVARIATE,
        Stage.VALIDATION,
    },
    Stage.UNITS: {
        Stage.TYPES,
        Stage.NORMALIZATION,
        Stage.INVALID_VALUES,
        Stage.PATTERNS,
        Stage.DUPLICATES,
        Stage.KEYS,
        Stage.STATISTICS,
        Stage.VALIDATION,
    },
    Stage.TYPES: {
        Stage.NORMALIZATION,
        Stage.INVALID_VALUES,
        Stage.PATTERNS,
        Stage.DUPLICATES,
        Stage.KEYS,
        Stage.STATISTICS,
        Stage.UNIVARIATE,
        Stage.BIVARIATE,
        Stage.VALIDATION,
    },
    Stage.NORMALIZATION: {
        Stage.MISSINGNESS,
        Stage.INVALID_VALUES,
        Stage.PATTERNS,
        Stage.DUPLICATES,
        Stage.KEYS,
        Stage.VALIDATION,
    },
    Stage.COLUMN_NAMES: set(),  # column references remapped when unambiguous; else flagged for review
    Stage.INVALID_VALUES: {
        Stage.MISSINGNESS,
        Stage.PATTERNS,
        Stage.STATISTICS,
        Stage.DUPLICATES,
        Stage.KEYS,
        Stage.UNIVARIATE,
        Stage.BIVARIATE,
        Stage.VALIDATION,
    },
    Stage.STRUCTURES: {
        Stage.DUPLICATES,
        Stage.KEYS,
        Stage.STATISTICS,
        Stage.UNIVARIATE,
        Stage.BIVARIATE,
        Stage.VALIDATION,
    },
    Stage.PATTERNS: {
        Stage.DUPLICATES,
        Stage.KEYS,
        Stage.STATISTICS,
        Stage.VALIDATION,
    },
    Stage.DUPLICATES: {
        Stage.KEYS,
        Stage.STATISTICS,
        Stage.UNIVARIATE,
        Stage.BIVARIATE,
        Stage.VALIDATION,
    },
    Stage.KEYS: {
        Stage.STATISTICS,
        Stage.VALIDATION,
    },
    Stage.OUTCOME: set(),  # invalidates outcome-focused bivariate results only, handled via flag
    Stage.UNIVARIATE: set(),
    Stage.BIVARIATE: {
        Stage.STATISTICS,
        Stage.VALIDATION,
    },
}

# Statuses that may be demoted to needs_recalculation when invalidated.
INVALIDATABLE = {"completed", "completed_with_warnings"}


def invalidated_stages(changed_stage: str) -> set[str]:
    return DEPENDENCY_MAP.get(changed_stage, set())


def apply_invalidation(stage_states: dict[str, str], changed_stage: str, outcome_focused_only: bool = False) -> list[str]:
    """Mutate stage_states in place; return the list of stages marked for recalculation."""
    marked: list[str] = []
    if changed_stage == Stage.OUTCOME:
        # Outcome-variable changes invalidate outcome-focused bivariate results only.
        if stage_states.get(Stage.BIVARIATE) in INVALIDATABLE:
            stage_states[Stage.BIVARIATE] = "needs_recalculation"
            marked.append(Stage.BIVARIATE)
        return marked
    for stage in invalidated_stages(changed_stage):
        if stage_states.get(stage) in INVALIDATABLE:
            stage_states[stage] = "needs_recalculation"
            marked.append(stage)
    return marked
