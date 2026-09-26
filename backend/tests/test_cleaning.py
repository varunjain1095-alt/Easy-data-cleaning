import polars as pl
import pytest

from app.analysis import basic, missingness, normalize, typing as type_analysis
from app.config import MISSING_MARKERS, ROW_ID_COLUMN
from app.opspec import OperationSpec, Stage, execute_op

MARKERS = sorted(MISSING_MARKERS)


def _df(**cols):
    data = {ROW_ID_COLUMN: list(range(len(next(iter(cols.values())))))}
    data.update(cols)
    return pl.DataFrame(data)


def _op(op_type, params, stage=Stage.MISSINGNESS):
    return OperationSpec(op_type=op_type, stage=stage, params=params)


# -- missingness assessment ----------------------------------------------------


def test_missingness_counts_markers():
    df = _df(a=[1, None, 3], b=["x", "N/A", " y "], c=["ok", "", "  "])
    result = missingness.assess_missingness(df)
    by_col = {c["column"]: c for c in result["columns"]}
    assert by_col["a"]["missing_count"] == 1
    assert by_col["b"]["missing_count"] == 1  # "N/A"
    assert by_col["b"]["marker_breakdown"].get("n/a") == 1
    assert by_col["c"]["missing_count"] == 2  # "" and "  "
    assert by_col["c"]["marker_breakdown"]["blank"] == 2


def test_missingness_recommendation_skew():
    df = _df(a=[1.0, 1.0, 1.0, 1.0, 100.0, None])
    result = missingness.assess_missingness(df)
    rec = result["columns"][0]["recommendation"]
    assert rec["method"] == "median"
    assert "skew" in rec["reason"]


def test_methods_availability_by_type():
    df = _df(txt=["a", None, "c"], num=[1.0, None, 3.0])
    result = missingness.assess_missingness(df)
    by_col = {c["column"]: c for c in result["columns"]}
    assert "mean" in by_col["num"]["available_methods"]
    assert "mean" not in by_col["txt"]["available_methods"]


# -- treat_missing ops -----------------------------------------------------------


def test_treat_missing_constant_normalizes_markers():
    df = _df(a=["x", "N/A", "y"])
    out = execute_op(df, _op("treat_missing", {"column": "a", "method": "constant", "value": "Z"}))
    assert out["a"].to_list() == ["x", "Z", "y"]


def test_treat_missing_mean_median_mode():
    df = _df(a=[1.0, None, 3.0, 3.0])
    out = execute_op(df, _op("treat_missing", {"column": "a", "method": "mean"}))
    assert out["a"].to_list()[1] == pytest.approx(7 / 3)
    out = execute_op(df, _op("treat_missing", {"column": "a", "method": "median"}))
    assert out["a"].to_list()[1] == 3.0
    df2 = _df(a=["x", None, "x", "y"])
    out = execute_op(df2, _op("treat_missing", {"column": "a", "method": "mode"}))
    assert out["a"].to_list()[1] == "x"


def test_treat_missing_ffill_respects_order():
    df = _df(a=[10.0, None, 30.0], t=[3, 1, 2])  # t defines order
    out = execute_op(df, _op("treat_missing", {"column": "a", "method": "forward_fill", "order_by": "t"}))
    # order by t: row1(t=1,a=None) gets previous in order → none; row2(t=2,a=30) fills... let's check
    # rows in t order: (id1,t1,None), (id2,t2,30), (id0,t3,10). ffill: id1 stays None.
    assert out.sort(ROW_ID_COLUMN)["a"].to_list()[1] is None


def test_treat_missing_interpolate_linear():
    df = _df(a=[0.0, None, None, 6.0])
    out = execute_op(df, _op("treat_missing", {"column": "a", "method": "interpolate_linear"}))
    assert out["a"].to_list() == [0.0, 2.0, 4.0, 6.0]


def test_treat_missing_group_impute():
    df = _df(salary=[100.0, None, 200.0, 400.0, None], dept=["x", "x", "y", "y", "y"])
    out = execute_op(
        df,
        _op("treat_missing", {"column": "salary", "method": "group_impute",
                              "group_by": ["dept"], "statistic": "median"}),
    )
    vals = out["salary"].to_list()
    assert vals[1] == 100.0   # dept x median of [100]
    assert vals[4] == 300.0   # dept y median of [200,400]


