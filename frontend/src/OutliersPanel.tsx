import { useEffect, useMemo, useState } from "react";
import {
  ApiError,
  bivariate,
  classifyOutlier,
  contextCandidates,
  declareOutcome,
  getOutcome,
  getPreview,
  markOutlier,
  outlierState,
  Preview,
  ProjectItem,
  screenOutliers,
  treatOutliers,
} from "./api";
import Plot from "./Plot";

const METHODS = [
  { v: "iqr", label: "IQR boundaries" },
  { v: "zscore", label: "Z-score" },
  { v: "modified_zscore", label: "Modified Z-score (median/MAD)" },
  { v: "percentile", label: "Percentile boundaries" },
  { v: "manual", label: "Manual min/max" },
];

const METHOD_HINTS: Record<string, string> = {
  iqr: "Robust default, flags values far outside the middle 50% of the data.",
  zscore: "Flags values more than N standard deviations from the mean, best for bell-shaped data.",
  modified_zscore: "Like Z-score but uses median/MAD, better when the data is skewed.",
  percentile: "Flags everything below/above fixed percentiles (e.g. bottom and top 1%).",
  manual: "You type the exact lower and upper bounds yourself.",
};

const CLASSIFICATIONS = [
  ["valid_extreme", "Valid extreme"],
  ["likely_error", "Likely error"],
  ["needs_review", "Needs review"],
  ["excluded", "Exclude from relationship"],
];

