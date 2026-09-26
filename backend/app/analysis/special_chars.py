"""Special-character scan (pre-missingness stage).

Reports, per column, which special characters appear in its values — plus
column-name issues. Nothing mutates here; treatments are applied via the
`clean_special_chars` op and confirmed per column.
"""

import re

import polars as pl

from ..config import ROW_ID_COLUMN
from .basic import propose_column_name

# Characters treated as "special" in cell values: anything that isn't
# alphanumeric, whitespace, or ordinary punctuation (- . , ' / :).
SPECIAL_CHAR_RE = re.compile(r"[^A-Za-z0-9\s\-.,'/:%]")

# Column names follow the stricter rule used by the names assessment:
# clean names are [a-z0-9_].
NAME_SPECIAL_RE = re.compile(r"[^a-z0-9_]")


def assess_special_chars(df: pl.DataFrame, sample_limit: int = 3) -> dict:
    """Report special characters found in column names and string values.

    Returns:
      column_names: [{column, chars, proposed}] — only flagged names
      columns: [{column, chars: [{char, count, samples}]}] — only columns
               with at least one detected special character
    """
    name_findings = []
    for c in df.columns:
        if c == ROW_ID_COLUMN:
            continue
        lowered = c.lower().replace(" ", "_")
        chars = sorted(set(NAME_SPECIAL_RE.findall(lowered)) | ({" "} if " " in c else set()))
        if chars or c != c.lower() or c != c.strip():
            problems = []
            if " " in chars:
                problems.append("spaces")
            if [ch for ch in chars if ch != " "]:
                problems.append("special_characters")
            if c != c.strip():
                problems.append("surrounding_whitespace")
            if c != c.lower():
                problems.append("case")
            name_findings.append(
                {"column": c, "chars": chars, "problems": problems, "proposed": propose_column_name(c)}
            )

    col_findings = []
    for c in df.columns:
        if c == ROW_ID_COLUMN or df.schema[c] != pl.String:
            continue
        s = df[c].drop_nulls()
        if s.len() == 0:
            continue
        has_special = s.str.contains(SPECIAL_CHAR_RE.pattern)
        if int(has_special.sum()) == 0:
            continue
        # Vectorized: extract every special char, explode, count.
        vc = s.str.extract_all(SPECIAL_CHAR_RE.pattern).explode().drop_nulls().value_counts()
        sample_pool = s.filter(has_special).head(60).to_list()
        matches = []
        for ch, count in vc.iter_rows():
            samples = [v for v in sample_pool if ch in v][:sample_limit]
            matches.append({"char": ch, "count": int(count), "samples": samples})
        col_findings.append(
            {"column": c, "dtype": "String", "chars": sorted(matches, key=lambda x: -x["count"])}
        )

    return {"column_names": name_findings, "columns": col_findings}
