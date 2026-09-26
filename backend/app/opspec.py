import random
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

import polars as pl

from .config import MISSING_MARKERS, ROW_ID_COLUMN
from .security import new_id


class Stage(str):
    """Workflow stages used for dependency invalidation (architecture section 15)."""

    SPECIAL_CHARS = "special_chars"
    MISSINGNESS = "missingness"
    UNITS = "units"
    TYPES = "types"
    NORMALIZATION = "normalization"
    COLUMN_NAMES = "column_names"
    INVALID_VALUES = "invalid_values"
    STRUCTURES = "structures"
    PATTERNS = "patterns"
    DUPLICATES = "duplicates"
    KEYS = "keys"
    OUTCOME = "outcome"
    UNIVARIATE = "univariate"
    BIVARIATE = "bivariate"
    VALIDATION = "validation"
    STATISTICS = "statistics"


@dataclass
class OperationSpec:
    """Deterministic specification of one confirmed transformation.

    This is the unit of record, replay, undo/redo, and reproducible export.
    """

    op_type: str
    stage: str
    params: dict[str, Any]
    target_columns: list[str] = field(default_factory=list)
    op_id: str = field(default_factory=new_id)
    seq: int = 0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "OperationSpec":
        return cls(**data)


# ---------------------------------------------------------------------------
# Operation executors: op_type -> fn(df, params) -> df
# All ops are deterministic pure functions of (df, params).
# ---------------------------------------------------------------------------

_BOOL_TRUE = {"y", "yes", "true", "t", "1"}
_BOOL_FALSE = {"n", "no", "false", "f", "0"}


