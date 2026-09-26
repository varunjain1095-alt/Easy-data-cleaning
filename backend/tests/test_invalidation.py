from app.invalidation import apply_invalidation
from app.opspec import Stage


def _states(**overrides):
    states = {s: "not_started" for s in (
        Stage.MISSINGNESS, Stage.TYPES, Stage.NORMALIZATION, Stage.COLUMN_NAMES,
        Stage.INVALID_VALUES, Stage.STRUCTURES, Stage.DUPLICATES, Stage.OUTCOME,
        Stage.UNIVARIATE, Stage.BIVARIATE, Stage.VALIDATION, Stage.STATISTICS,
    )}
    states.update(overrides)
    return states


def test_missingness_invalidates_dependents():
    states = _states(types="completed", duplicates="completed", validation="completed")
    marked = apply_invalidation(states, Stage.MISSINGNESS)
    assert set(marked) == {Stage.TYPES, Stage.DUPLICATES, Stage.VALIDATION}
    assert states[Stage.TYPES] == "needs_recalculation"
    assert states[Stage.NORMALIZATION] == "not_started"  # untouched


def test_only_completed_stages_marked():
    states = _states(duplicates="in_progress", validation="skipped")
    marked = apply_invalidation(states, Stage.MISSINGNESS)
    assert Stage.DUPLICATES not in marked
    assert Stage.VALIDATION not in marked


def test_outcome_change_only_invalidates_bivariate():
    states = _states(bivariate="completed", univariate="completed", validation="completed")
    marked = apply_invalidation(states, Stage.OUTCOME)
    assert marked == [Stage.BIVARIATE]
    assert states[Stage.UNIVARIATE] == "completed"


def test_unknown_stage_invalidates_nothing():
    states = _states(validation="completed")
    assert apply_invalidation(states, "nonexistent_stage") == []