def test_treat_missing_random_deterministic():
    df = _df(a=[1.0, None, 3.0, None])
    o1 = execute_op(df, _op("treat_missing", {"column": "a", "method": "random_sample", "seed": 7}))
    o2 = execute_op(df, _op("treat_missing", {"column": "a", "method": "random_sample", "seed": 7}))
    assert o1["a"].to_list() == o2["a"].to_list()
    assert all(v is not None for v in o1["a"].to_list())


def test_unimputable_groups_reported():
    df = _df(a=[1.0, None, None, 2.0], g=["x", "x", "z", "z"])
    res = missingness.unimputable_groups(df, "a", ["g"], MARKERS)
    # group z: [None,2.0] has 1 observed -> imputable; x: [1,None] imputable
    assert res["empty_group_count"] == 0
    df2 = _df(a=[None, None, 2.0], g=["x", "x", "z"])
    res2 = missingness.unimputable_groups(df2, "a", ["g"], MARKERS)
    assert res2["empty_group_count"] == 1


# -- type inference ---------------------------------------------------------------


def test_infer_integer_and_identifier():
    df = _df(zip=["02139", "02140", "94105"], age=[30, 40, 50])
    res = type_analysis.assess_types(df, MARKERS, ["zip", "age"])
    by = {c["column"]: c for c in res["columns"]}
    assert by["zip"]["suggested"] == "string"
    assert by["zip"]["identifier_detected"] is True
    assert by["age"]["suggested"] == "integer"


def test_infer_boolean_and_date():
    df = _df(flag=["Yes", "no", "Y"], d=["2024-01-01", "2024-02-03", "2024-03-05"])
    res = type_analysis.assess_types(df, MARKERS, ["flag", "d"])
    by = {c["column"]: c for c in res["columns"]}
    assert by["flag"]["suggested"] == "boolean"
    assert by["d"]["suggested"] == "date"


def test_ambiguous_date_flagged():
    # 01/02/2024 parses as both day-first and month-first
    df = _df(d=["01/02/2024", "03/04/2024", "05/06/2024"])
    res = type_analysis.infer_column(df, "d", MARKERS)
    assert res["suggested"] == "date"
    assert res["ambiguous_date_format"] is True


def test_convert_type_incompatible_become_null():
    df = _df(a=["1", "x", "3"])
    out = execute_op(df, _op("convert_type", {"column": "a", "target": "integer"}, Stage.TYPES))
    assert out["a"].to_list() == [1, None, 3]
    assert out.schema["a"] == pl.Int64


# -- normalization -----------------------------------------------------------------


def test_normalize_assess_counts():
    df = _df(a=[" x", "y ", "z"], b=["Male", "male", "M"])
    res = normalize.assess_normalization(df)
    by = {c["column"]: c for c in res["columns"]}
    assert by["a"]["safe"]["trim"] == 2
    assert by["b"]["meaning_changing"]["lower"] >= 1
    # "Male"/"male" share normalized form
    assert any("male" in m["variants"] for m in by["b"]["suggested_mappings"])


def test_normalize_ops():
    df = _df(a=["  Hello  World ", "Foo"])
    out = execute_op(
        df,
        _op("normalize", {"columns": ["a"], "operations": [
            {"op": "trim"}, {"op": "collapse_spaces"}, {"op": "lower"}
        ]}, Stage.NORMALIZATION),
    )
    assert out["a"].to_list() == ["hello world", "foo"]


def test_normalize_map_values():
    df = _df(g=["M", "male", "F"])
    out = execute_op(
        df,
        _op("normalize", {"columns": ["g"], "operations": [
            {"op": "map_values", "mapping": {"M": "Male", "male": "Male", "F": "Female"}}
        ]}, Stage.NORMALIZATION),
    )
    assert out["g"].to_list() == ["Male", "Male", "Female"]


# -- basic cleaning -----------------------------------------------------------------