export default function OutliersPanel({ item, onChanged, refresh = 0, stage = "univariate" }: { item: ProjectItem; onChanged: () => void; refresh?: number; stage?: string }) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [outcome, setOutcome] = useState<any>(undefined);
  const [col, setCol] = useState("");
  const [method, setMethod] = useState("iqr");
  const [params, setParams] = useState<Record<string, string>>({});
  const [screen, setScreen] = useState<any>(null);
  const [candidates, setCandidates] = useState<any[]>([]);
  const [context, setContext] = useState("");
  const [biv, setBiv] = useState<any>(null);
  const [state, setState] = useState<any>(null);
  const [treat, setTreat] = useState<Record<string, string>>({ action: "keep", method: "median" });
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    getPreview(item.item_id).then(setPreview);
    getOutcome(item.item_id).then((r) => setOutcome(r.outcome));
    outlierState(item.item_id).then(setState).catch(() => null);
  }, [item.item_id, refresh]);

  const numericCols = useMemo(
    () => preview?.columns.filter((c) => /int|float|decimal|double/i.test(c.dtype)).map((c) => c.name) ?? [],
    [preview],
  );

  const colState = state?.columns?.[col] ?? {};
  const reviewed = !!colState.bivariate_reviewed || Object.keys(colState.classifications || {}).length > 0;

  const runScreen = async () => {
    setErr(null);
    setScreen(null);
    setBiv(null);
    try {
      const p: Record<string, number> = {};
      for (const [k, v] of Object.entries(params)) if (v !== "") p[k] = Number(v);
      const res = await screenOutliers(item.item_id, col, method, p);
      setScreen(res);
      setState(await outlierState(item.item_id));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  };

  const runBivariate = async (ctx: string) => {
    setContext(ctx);
    setErr(null);
    try {
      setBiv(await bivariate(item.item_id, col, ctx));
      setState(await outlierState(item.item_id));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  };

  const classify = async (rowId: number, classification: string) => {
    setState((await classifyOutlier(item.item_id, col, rowId, classification)).state);
  };

  const applyTreat = async (rowIds: number[]) => {
    setErr(null);
    try {
      const body: Record<string, unknown> = { column: col, row_ids: rowIds, action: treat.action };
      if (treat.action === "cap") { body.min = Number(treat.min); body.max = Number(treat.max); }
      if (treat.action === "impute") body.impute_method = treat.method;
      if (treat.action === "correct") body.value = treat.value;
      const r = await treatOutliers(item.item_id, body);
      setMsg(`Applied ${treat.action}; mean ${r.distribution?.before?.mean?.toFixed(2)} → ${r.distribution?.after?.mean?.toFixed(2)}`);
      setState(await outlierState(item.item_id));
      onChanged();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  };

  const markAction = async (action: string) => {
    setState(await markOutlier(item.item_id, col, action));
  };

  // Columns sent to contextual review from the screening stage
  const markedCols = Object.entries(state?.columns ?? {})
    .filter(([, s]: any) => s.univariate_decision === "mark" && (s.flagged_ids ?? []).length > 0)
    .map(([c]) => c);

  const pickReviewCol = async (c: string) => {
    setCol(c);
    setBiv(null);
    setContext("");
    setCandidates([]);
    if (!c) return;
    try {
      const cand = await contextCandidates(item.item_id, c);
      setCandidates(cand.candidates);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  };

  const bounds = colState.bounds ?? screen?.bounds;

  return (
    <div>
      {stage === "bivariate" && (
        <OutcomeBar item={item} outcome={outcome} setOutcome={setOutcome} columns={preview?.columns.map((c) => c.name) ?? []} />
      )}

      {stage !== "bivariate" && (
        <>
          <div className="row" style={{ alignItems: "flex-end", marginBottom: 4 }}>
            <div>
              <label>Numeric column</label>
              <select value={col} onChange={(e) => { setCol(e.target.value); setScreen(null); setBiv(null); }}>
                <option value="">choose…</option>
                {numericCols.map((c) => <option key={c}>{c}</option>)}
              </select>
            </div>
            <div>
              <label>Method</label>
              <select value={method} onChange={(e) => setMethod(e.target.value)}>
                {METHODS.map((m) => <option key={m.v} value={m.v}>{m.label}</option>)}
              </select>
            </div>
            {method === "iqr" && <Param label="multiplier" def="1.5" params={params} setParams={setParams} />}
            {method === "zscore" && <Param label="threshold" def="3.0" params={params} setParams={setParams} />}
            {method === "modified_zscore" && <Param label="threshold" def="3.5" params={params} setParams={setParams} />}
            {method === "percentile" && (
              <>
                <Param label="lo (%)" def="1" params={params} setParams={setParams} k="lo" />
                <Param label="hi (%)" def="99" params={params} setParams={setParams} k="hi" />
              </>
            )}
            {method === "manual" && (
              <>
                <Param label="min" params={params} setParams={setParams} k="min" />
                <Param label="max" params={params} setParams={setParams} k="max" />
              </>
            )}
            <div><button className="primary" onClick={runScreen} disabled={!col}>Screen</button></div>
          </div>
          <p className="muted" style={{ fontSize: "0.82rem", marginTop: 0 }}>{METHOD_HINTS[method]}</p>
          {err && <div className="error">{err}</div>}
          {msg && <div className="notice">{msg}</div>}

          {screen && (
            <div>
              <Histogram screen={screen} />
              <p className="muted">
                {screen.outlier_count} flagged ({screen.outlier_pct}%) · bounds [{screen.bounds.lower.toFixed(2)}, {screen.bounds.upper.toFixed(2)}] ·
                mean {screen.stats.mean.toFixed(2)}, median {screen.stats.median.toFixed(2)}, std {screen.stats.std.toFixed(2)}, skew {screen.stats.skewness.toFixed(2)}
              </p>
              {screen.flagged.length > 0 && (
                <p className="muted" style={{ fontSize: "0.82rem" }}>
                  Flagged values:{" "}
                  {screen.flagged.slice(0, 20).map((f: any) => `#${f.row_id} = ${f.value}`).join("  ·  ")}
                  {screen.flagged.length > 20 && ` … +${screen.flagged.length - 20} more`}
                </p>
              )}
              <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
                <button className="primary" title="Send these flagged values to the Outliers, context stage for review" onClick={() => markAction("mark")}>Mark for context review</button>
                <button title="Accept all flagged values as-is; no review needed" onClick={() => markAction("keep_all")}>Keep all flagged</button>
                <button title="This column has no meaningful outliers, skip it" onClick={() => markAction("exclude")}>Exclude column</button>
              </div>
              {markedCols.length > 0 && (
                <p className="muted" style={{ fontSize: "0.82rem" }}>
                  Marked for review: {markedCols.join(", ")}, continue in the "Outliers, context" stage.
                </p>
              )}
            </div>
          )}
        </>
      )}

      {stage === "bivariate" && (
        <>
          {markedCols.length === 0 && (
            <p className="muted">
              No columns flagged for review yet, run screening in "Outliers, screening" and choose
              <strong> Mark for context review</strong> on a column first.
            </p>
          )}
          {markedCols.length > 0 && (
            <>
              <div style={{ marginBottom: 8 }}>
                <label>Flagged column under review</label>
                <select value={col} onChange={(e) => pickReviewCol(e.target.value)}>
                  <option value="">choose…</option>
                  {markedCols.map((c) => (
                    <option key={c} value={c}>
                      {c}, {(state.columns[c].flagged_ids ?? []).length} flagged
                    </option>
                  ))}
                </select>
              </div>

              {col && candidates.length > 0 && (
                <div style={{ marginBottom: 8 }}>
                  <label>Compare against</label>
                  <p className="muted" style={{ fontSize: "0.82rem", margin: "0 0 4px" }}>
                    Pick a variable that might explain the extreme values, each suggestion states why it may help.
                  </p>
                  <select value={context} onChange={(e) => runBivariate(e.target.value)}>
                    <option value="">choose…</option>
                    {candidates.map((c) => (
                      <option key={c.column} value={c.column}>
                        {c.column} ({c.kind}{c.is_outcome ? ", outcome" : ""}), {c.reason}; {c.usable_pairs} usable pairs
                      </option>
                    ))}
                  </select>
                </div>
              )}

              {biv && <BivariateView biv={biv} col={col} onClassify={classify} state={colState} />}

              {col && reviewed && (
                <div style={{ marginTop: 12 }}>
                  <h2 style={{ fontSize: "0.95rem" }}>Treat flagged records</h2>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <select value={treat.action} onChange={(e) => setTreat({ ...treat, action: e.target.value })}>
                      <option value="keep">keep</option>
                      <option value="remove">remove row</option>
                      <option value="set_missing">set missing</option>
                      <option value="cap">cap at boundary</option>
                      <option value="impute">impute</option>
                      <option value="correct">correct value</option>
                    </select>
                    {treat.action === "cap" && (
                      <>
                        <input type="text" placeholder="min" style={{ width: 70 }}
                          value={treat.min ?? bounds?.lower?.toFixed(2) ?? ""}
                          onChange={(e) => setTreat({ ...treat, min: e.target.value })} />
                        <input type="text" placeholder="max" style={{ width: 70 }}
                          value={treat.max ?? bounds?.upper?.toFixed(2) ?? ""}
                          onChange={(e) => setTreat({ ...treat, max: e.target.value })} />
                      </>
                    )}
                    {treat.action === "impute" && (
                      <select value={treat.method} onChange={(e) => setTreat({ ...treat, method: e.target.value })}>
                        {["mean", "median", "mode"].map((m) => <option key={m}>{m}</option>)}
                      </select>
                    )}
                    {treat.action === "correct" && (
                      <input type="text" placeholder="corrected value" onChange={(e) => setTreat({ ...treat, value: e.target.value })} />
                    )}
                    <button className="primary"
                      onClick={() => applyTreat(colState.flagged_ids || screen?.flagged.map((f: any) => f.row_id) || [])}>
                      Apply to all flagged
                    </button>
                  </div>
                </div>
              )}
              {col && !reviewed && (
                <p className="muted" style={{ fontSize: "0.82rem" }}>
                  Classify the flagged records above (or run a comparison) to unlock treatment options.
                </p>
              )}
            </>
          )}
          {err && <div className="error">{err}</div>}
          {msg && <div className="notice">{msg}</div>}
        </>
      )}
    </div>
  );
}

function Param({ label, def, params, setParams, k }: any) {
  const key = k || label.split(" ")[0];
  return (
    <div>
      <label>{label}</label>
      <input type="text" style={{ width: 70 }} defaultValue={def}
        onChange={(e) => setParams({ ...params, [key]: e.target.value })} />
    </div>
  );
}

function OutcomeBar({ item, outcome, setOutcome, columns }: any) {
  const [col, setCol] = useState("");
  const [meaning, setMeaning] = useState("");
  const [pos, setPos] = useState("");
  return (
    <div className="notice" style={{ marginBottom: 12 }}>
      <strong>Outcome variable</strong>:{" "}
      {outcome ? `${outcome.column} (${outcome.meaning || "declared"})` : "none declared"}
      <span style={{ marginLeft: 12 }}>
        <select value={col} onChange={(e) => setCol(e.target.value)}>
          <option value="">declare outcome…</option>
          <option value="__none__">No outcome variable</option>
          {columns.map((c: string) => <option key={c}>{c}</option>)}
        </select>
        {col && col !== "__none__" && (
          <>
            <input type="text" placeholder="meaning (e.g. churn)" style={{ marginLeft: 6, width: 140 }}
              onChange={(e) => setMeaning(e.target.value)} />
            <input type="text" placeholder="positive class" style={{ marginLeft: 6, width: 110 }}
              onChange={(e) => setPos(e.target.value)} />
          </>
        )}
        {col && (
          <button style={{ marginLeft: 6 }} onClick={async () => {
            const r = await declareOutcome(item.item_id,
              col === "__none__" ? { column: null } : { column: col, meaning, positive_class: pos });
            setOutcome(r.outcome);
          }}>Set</button>
        )}
      </span>
    </div>
  );
}

function Histogram({ screen }: { screen: any }) {
  const edges = screen.histogram.map((b: any) => b.edge);
  const counts = screen.histogram.map((b: any) => b.count);
  const min = screen.stats.min;
  const width = edges.length > 1 ? edges[1] - edges[0] : 1;
  const maxCount = Math.max(...counts, 1);
  const lo = screen.bounds.lower;
  const hi = screen.bounds.upper;
  const xmax = Math.max(screen.stats.max, hi);
  return (
    <Plot
      data={[
        { type: "bar", x: edges.map((e: number) => e - width / 2), y: counts, marker: { color: "#9db9e8" }, name: "values" },
        {
          // Flagged values drawn as large markers partway up the chart so they
          // are impossible to miss, plus a red rug strip along the axis.
          type: "scatter", mode: "markers",
          x: screen.flagged.map((f: any) => f.value), y: screen.flagged.map(() => maxCount * 0.55),
          marker: { color: "#d64545", size: 11, symbol: "diamond", line: { color: "#8f1f1f", width: 1 } },
          name: `flagged (${screen.flagged.length})`, showlegend: true,
        },
      ]}
      layout={{
        shapes: [
          // shaded outlier regions beyond the bounds
          { type: "rect", x0: Math.min(min, lo), x1: lo, y0: 0, y1: 1, yref: "paper", fillcolor: "rgba(214,69,69,0.08)", line: { width: 0 } },
          { type: "rect", x0: hi, x1: xmax, y0: 0, y1: 1, yref: "paper", fillcolor: "rgba(214,69,69,0.08)", line: { width: 0 } },
          { type: "line", x0: lo, x1: lo, y0: 0, y1: 1, yref: "paper", line: { color: "#d64545", dash: "dash" } },
          { type: "line", x0: hi, x1: hi, y0: 0, y1: 1, yref: "paper", line: { color: "#d64545", dash: "dash" } },
          { type: "line", x0: screen.stats.mean, x1: screen.stats.mean, y0: 0, y1: 1, yref: "paper", line: { color: "#1b74e4" } },
          { type: "line", x0: screen.stats.median, x1: screen.stats.median, y0: 0, y1: 1, yref: "paper", line: { color: "#2e8b57" } },
        ],
        annotations: [
          { x: lo, y: 1.02, yref: "paper", text: "lower bound", showarrow: false, font: { color: "#d64545", size: 11 }, xanchor: "right" },
          { x: hi, y: 1.02, yref: "paper", text: "upper bound", showarrow: false, font: { color: "#d64545", size: 11 }, xanchor: "left" },
          { x: screen.stats.mean, y: 1.02, yref: "paper", text: "mean", showarrow: false, font: { color: "#1b74e4", size: 11 }, xanchor: "left" },
          { x: screen.stats.median, y: 1.02, yref: "paper", text: "median", showarrow: false, font: { color: "#2e8b57", size: 11 }, xanchor: "right" },
        ],
        legend: { orientation: "h", y: -0.15 },
        title: screen.column, xaxis: { range: [Math.min(min, lo), xmax] },
        margin: { t: 50 },
      }}
    />
  );
}

function BivariateView({ biv, col, onClassify, state }: any) {
  const flagged = new Set(biv.flagged_ids);
  const cls: Record<string, any> = state?.classifications || {};
  return (
    <div style={{ marginTop: 12 }}>
      {biv.insufficient_evidence && (
        <div className="notice">Insufficient evidence: fewer than 10 usable paired observations.</div>
      )}
      {biv.kind === "numeric" && (
        <Plot
          data={[
            {
              type: "scatter", mode: "markers",
              x: biv.points.map((p: any) => p[biv.context]), y: biv.points.map((p: any) => p[col]),
              marker: {
                color: biv.points.map((p: any) => flagged.has(p.__qdc_row_id) ? "#d64545" : "#9db9e8"),
                size: biv.points.map((p: any) => flagged.has(p.__qdc_row_id) ? 9 : 5),
              },
              name: "records",
            },
            biv.trend && {
              type: "scatter", mode: "lines",
              x: biv.points.map((p: any) => p[biv.context]).sort((a: number, b: number) => a - b),
              y: biv.points.map((p: any) => p[biv.context]).sort((a: number, b: number) => a - b).map((x: number) => biv.trend.slope * x + biv.trend.intercept),
              line: { color: "#1b74e4" }, name: "trend",
            },
          ]}
          layout={{ title: `${col} vs ${biv.context}${biv.sampled ? " (sampled view)" : ""} · r=${biv.correlation?.toFixed(2)}` }}
        />
      )}
      {(biv.kind === "categorical" || biv.kind === "boolean") && (
        <Plot
          data={Object.entries(biv.group_distributions || {}).map(([g, vals]: any) => ({
            type: "box", y: vals, name: g, boxpoints: "all", marker: { color: "#9db9e8" },
          }))}
          layout={{ title: `${col} by ${biv.context}` }}
        />
      )}
      {biv.kind === "datetime" && (
        <Plot
          data={[
            { type: "scatter", mode: "markers", x: biv.points.map((p: any) => p.x), y: biv.points.map((p: any) => p.y),
              marker: { color: biv.points.map((p: any) => flagged.has(p.row_id) ? "#d64545" : "#9db9e8") }, name: "values" },
            { type: "scatter", mode: "lines", x: biv.points.map((p: any) => p.x), y: biv.points.map((p: any) => p.roll_median), line: { color: "#1b74e4" }, name: "rolling median" },
            { type: "scatter", mode: "lines", x: biv.points.map((p: any) => p.x), y: biv.points.map((p: any) => p.roll_hi), line: { color: "#ccc", dash: "dot" }, name: "rolling IQR" },
            { type: "scatter", mode: "lines", x: biv.points.map((p: any) => p.x), y: biv.points.map((p: any) => p.roll_lo), line: { color: "#ccc", dash: "dot" }, showlegend: false },
          ]}
          layout={{ title: `${col} over ${biv.context}${biv.sampled ? " (sampled view)" : ""}` }}
        />
      )}

      <table style={{ marginTop: 8 }}>
        <thead><tr><th>Row</th><th>Value</th><th>Evidence</th><th>Classification</th></tr></thead>
        <tbody>
          {biv.flagged_ids.map((rid: number) => (
            <tr key={rid}>
              <td>{rid}</td>
              <td>{String(biv.points?.find?.((p: any) => (p.__qdc_row_id ?? p.row_id) === rid)?.[col] ?? "")}</td>
              <td>{(biv.evidence[String(rid)] || "").replace(/_/g, " ")}</td>
              <td>
                {CLASSIFICATIONS.map(([v, label]) => (
                  <button key={v}
                    style={{ padding: "1px 6px", fontSize: "0.75rem", marginRight: 4, background: cls[String(rid)]?.classification === v ? "#1b74e4" : undefined, color: cls[String(rid)]?.classification === v ? "#fff" : undefined }}
                    onClick={() => onClassify(rid, v)}>{label}</button>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