def _op_rename_column(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    return df.rename({params["from"]: params["to"]})


def _op_rename_columns(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    return df.rename(params["mapping"])


def _op_drop_rows(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    return df.filter(~pl.col(ROW_ID_COLUMN).is_in(params["row_ids"]))


def _op_drop_column(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    return df.drop(params["column"])


def _op_fill_null_constant(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    return df.with_columns(pl.col(params["column"]).fill_null(params["value"]))


def _markers(params: dict) -> list[str]:
    return list(params.get("markers") or sorted(MISSING_MARKERS))


def _is_missing_expr(col: str, markers: list[str]) -> pl.Expr:
    s = pl.col(col).cast(pl.String)
    return pl.col(col).is_null() | (s.str.strip_chars().str.to_lowercase().is_in(markers) & s.is_not_null())


def _blank_to_null(df: pl.DataFrame, col: str, markers: list[str]) -> pl.DataFrame:
    """Normalize recognized missing markers in a string column to real nulls."""
    if col not in df.columns or df.schema[col] != pl.String:
        return df
    mask = pl.col(col).str.strip_chars().str.to_lowercase().is_in(markers)
    return df.with_columns(
        pl.when(mask).then(pl.lit(None).cast(pl.String)).otherwise(pl.col(col)).alias(col)
    )


def _numeric_series(df: pl.DataFrame, col: str) -> pl.Series:
    """Numeric view of a column's non-null values (string columns are parsed).

    Handles decimal-comma formats ("2,6") common in European datasets: values
    that fail direct parsing but match the decimal-comma pattern are retried
    with the comma converted to a dot.
    """
    s = df[col].drop_nulls()
    if df.schema[col] == pl.String:
        s = s.str.strip_chars()
        parsed = s.cast(pl.Float64, strict=False)
        comma_mask = s.str.contains(r"^-?\d+,\d+$")
        if bool((parsed.is_null() & comma_mask).any()):
            fixed = s.str.replace_all(",", ".").cast(pl.Float64, strict=False)
            parsed = pl.select(
                pl.when(parsed.is_null() & comma_mask).then(fixed).otherwise(parsed)
            ).to_series()
        s = parsed.drop_nulls()
    return s


def _ordered(df: pl.DataFrame, order_by: str | None) -> pl.DataFrame:
    if order_by and order_by in df.columns:
        return df.sort([order_by, ROW_ID_COLUMN])
    return df.sort(ROW_ID_COLUMN)


def _fill_series_linear(values: list, positions: list[float]) -> list:
    """Linear interpolation over arbitrary positions (index or timestamps)."""
    n = len(values)
    known = [(positions[i], values[i]) for i in range(n) if values[i] is not None]
    if len(known) < 2:
        return values
    out = list(values)
    for i in range(n):
        if out[i] is None:
            x = positions[i]
            lo = max((k for k in known if k[0] <= x), key=lambda k: k[0], default=None)
            hi = min((k for k in known if k[0] >= x), key=lambda k: k[0], default=None)
            if lo is None or hi is None:
                continue  # cannot extrapolate at edges
            if hi[0] == lo[0]:
                out[i] = lo[1]
            else:
                frac = (x - lo[0]) / (hi[0] - lo[0])
                out[i] = lo[1] + frac * (hi[1] - lo[1])
    return out


def _op_clean_special_chars(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """Handle special characters in a string column.

    params: column, chars (list[str]), action: strip | replace | blank,
            replacement? (used by 'replace')
    - strip:   remove each listed character from values
    - replace: substitute each listed character with `replacement`
    - blank:   set the whole cell to null if it contains any listed character
    """
    col = params["column"]
    chars = params["chars"]
    action = params["action"]
    if df.schema[col] != pl.String:
        raise ValueError(f"clean_special_chars applies to String columns; '{col}' is {df.schema[col]}")
    if not chars:
        raise ValueError("No characters selected")
    if action == "blank":
        pat = "|".join(re.escape(c) for c in chars)
        return df.with_columns(
            pl.when(pl.col(col).str.contains(pat))
            .then(pl.lit(None).cast(pl.String))
            .otherwise(pl.col(col))
            .alias(col)
        )
    expr = pl.col(col)
    for ch in chars:
        repl = "" if action == "strip" else str(params.get("replacement", ""))
        expr = expr.str.replace_all(re.escape(ch), repl, literal=False)
    return df.with_columns(expr.alias(col))


def _op_treat_missing(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """Missingness treatments (architecture section 3.2). Deterministic.

    params: column, method, markers?, value?, statistic?, group_by?,
            order_by?, seed?
    methods: drop_rows | drop_column | constant | mean | median | mode |
             forward_fill | backward_fill | interpolate_linear |
             interpolate_time | group_impute | random_sample
    """
    col = params["column"]
    method = params["method"]
    markers = _markers(params)

    if method == "drop_column":
        return df.drop(col)
    if method == "drop_rows":
        return df.filter(~_is_missing_expr(col, markers))

    df = _blank_to_null(df, col, markers)

    if method == "constant":
        # constants are validated/coerced to the column's dtype
        val = params["value"]
        try:
            val = pl.Series([val]).cast(df.schema[col], strict=False)[0]
        except Exception:
            pass
        return df.with_columns(pl.col(col).fill_null(val))
    if method in ("mean", "median", "mode"):
        s = _numeric_series(df, col) if method != "mode" else df[col].drop_nulls()
        if s.len() == 0:
            raise ValueError(
                f"Cannot compute {method} on '{col}': no parseable numeric values "
                "(convert the column's type first if it holds non-numeric text)"
            )
        val = getattr(s, method)()
        if method == "mode":
            val = s.mode().sort().first()
        try:
            val = pl.Series([val]).cast(df.schema[col], strict=False)[0]
        except Exception:
            pass
        return df.with_columns(pl.col(col).fill_null(val))
    if method in ("forward_fill", "backward_fill"):
        order_by = params.get("order_by")
        ordered = _ordered(df, order_by)
        expr = pl.col(col).forward_fill() if method == "forward_fill" else pl.col(col).backward_fill()
        ordered = ordered.with_columns(expr.alias(col))
        return ordered.sort(ROW_ID_COLUMN) if order_by else ordered
    if method in ("interpolate_linear", "interpolate_time"):
        order_by = params.get("order_by")
        ordered = _ordered(df, order_by)
        vals = _numeric_series(ordered, col)
        if vals.len() < 2:
            return ordered.sort(ROW_ID_COLUMN) if order_by else ordered
        raw = ordered[col].to_list()
        nums = []
        for v in raw:
            try:
                nums.append(float(v) if v is not None else None)
            except (TypeError, ValueError):
                nums.append(None)
        if method == "interpolate_time" and order_by:
            pos_raw = ordered[order_by].to_list()
            positions = [
                (p.timestamp() if hasattr(p, "timestamp") else float(i))
                for i, p in enumerate(pos_raw)
            ]
        else:
            positions = [float(i) for i in range(len(nums))]
        filled = _fill_series_linear(nums, positions)
        ordered = ordered.with_columns(pl.Series(col, filled, dtype=pl.Float64))
        return ordered.sort(ROW_ID_COLUMN) if order_by else ordered
    if method == "group_impute":
        group_by = params["group_by"]
        statistic = params.get("statistic", "median")
        agg_col = "__qdc_group_stat"
        is_string_col = df.schema[col] == pl.String
        if statistic == "mode":
            stats = df.group_by(group_by).agg(
                pl.col(col).drop_nulls().mode().first().alias(agg_col)
            )
        elif is_string_col:
            stats = (
                df.with_columns(pl.col(col).cast(pl.Float64, strict=False).alias("__qdc_num"))
                .group_by(group_by)
                .agg(getattr(pl.col("__qdc_num"), statistic)().alias(agg_col))
            )
        else:
            stats = df.group_by(group_by).agg(getattr(pl.col(col), statistic)().alias(agg_col))
        joined = df.join(stats, on=group_by, how="left")
        if is_string_col and statistic != "mode":
            joined = joined.with_columns(
                pl.when(pl.col(col).is_null())
                .then(pl.col(agg_col).cast(pl.String))
                .otherwise(pl.col(col))
                .alias(col)
            )
        else:
            joined = joined.with_columns(pl.col(col).fill_null(pl.col(agg_col)))
        return joined.drop(agg_col)
    if method == "random_sample":
        seed = params.get("seed", 0)
        observed = [v for v in df[col].to_list() if v is not None]
        if not observed:
            return df
        rng = random.Random(seed)
        vals = [rng.choice(observed) if v is None else v for v in df[col].to_list()]
        return df.with_columns(pl.Series(col, vals))
    raise ValueError(f"Unknown missingness method: {method}")


def _try_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _BOOL_TRUE:
        return True
    if s in _BOOL_FALSE:
        return False
    return None


def _op_convert_type(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """Confirmed type conversion (architecture section 4). Incompatible values
    become null; the assessment endpoint previews them first."""
    col = params["column"]
    target = params["target"]
    fmt = params.get("format")  # explicit strptime format when needed

    if target == "integer":
        expr = pl.col(col).cast(pl.String).str.strip_chars().str.replace_all(",", "").cast(pl.Int64, strict=False)
        if df.schema[col] != pl.String:
            expr = pl.col(col).cast(pl.Int64, strict=False)
        return df.with_columns(expr.alias(col))
    if target in ("decimal", "float"):
        expr = pl.col(col).cast(pl.String).str.strip_chars().str.replace_all(",", "").cast(pl.Float64, strict=False)
        if df.schema[col] != pl.String:
            expr = pl.col(col).cast(pl.Float64, strict=False)
        return df.with_columns(expr.alias(col))
    if target == "boolean":
        if df.schema[col] == pl.Boolean:
            return df
        mapped = [_try_bool(v) for v in df[col].to_list()]
        return df.with_columns(pl.Series(col, mapped, dtype=pl.Boolean))
    if target in ("string", "text"):
        return df.with_columns(pl.col(col).cast(pl.String).alias(col))
    if target == "categorical":
        return df.with_columns(pl.col(col).cast(pl.String).cast(pl.Categorical).alias(col))
    if target in ("date", "datetime", "time"):
        if df.schema[col] == pl.String:
            s = pl.col(col).str.strip_chars()
            if target == "date":
                expr = s.str.to_date(fmt, strict=False)
            elif target == "datetime":
                expr = s.str.to_datetime(fmt, strict=False)
            else:
                expr = s.str.to_time(fmt, strict=False)
        else:
            expr = pl.col(col).cast(
                {"date": pl.Date, "datetime": pl.Datetime, "time": pl.Time}[target],
                strict=False,
            )
        return df.with_columns(expr.alias(col))
    raise ValueError(f"Unknown target type: {target}")


def _apply_norm_op(df: pl.DataFrame, col: str, op: dict) -> pl.DataFrame:
    kind = op["op"]
    e = pl.col(col)
    if kind == "trim":
        return df.with_columns(e.str.strip_chars().alias(col))
    if kind == "collapse_spaces":
        return df.with_columns(e.str.replace_all(r" {2,}", " ").alias(col))
    if kind == "lower":
        return df.with_columns(e.str.to_lowercase().alias(col))
    if kind == "upper":
        return df.with_columns(e.str.to_uppercase().alias(col))
    if kind == "title":
        return df.with_columns(e.str.to_titlecase().alias(col))
    if kind == "empty_to_null":
        return df.with_columns(
            pl.when(e.str.strip_chars() == "").then(pl.lit(None).cast(pl.String)).otherwise(e).alias(col)
        )
    if kind == "strip_linebreaks":
        return df.with_columns(e.str.replace_all(r"[\r\n\t]+", " ").alias(col))
    if kind == "standardize_punctuation":
        return df.with_columns(
            e.str.replace_all("[’‘`´]", "'")
            .str.replace_all("[“”\"]", '"')
            .str.replace_all("[—–]", "-")
            .str.replace_all("…", "...")
            .alias(col)
        )
    if kind == "map_values":
        mapping = op["mapping"]
        vals = [mapping.get(v, v) for v in df[col].to_list()]
        return df.with_columns(pl.Series(col, vals))
    raise ValueError(f"Unknown normalization op: {kind}")


def _op_normalize(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    for col in params["columns"]:
        if df.schema.get(col) != pl.String:
            continue
        for op in params["operations"]:
            df = _apply_norm_op(df, col, op)
    return df


def _op_mask_values(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """Set values failing a validation rule to null (invalid-value treatment)."""
    col = params["column"]
    rule = params["rule"]
    kind = rule["type"]
    if kind == "range":
        lo, hi = rule.get("min"), rule.get("max")
        mask = pl.lit(False)
        num = pl.col(col).cast(pl.Float64, strict=False)
        if lo is not None:
            mask = mask | (num < lo)
        if hi is not None:
            mask = mask | (num > hi)
        mask = mask & num.is_not_null()
    elif kind == "not_in":
        mask = ~pl.col(col).cast(pl.String).is_in([str(v) for v in rule["values"]]) & pl.col(col).is_not_null()
    elif kind == "unparseable_date":
        parsed = pl.col(col).cast(pl.String).str.to_date(rule.get("format"), strict=False)
        mask = parsed.is_null() & pl.col(col).is_not_null()
    elif kind == "unparseable_numeric":
        parsed = pl.col(col).cast(pl.String).str.strip_chars().cast(pl.Float64, strict=False)
        mask = parsed.is_null() & pl.col(col).is_not_null()
    else:
        raise ValueError(f"Unknown rule type: {kind}")
    return df.with_columns(
        pl.when(mask).then(pl.lit(None).cast(df.schema[col])).otherwise(pl.col(col)).alias(col)
    )


def _op_standardize_format(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """Basic format standardization (architecture section 7.4)."""
    col = params["column"]
    kind = params["kind"]
    if kind == "boolean":
        # params.representation: true_false | yes_no | one_zero | bool
        rep = params.get("representation", "bool")
        mapped = [_try_bool(v) for v in df[col].to_list()]
        if rep == "bool":
            return df.with_columns(pl.Series(col, mapped, dtype=pl.Boolean))
        out = []
        for b in mapped:
            if b is None:
                out.append(None)
            elif rep == "yes_no":
                out.append("Yes" if b else "No")
            elif rep == "one_zero":
                out.append("1" if b else "0")
            else:
                out.append("True" if b else "False")
        return df.with_columns(pl.Series(col, out))
    if kind == "date_format":
        # to ISO yyyy-mm-dd strings
        s = pl.col(col)
        if df.schema[col] == pl.String:
            s = s.str.to_date(params.get("format"), strict=False)
        return df.with_columns(s.dt.strftime("%Y-%m-%d").alias(col))
    if kind == "decimal_separator":
        s = pl.col(col).cast(pl.String)
        if params.get("from_separator") == ",":
            s = s.str.replace(".", "").str.replace(",", ".")
        return df.with_columns(s.cast(pl.Float64, strict=False).alias(col))
    if kind == "currency":
        s = (
            pl.col(col).cast(pl.String)
            .str.replace_all(r"[^\d,.\-]", "")
            .str.replace_all(",", "")
            .cast(pl.Float64, strict=False)
        )
        return df.with_columns(s.alias(col))
    if kind == "percent":
        s = pl.col(col).cast(pl.String).str.replace_all("%", "").str.strip_chars().cast(pl.Float64, strict=False)
        if params.get("mode") == "proportion":
            s = s / 100.0
        return df.with_columns(s.alias(col))
    raise ValueError(f"Unknown format kind: {kind}")


def _op_treat_rows(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """Outlier treatments applied to explicitly reviewed rows (section 8.5).

    methods: missing | value | cap | mean | median | mode
    - missing: set cells to null
    - value:   set cells to params.value (manual correction / cap-to-value)
    - cap:     clip to [params.min, params.max]
    - mean/median/mode: fill those rows with the column statistic
    """
    col = params["column"]
    row_ids = set(params["row_ids"])
    method = params["method"]
    target = pl.col(ROW_ID_COLUMN).is_in(list(row_ids))

    if method == "cap":
        lo, hi = params.get("min"), params.get("max")
        e = pl.col(col).cast(pl.Float64, strict=False)
        if lo is not None:
            e = e.clip(lower_bound=lo)
        if hi is not None:
            e = e.clip(upper_bound=hi)
        return df.with_columns(
            pl.when(target).then(e.cast(df.schema[col], strict=False)).otherwise(pl.col(col)).alias(col)
        )
    if method == "missing":
        return df.with_columns(
            pl.when(target).then(pl.lit(None).cast(df.schema[col])).otherwise(pl.col(col)).alias(col)
        )
    if method in ("mean", "median", "mode"):
        s = df[col].drop_nulls()
        if df.schema[col] == pl.String:
            s = s.str.strip_chars().cast(pl.Float64, strict=False).drop_nulls()
        val = s.mode().sort().first() if method == "mode" else getattr(s, method)()
        return df.with_columns(
            pl.when(target).then(pl.lit(val)).otherwise(pl.col(col)).alias(col)
        )
    if method == "value":
        val = params["value"]
        try:
            val = pl.Series([val]).cast(df.schema[col], strict=False)[0]
        except Exception:
            pass
        return df.with_columns(
            pl.when(target).then(pl.lit(val)).otherwise(pl.col(col)).alias(col)
        )
    raise ValueError(f"Unknown treat_rows method: {method}")


_UNIT_SPLIT_RE = re.compile(r"^\s*([^\d\s.,+\-]*)\s*([+-]?[\d]+(?:[.,]\d+)?)\s*([^\d]*)\s*$")
_UNIT_NUM_RE = re.compile(r"^\s*[+-]?[\d]+(?:[.,]\d+)?\s*$")


def _op_convert_units(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """Convert unit-bearing values to normalized numbers.

    rules: [{"unit": "kg", "factor": 1.0}] for unit tokens ("" = plain
    numbers), or [{"condition": {"op": "<=", "value": 1}, "factor": 100}]
    scale rules on numeric columns. Only values matching a confirmed rule
    are converted; unmatched values are left unchanged. Dtype: Float64 when
    everything converted, String while unmatched text remains.
    """
    col = params["column"]
    rules = params["rules"]
    markers = {str(m).strip().lower() for m in _markers(params)}
    unit_rules = {
        str(r["unit"]).strip().lower(): float(r["factor"])
        for r in rules if r.get("unit") is not None
    }
    cond_rules = [r for r in rules if "condition" in r]

    if df.schema[col].is_numeric():
        # Scale rules only: deterministic condition + factor per rule.
        expr = pl.col(col)
        for r in cond_rules:
            c, f = r["condition"], float(r["factor"])
            op, v = c.get("op"), float(c.get("value", 0))
            cond = {
                "<=": pl.col(col) <= v, ">=": pl.col(col) >= v,
                "<": pl.col(col) < v, ">": pl.col(col) > v,
                "==": pl.col(col) == v, "all": pl.lit(True),
            }.get(op)
            if cond is None:
                raise ValueError(f"Unknown scale condition op: {op}")
            expr = pl.when(cond).then(pl.col(col).cast(pl.Float64) * f).otherwise(expr)
        return df.with_columns(expr.alias(col))

    if df.schema[col] != pl.String:
        raise ValueError(f"convert_units on {col}: expected String or numeric column")

    out: list = []
    all_numeric = True
    for v in df[col].to_list():
        if v is None or (isinstance(v, str) and v.strip().lower() in markers):
            out.append(None)
            continue
        s = str(v)
        if _UNIT_NUM_RE.match(s):
            f = unit_rules.get("")
            n = float(s.replace(",", "."))
            out.append(n * f if f is not None else n)
            continue
        m = _UNIT_SPLIT_RE.match(s)
        if m is None:
            out.append(s)
            all_numeric = False
            continue
        pre, num_s, suf = m.group(1), m.group(2), m.group(3)
        unit = (pre or suf or "").strip().lower()
        try:
            n = float(num_s.replace(",", "."))
        except ValueError:
            out.append(s)
            all_numeric = False
            continue
        f = unit_rules.get(unit)
        if unit and f is None:
            out.append(s)  # unmapped unit: leave unchanged
            all_numeric = False
            continue
        out.append(n * (f if f is not None else 1.0))

    if all_numeric:
        return df.with_columns(pl.Series(col, out, dtype=pl.Float64).alias(col))
    return df.with_columns(
        pl.Series(col, [o if isinstance(o, str) or o is None else repr(o) for o in out], dtype=pl.String).alias(col)
    )


def _op_clean_pattern(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """Pattern-stage treatments on a string column.

    actions:
      set_missing  - values not matching `pattern` -> null
      extract      - replace value with capture group `group` of `pattern`
      strip_affix  - remove literal `prefix` and/or `suffix`
      pad          - left-pad with zeros to `length` (string IDs only)
      replace      - regex `pattern` -> `replacement`
    """
    col = params["column"]
    action = params["action"]
    pattern = params.get("pattern")
    if df.schema[col] != pl.String:
        raise ValueError(f"clean_pattern applies to text columns — {col} is {df.schema[col]}")
    s = pl.col(col)

    if action == "set_missing":
        if not pattern:
            raise ValueError("set_missing requires a pattern")
        return df.with_columns(
            pl.when(s.str.contains(pattern)).then(s).otherwise(pl.lit(None).cast(pl.String)).alias(col)
        )
    if action == "extract":
        if not pattern:
            raise ValueError("extract requires a pattern")
        group = int(params.get("group", 1))
        n_groups = re.compile(pattern).groups
        if group < 1 or group > n_groups:
            raise ValueError(f"pattern has {n_groups} capture group(s); group {group} requested")
        return df.with_columns(
            pl.when(s.str.contains(pattern))
            .then(s.str.extract(pattern, group))
            .otherwise(s)
            .alias(col)
        )
    if action == "strip_affix":
        prefix, suffix = params.get("prefix"), params.get("suffix")
        if not prefix and not suffix:
            raise ValueError("strip_affix requires a prefix and/or suffix")
        expr = s
        if prefix:
            expr = expr.str.strip_prefix(prefix)
        if suffix:
            expr = expr.str.strip_suffix(suffix)
        return df.with_columns(expr.alias(col))
    if action == "pad":
        length = int(params["length"])
        return df.with_columns(s.str.zfill(length).alias(col))
    if action == "replace":
        if not pattern:
            raise ValueError("replace requires a pattern")
        return df.with_columns(s.str.replace_all(pattern, params.get("replacement", "")).alias(col))
    raise ValueError(f"Unknown clean_pattern action: {action}")


def _op_merge_records(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """Merge duplicate records: set chosen field values on the primary record,
    then drop the merged-away rows. Conflicting populated values are never
    resolved automatically - field_values must come from explicit user choice."""
    primary = params["primary_row_id"]
    drops = params.get("drop_row_ids", [])
    field_values = params.get("field_values", {})
    for col, val in field_values.items():
        df = df.with_columns(
            pl.when(pl.col(ROW_ID_COLUMN) == primary)
            .then(pl.lit(val).cast(df.schema[col]))
            .otherwise(pl.col(col))
            .alias(col)
        )
    if drops:
        df = df.filter(~pl.col(ROW_ID_COLUMN).is_in(drops))
    return df


OP_REGISTRY: dict[str, Callable[[pl.DataFrame, dict], pl.DataFrame]] = {
    "rename_column": _op_rename_column,
    "rename_columns": _op_rename_columns,
    "drop_rows": _op_drop_rows,
    "drop_column": _op_drop_column,
    "fill_null_constant": _op_fill_null_constant,
    "clean_special_chars": _op_clean_special_chars,
    "treat_missing": _op_treat_missing,
    "convert_type": _op_convert_type,
    "normalize": _op_normalize,
    "mask_values": _op_mask_values,
    "standardize_format": _op_standardize_format,
    "convert_units": _op_convert_units,
    "clean_pattern": _op_clean_pattern,
    "merge_records": _op_merge_records,
    "treat_rows": _op_treat_rows,
}


def execute_op(df: pl.DataFrame, spec: OperationSpec) -> pl.DataFrame:
    fn = OP_REGISTRY.get(spec.op_type)
    if fn is None:
        raise ValueError(f"Unknown operation type: {spec.op_type}")
    return fn(df, spec.params)
