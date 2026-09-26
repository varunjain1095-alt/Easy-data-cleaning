"""Units & scales, Patterns, and Keys & uniqueness stage tests."""

import polars as pl
import pytest

from app.analysis import keys, patterns, units
from app.masking import markers_param
from app.opspec import OperationSpec, execute_op

MARKERS = markers_param(None)


def _df(vals, col="v"):
    return pl.DataFrame({col: vals})


# --- Units -----------------------------------------------------------------

def test_units_detects_prefix_and_suffix():
    df = _df(["5kg", "10 lb", "€100", "USD 50", "25%", "junk"])
    r = units.assess_units(df, "v", MARKERS)
    by = {g["canonical"]: g for g in r["groups"]}
    assert by["kg"]["count"] == 1 and by["kg"]["position"] == "suffix"
    assert by["lb"]["position"] == "suffix"
    assert by["€"]["position"] == "prefix"
    assert by["usd"]["position"] == "prefix"
    assert by["%"]["position"] == "suffix"
    assert r["unmatched"]["count"] == 1


def test_units_case_and_spacing_variants():
    df = _df(["5 kg", "5KG", " 7Kg "])
    r = units.assess_units(df, "v", MARKERS)
    assert len(r["groups"]) == 1
    assert r["groups"][0]["canonical"] == "kg"
    assert r["groups"][0]["count"] == 3


def test_units_numeric_scale_hint():
    df = _df([0.1, 0.2, 0.5, 10.0, 20.0, 50.0, 1.0, 0.9, 30.0, 0.7, 0.8, 15.0])
    r = units.assess_units(df, "v", MARKERS)
    assert r["scale_hint"] and r["scale_hint"]["kind"] == "possible_mixed_scale"


def test_convert_units_mixed_keeps_string_dtype():
    df = _df(["5kg", "junk"])
    r = execute_op(df, OperationSpec(op_type="convert_units", stage="units",
        params={"column": "v", "rules": [{"unit": "kg", "factor": 1.0}]}))
    assert r.schema["v"] == pl.String
    assert r["v"].to_list() == ["5.0", "junk"]


def test_convert_units_full_goes_float():
    df = _df(["5kg", "10 lb", "30"])
    r = execute_op(df, OperationSpec(op_type="convert_units", stage="units",
        params={"column": "v", "rules": [{"unit": "kg", "factor": 1.0},
                                          {"unit": "lb", "factor": 0.453592}]}))
    assert r.schema["v"] == pl.Float64
    assert r["v"].to_list()[0] == 5.0
    assert abs(r["v"].to_list()[1] - 4.53592) < 1e-5
    assert r["v"].to_list()[2] == 30.0


def test_convert_units_scale_condition():
    df = _df([0.25, 25.0])
    r = execute_op(df, OperationSpec(op_type="convert_units", stage="units",
        params={"column": "v", "rules": [{"condition": {"op": "<=", "value": 1}, "factor": 100}]}))
    assert r["v"].to_list() == [25.0, 25.0]


# --- Patterns ---------------------------------------------------------------

def test_pattern_assess_reports_nonmatching():
    df = _df(["a@b.com", "bad", "c@d.org", "nope"])
    r = patterns.assess_pattern(df, "v", "email", MARKERS)
    assert r["matching"] == 2 and r["non_matching"] == 2
    assert set(r["samples"]) == {"bad", "nope"}


def test_pattern_invalid_regex_rejected():
    assert patterns.validate_pattern("([") is not None
    assert patterns.validate_pattern("x" * 400) is not None
    assert patterns.validate_pattern("(?=a)b") is not None  # lookahead rejected
    assert patterns.validate_pattern(r"^\d+$") is None


def test_clean_pattern_set_missing():
    df = _df(["a@b.com", "bad"])
    r = execute_op(df, OperationSpec(op_type="clean_pattern", stage="patterns",
        params={"column": "v", "action": "set_missing",
                "pattern": patterns.PATTERN_PRESETS["email"]}))
    assert r["v"].to_list() == ["a@b.com", None]


def test_clean_pattern_extract_validates_group():
    df = _df(["ID-0042"])
    with pytest.raises(ValueError, match="capture group"):
        execute_op(df, OperationSpec(op_type="clean_pattern", stage="patterns",
            params={"column": "v", "action": "extract", "pattern": r"^ID-(\d+)$", "group": 2}))
    r = execute_op(df, OperationSpec(op_type="clean_pattern", stage="patterns",
        params={"column": "v", "action": "extract", "pattern": r"^ID-(\d+)$", "group": 1}))
    assert r["v"].to_list() == ["0042"]


def test_clean_pattern_pad_and_strip_affix():
    df = _df(["42", "X-9"])
    r = execute_op(df, OperationSpec(op_type="clean_pattern", stage="patterns",
        params={"column": "v", "action": "pad", "length": 5}))
    assert r["v"].to_list() == ["00042", "00X-9"]
    df2 = _df(["PRE-10", "PRE-11"])
    r2 = execute_op(df2, OperationSpec(op_type="clean_pattern", stage="patterns",
        params={"column": "v", "action": "strip_affix", "prefix": "PRE-"}))
    assert r2["v"].to_list() == ["10", "11"]


def test_clean_pattern_rejects_numeric_column():
    df = _df([1, 2])
    with pytest.raises(ValueError, match="text columns"):
        execute_op(df, OperationSpec(op_type="clean_pattern", stage="patterns",
            params={"column": "v", "action": "pad", "length": 5}))


# --- Keys -------------------------------------------------------------------

def test_keys_three_buckets():
    df = pl.DataFrame({
        "id": ["A1", "A1", "A1", None, "B2"],
        "name": ["x", "x", "y", "z", "q"],
        "__qdc_row_id": [1, 2, 3, 4, 5],
    })
    r = keys.assess_keys(df, ["id"], MARKERS)
    assert r["missing"]["count"] == 1
    # A1 x3: rows 1,2 identical? no — row3 has name=y -> all three form one group with conflict
    assert r["repeated_conflicting"]["count"] == 1
    assert "name" in r["repeated_conflicting"]["groups"][0]["conflicts"]


def test_keys_identical_vs_conflicting():
    df = pl.DataFrame({
        "id": ["A1", "A1", "B2", "B2"],
        "name": ["x", "x", "p", "q"],
        "__qdc_row_id": [1, 2, 3, 4],
    })
    r = keys.assess_keys(df, ["id"], MARKERS)
    assert r["repeated_identical"]["count"] == 1   # A1/A1 same name
    assert r["repeated_conflicting"]["count"] == 1  # B2 with p vs q


def test_keys_string_id_leading_zeros_not_coerced():
    df = pl.DataFrame({
        "id": ["00417", "417"],
        "name": ["a", "b"],
        "__qdc_row_id": [1, 2],
    })
    r = keys.assess_keys(df, ["id"], MARKERS)
    assert r["repeated_identical"]["count"] == 0
    assert r["repeated_conflicting"]["count"] == 0
    assert r["unique_rows"] == 2


def test_keys_composite():
    df = pl.DataFrame({
        "o": [1, 1, 2],
        "p": ["a", "a", "b"],
        "v": [10, 11, 12],
        "__qdc_row_id": [1, 2, 3],
    })
    r = keys.assess_keys(df, ["o", "p"], MARKERS)
    assert r["repeated_conflicting"]["count"] == 1
    assert r["repeated_conflicting"]["groups"][0]["key"] == {"o": 1, "p": "a"}