def test_column_name_assessment_and_collision():
    df = _df(**{"First Name": [1], "first  name": [2], "OK": [3]})
    res = basic.column_name_assessment(df)
    mapping = res["proposed_mapping"]
    assert mapping["First Name"] == "first_name"
    assert mapping["first  name"] == "first_name"  # collision!
    assert "first_name" in res["collisions"]
    assert len(res["collisions"]["first_name"]) == 2


def test_structure_assessment():
    df = _df(a=[1, None, 3], b=[None, None, None], c=[5, 5, 5], d=[7, None, None])
    res = basic.structure_assessment(df)
    assert res["empty_columns"] == ["b"]
    # c=[5,5,5] and d=[7,None,None] each have one distinct non-missing value
    assert {c["column"] for c in res["constant_columns"]} == {"c", "d"}
    # row 1: a=None,b=None,c=5,d=None -> not empty (c=5); check counts
    assert res["empty_row_count"] == 0


def test_invalid_range_eval_and_mask():
    df = _df(age=[25, -3, 40, 999])
    res = basic.eval_invalid_rule(df, "age", {"type": "range", "min": 0, "max": 120})
    assert res["invalid_count"] == 2
    out = execute_op(df, _op("mask_values", {"column": "age", "rule": {"type": "range", "min": 0, "max": 120}}, Stage.INVALID_VALUES))
    assert out["age"].to_list() == [25, None, 40, None]


def test_standardize_formats():
    df = _df(f=["Yes", "No", "Y"], p=["10%", "25%", "5%"], m=["$1,200", "$50", "999"])
    out = execute_op(df, _op("standardize_format", {"column": "f", "kind": "boolean", "representation": "bool"}, Stage.INVALID_VALUES))
    assert out["f"].to_list() == [True, False, True]
    out = execute_op(df, _op("standardize_format", {"column": "p", "kind": "percent", "mode": "proportion"}, Stage.INVALID_VALUES))
    assert out["p"].to_list() == [0.10, 0.25, 0.05]
    out = execute_op(df, _op("standardize_format", {"column": "m", "kind": "currency"}, Stage.INVALID_VALUES))
    assert out["m"].to_list() == [1200.0, 50.0, 999.0]


def test_special_chars_assessment():
    from app.analysis.special_chars import assess_special_chars
    df = _df(name=["José@", "pri#ce", "ok"], val=["a", "b", "c"])
    df = df.rename({"name": "Na@me"})
    res = assess_special_chars(df)
    names = {n["column"]: n for n in res["column_names"]}
    assert "Na@me" in names
    assert "@" in names["Na@me"]["chars"]
    assert names["Na@me"]["proposed"] == "na_me"
    cols = {c["column"]: c for c in res["columns"]}
    assert "Na@me" in cols
    chars = {x["char"] for x in cols["Na@me"]["chars"]}
    assert "@" in chars and "#" in chars
    assert "val" not in cols  # clean column not reported


def test_clean_special_chars_ops():
    df = _df(price=["19$", "2$", "3$"], note=["a@b", None, "c"])
    out = execute_op(df, _op("clean_special_chars", {"column": "price", "chars": ["$"], "action": "strip"}, Stage.SPECIAL_CHARS))
    assert out["price"].to_list() == ["19", "2", "3"]
    out = execute_op(df, _op("clean_special_chars", {"column": "note", "chars": ["@"], "action": "replace", "replacement": "at"}, Stage.SPECIAL_CHARS))
    assert out["note"].to_list() == ["aatb", None, "c"]
    out = execute_op(df, _op("clean_special_chars", {"column": "note", "chars": ["@"], "action": "blank"}, Stage.SPECIAL_CHARS))
    assert out["note"].to_list() == [None, None, "c"]
    # non-string column rejected
    dfi = _df(age=[25, -3, 40, 999])
    try:
        execute_op(dfi, _op("clean_special_chars", {"column": "age", "chars": ["@"], "action": "strip"}, Stage.SPECIAL_CHARS))
        assert False
    except ValueError:
        pass


def test_special_chars_op_replay_deterministic():
    df = _df(x=["a@b", "c#d", None])
    spec = _op("clean_special_chars", {"column": "x", "chars": ["@", "#"], "action": "strip"}, Stage.SPECIAL_CHARS)
    a = execute_op(df, spec)
    b = execute_op(df, spec)
    assert a.equals(b)
