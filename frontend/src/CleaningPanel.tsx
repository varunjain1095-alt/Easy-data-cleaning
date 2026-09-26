import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { CheckIcon } from "lucide-react";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table";
import {
  Table as TTable, TableBody as TBody, TableCell as TCell, TableHead as THead,
  TableHeaderCell as THeadCell, TableRoot as TRoot, TableRow as TRow,
} from "@/components/TremorTable";

import {
  ApiError,
  applyOp,
  assess,
  assessInvalid,
  assessKeys,
  assessPattern,
  assessUnits,
  declareKey,
  dupAnalyze,
  dupEstimate,
  dupResolve,
  dupResult,
  getHistory,
  getKeyDeclaration,
  getMissingRows,
  getPreview,
  OpRequest,
  patternPresets,
  pollJob,
  Preview,
  previewOp,
  ProjectItem,
  redo,
  setStage as apiSetStage,
  undo,
} from "./api";

const STAGES: { key: string; label: string }[] = [
  { key: "special_chars", label: "Special characters" },
  { key: "missingness", label: "Missing values" },
  { key: "units", label: "Units & scales" },
  { key: "types", label: "Data types" },
  { key: "normalization", label: "Normalization" },
  { key: "column_names", label: "Column names & formats" },
  { key: "invalid_values", label: "Invalid values & structures" },
  { key: "patterns", label: "Patterns" },
  { key: "duplicates", label: "Duplicates" },
  { key: "keys", label: "Keys & uniqueness" },
];

export default function CleaningPanel({
  item,
  onChanged,
  refresh = 0,
}: {
  item: ProjectItem;
  onChanged: () => void;
  refresh?: number;
}) {
  const [stage, setStage] = useState<string>("missingness");
  const [states, setStates] = useState(item.stage_states);
  const [msg, setMsg] = useState<string | null>(null);
  const [hist, setHist] = useState<{ can_undo: boolean; can_redo: boolean }>({ can_undo: false, can_redo: false });

  const refreshHistory = useCallback(
    () => getHistory(item.item_id).then((h) => setHist(h)).catch(() => null),
    [item.item_id],
  );
  useEffect(() => { refreshHistory(); }, [refreshHistory]);

  // Any applied change updates both project data and undo/redo availability.
  const changed = () => { refreshHistory(); onChanged(); };

  const mark = async (s: string, state: string) => {
    const r = await apiSetStage(item.item_id, s, state);
    setStates(r.stage_states);
    onChanged();
  };

  return (
    <div className="ws-card ws-workbench">
      <h2>Clean: {item.name}</h2>
      {msg && <div className="notice">{msg}</div>}
      <div className="stage-tabs" role="tablist" aria-label="Cleaning stages">
        {STAGES.map((s) => (
          <button
            key={s.key}
            role="tab"
            aria-selected={stage === s.key}
            className={`stage-tab${stage === s.key ? " active" : ""}`}
            onClick={() => setStage(s.key)}
            title={states[s.key]}
          >
            {s.label}
            {states[s.key] && states[s.key] !== "not_started" && (
              <span className={`badge ${states[s.key]}`}>
                {states[s.key].replace(/_/g, " ")}
              </span>
            )}
          </button>
        ))}
      </div>
      <div className="ws-toolbar">
        <button className="ws-btn ws-btn-outline" disabled={!hist.can_undo}
          title={hist.can_undo ? "Undo last change" : "Nothing to undo"}
          onClick={() => undo(item.item_id)
            .then(() => { setMsg("Undone"); refreshHistory(); onChanged(); })
            .catch((e) => setMsg(e instanceof ApiError ? e.message : String(e)))}>
          Undo
        </button>
        <button className="ws-btn ws-btn-outline" disabled={!hist.can_redo}
          title={hist.can_redo ? "Redo last undone change" : "Nothing to redo"}
          onClick={() => redo(item.item_id)
            .then(() => { setMsg("Redone"); refreshHistory(); onChanged(); })
            .catch((e) => setMsg(e instanceof ApiError ? e.message : String(e)))}>
          Redo
        </button>
        <button className="ws-btn ws-btn-outline" onClick={() => mark(stage, "skipped")}>Skip stage</button>
        <button className="ws-btn ws-btn-primary" onClick={() => mark(stage, "completed")}>
          <CheckIcon aria-hidden="true" /> Mark stage completed
        </button>
      </div>
      <p className="stage-intro ws-info">{STAGE_INTROS[stage]}</p>
      {stage === "special_chars" && <SpecialChars item={item} onChanged={changed} refresh={refresh} />}
      {stage === "missingness" && <Missingness item={item} onChanged={changed} refresh={refresh} hist={hist} />}
      {stage === "types" && <Types item={item} onChanged={changed} refresh={refresh} />}
      {stage === "normalization" && <Normalize item={item} onChanged={changed} refresh={refresh} />}
      {stage === "column_names" && <Basic item={item} onChanged={changed} section="names" refresh={refresh} />}
      {stage === "invalid_values" && <Basic item={item} onChanged={changed} section="invalid" refresh={refresh} />}
      {stage === "units" && <UnitsPanel item={item} onChanged={changed} refresh={refresh} />}
      {stage === "patterns" && <PatternsPanel item={item} onChanged={changed} refresh={refresh} />}
      {stage === "duplicates" && <Duplicates item={item} onChanged={changed} refresh={refresh} />}
      {stage === "keys" && <KeysPanel item={item} onChanged={changed} refresh={refresh} />}

    </div>
  );
}

function useAssessment(item: ProjectItem, name: string, deps: unknown[] = []) {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    assess(item.item_id, name).then(setData).catch((e) => setError(e.message));
  }, [item.item_id, name]);
  useEffect(reload, [reload, ...deps]);
  return { data, error, reload };
}

function ApplyBar({
  item, op, label, onDone,
}: { item: ProjectItem; op: OpRequest | null; label?: string; onDone: (r: any) => void }) {
  const [preview, setPreview] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  if (!op) return null;
  return (
    <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
      <button className="ws-btn ws-btn-outline ws-btn-sm"
        title={preview ? "Hide preview" : "Preview affected rows before applying"}
        onClick={() => {
          if (preview) { setPreview(null); return; }
          previewOp(item.item_id, op).then(setPreview).catch((e) => setErr(e.message));
        }}>
        Preview
      </button>
      <button className="ws-btn ws-btn-primary ws-btn-sm"
        onClick={() => applyOp(item.item_id, op).then((r) => { setPreview(null); onDone(r); }).catch((e) => setErr(e.message))}
      >
        {label || "Apply"}
      </button>
      {preview && (
        <span className="muted">
          affects {preview.affected_rows} cells · {preview.row_count_before}→{preview.row_count_after} rows
        </span>
      )}
      <Dialog open={!!preview} onOpenChange={(o) => { if (!o) setPreview(null); }}>
        <DialogContent className="qdc-dialog sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Preview: before / after</DialogTitle>
            <DialogDescription>
              affects {preview?.affected_rows} cells · {preview?.row_count_before}→{preview?.row_count_after} rows
            </DialogDescription>
          </DialogHeader>
          {preview && <PreviewCard preview={preview} />}
        </DialogContent>
      </Dialog>
      {err && <span className="error">{err}<button className="dismiss" title="Dismiss" onClick={() => setErr(null)}>×</button></span>}
    </span>
  );
}

function PreviewCard({ preview }: { preview: any }) {
  const before: any[] = preview.before_sample ?? [];
  const after: any[] = preview.after_sample ?? [];
  const beforeById = new Map(before.map((r) => [r.__qdc_row_id, r]));
  const afterById = new Map(after.map((r) => [r.__qdc_row_id, r]));
  const rowIds = [...new Set([...beforeById.keys(), ...afterById.keys()])];
  const removedCols = (preview.columns_before ?? []).filter((c: string) => !(preview.columns_after ?? []).includes(c));
  const addedCols = (preview.columns_after ?? []).filter((c: string) => !(preview.columns_before ?? []).includes(c));

  const diffs: { rid: unknown; col: string; from: unknown; to: unknown }[] = [];
  for (const rid of rowIds) {
    const b = beforeById.get(rid);
    const a = afterById.get(rid);
    if (b && a) {
      for (const k of Object.keys(b)) {
        if (k === "__qdc_row_id" || !(k in a)) continue;
        if (String(b[k]) !== String(a[k])) diffs.push({ rid, col: k, from: b[k], to: a[k] });
      }
    } else if (b) {
      diffs.push({ rid, col: "(entire row)", from: "present", to: "removed" });
    } else if (a) {
      diffs.push({ rid, col: "(entire row)", from: "empty", to: "added" });
    }
  }
  const showVal = (v: unknown) =>
    v === null || v === undefined || v === "" ? <em className="missing-val">missing</em> : String(v);

  return (
    <div style={{ maxHeight: "60vh", overflow: "auto" }}>
        {removedCols.length > 0 && <div className="muted" style={{ marginBottom: 6 }}>columns removed: {removedCols.join(", ")}</div>}
        {addedCols.length > 0 && <div className="muted" style={{ marginBottom: 6 }}>columns added: {addedCols.join(", ")}</div>}
        {diffs.length === 0 && removedCols.length === 0 && addedCols.length === 0 && (
          <p className="muted">No cell changes detected in the sampled rows.</p>
        )}
        {diffs.length > 0 && (
          <Table>
            <TableHeader><TableRow><TableHead>row</TableHead><TableHead>column</TableHead><TableHead>before</TableHead><TableHead>after</TableHead></TableRow></TableHeader>
            <TableBody>
              {diffs.map((d, i) => (
                <TableRow key={i}>
                  <TableCell className="muted">#{String(d.rid)}</TableCell>
                  <TableCell>{d.col}</TableCell>
                  <TableCell className="mono">{showVal(d.from)}</TableCell>
                  <TableCell className="mono">{showVal(d.to)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        {rowIds.length > 0 && (
          <p className="muted" style={{ marginTop: 8, marginBottom: 0 }}>
            showing up to {rowIds.length} changed rows
          </p>
        )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Special characters (pre-missingness scan: column names + cell values)
// ---------------------------------------------------------------------------

const CHAR_ACTIONS = ["keep", "strip", "replace", "blank"] as const;

/** Render a cell value with every occurrence of the given chars highlighted. */
function Highlighted({ text, chars }: { text: string; chars: string[] }) {
  const set = new Set(chars);
  return (
    <>
      {[...text].map((c, i) =>
        set.has(c) ? <mark key={i} title={c === " " ? "space" : `character ${c}`}>{c === " " ? "·" : c}</mark> : <span key={i}>{c}</span>
      )}
    </>
  );
}

const STAGE_INTROS: Record<string, string> = {
  special_chars: "Finds symbols and odd punctuation in column names and cell contents (e.g. @, #, or spaces). For each finding you choose: keep it, strip it, replace it, or blank the cell. Nothing changes until you click Apply.",
  missingness: "Shows how many cells in each column are empty or marked as missing (e.g. NA, null, -200). 'show rows' lists the actual rows. Pick a treatment per column, fill with the average, copy a neighbouring value, drop them, or leave them as they are.",
  types: "Suggests what each column really is (number, date, time, text) based on its contents, and converts it when you click Convert. 'Compatible' is how many values fit; 'Incompatible' shows the values that would become empty.",
  normalization: "Tidies up text formatting: stray spaces, double spaces, line breaks, inconsistent capitalisation. Tick what you want applied per column.",
  column_names: "Flags column names that are awkward to work with (spaces, capitals, symbols) and proposes clean versions. Tick the ones you want renamed, or type your own name.",
  invalid_values: "Lets you blank out values that break rules you set, e.g. negative ages, or a sentinel like -200.",
  duplicates: "Finds rows that are identical (100%) or nearly identical (90/80%). You review each group and decide which record to keep.",
  units: "Finds values carrying units (5kg, €100, 10 lb) and mixed scales (0.25 vs 25). You set the conversion factor per unit; only matched values are converted, the rest are left alone and reported.",
  patterns: "Checks that text values follow an expected shape: emails, phones, IDs. Pick a preset or write a pattern; non-matching values are listed, then you choose what to do with them.",
  keys: "Declares which column(s) must be unique, e.g. customer_id, or order_id + product_id. Reports missing keys, repeated keys with identical rows, and repeated keys with conflicting details.",

};

const PROBLEM_LABELS: Record<string, string> = {
  special_characters: "special characters",
  spaces: "spaces",
  case: "uppercase letters",
  surrounding_whitespace: "leading/trailing spaces",
};

function SpecialChars({ item, onChanged, refresh = 0 }: { item: ProjectItem; onChanged: () => void; refresh?: number }) {
  const { data, error, reload } = useAssessment(item, "special-chars", [refresh]);
  const [sel, setSel] = useState<Record<string, Set<string>>>({});   // column -> selected chars
  const [act, setAct] = useState<Record<string, string>>({});        // column -> action
  const [repl, setRepl] = useState<Record<string, string>>({});      // column -> replacement
  const [nameSel, setNameSel] = useState<Record<string, boolean>>({}); // column name -> clean it
  const [customNames, setCustomNames] = useState<Record<string, string>>({}); // column -> custom rename
  const [open, setOpen] = useState<[string, string] | null>(null);   // [column, char] expanded preview
  if (error) return <div className="error">{error}</div>;
  if (!data) return <p>Scanning…</p>;

  const toggleChar = (col: string, ch: string) => {
    setSel((s) => {
      const cur = new Set(s[col] ?? data.columns.find((c: any) => c.column === col).chars.map((x: any) => x.char));
      cur.has(ch) ? cur.delete(ch) : cur.add(ch);
      return { ...s, [col]: cur };
    });
  };
  const set = (fn: any, col: string, v: any) => fn((s: any) => ({ ...s, [col]: v }));

  const nameIssues = data.column_names.filter((n: any) => nameSel[n.column] ?? false);
  const cleanOp: OpRequest | null = nameIssues.length
    ? { op_type: "rename_columns", stage: "special_chars",
        params: { mapping: Object.fromEntries(nameIssues.map((n: any) => [n.column, n.proposed])) },
        target_columns: nameIssues.map((n: any) => n.column) }
    : null;

  return (
    <div>
      <h2 style={{ fontSize: "0.95rem" }}>Column names</h2>
      {data.column_names.length === 0 && <p className="muted">No special characters in column names.</p>}
      {data.column_names.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th></th><th>Column</th><th>Characters</th><th>Issues</th><th>Proposed name</th><th>Rename to</th><th></th></tr></thead>
            <tbody>
              {data.column_names.map((n: any) => {
                const custom = customNames[n.column] ?? n.proposed;
                const renameOp: OpRequest | null =
                  custom && custom.trim() && custom !== n.column
                    ? { op_type: "rename_columns", stage: "special_chars",
                        params: { mapping: { [n.column]: custom.trim() } },
                        target_columns: [n.column] }
                    : null;
                return (
                <tr key={n.column}>
                  <td><input type="checkbox" checked={nameSel[n.column] ?? false}
                    onChange={(e) => set(setNameSel, n.column, e.target.checked)} /></td>
                  <td><Highlighted text={n.column} chars={n.chars} /></td>
                  <td>
                    {n.chars.map((ch: string) => (
                      <code key={ch} className="char-chip" title={ch === " " ? "a space character" : `character ${ch}`}>{ch === " " ? "space" : ch}</code>
                    ))}
                  </td>
                  <td className="muted">{n.problems.map((p: string) => PROBLEM_LABELS[p] ?? p).join(", ")}</td>
                  <td>{n.proposed}</td>
                  <td>
                    <input type="text" value={custom} placeholder="new name"
                      onChange={(e) => set(setCustomNames, n.column, e.target.value)} />
                  </td>
                  <td>
                    {renameOp
                      ? <ApplyBar item={item} op={renameOp} label="Rename"
                          onDone={() => { setCustomNames({}); setNameSel({}); reload(); onChanged(); }} />
                      : <button className="primary" disabled
                          title="Enter a new name different from the current one">Rename</button>}
                  </td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {data.column_names.length > 0 && (
        <div style={{ marginTop: 8 }}>
          {cleanOp
            ? <ApplyBar item={item} op={cleanOp} label="Clean selected names"
                onDone={() => { setNameSel({}); reload(); onChanged(); }} />
            : <button className="primary" disabled
                title="Tick the checkbox on the columns you want renamed to the proposed name">
                Clean selected names
              </button>}
        </div>
      )}

      <h2 style={{ fontSize: "0.95rem", marginTop: 18 }}>Cell values</h2>
      {data.columns.length === 0 && <p className="muted">No special characters detected in any column's values.</p>}
      {data.columns.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Column</th><th>Characters found</th><th>Action</th><th></th></tr></thead>
            <tbody>
              {data.columns.map((c: any) => {
                const selected = sel[c.column] ?? new Set<string>(c.chars.map((x: any) => x.char));
                const action = act[c.column] ?? "keep";
                const chars = [...selected];
                const op: OpRequest | null =
                  action !== "keep" && chars.length
                    ? { op_type: "clean_special_chars", stage: "special_chars",
                        params: { column: c.column, chars, action, replacement: repl[c.column] ?? "" },
                        target_columns: [c.column] }
                    : null;
                const openChar = open && open[0] === c.column ? open[1] : null;
                return (
                  <Fragment key={c.column}>
                  <tr>
                    <td>{c.column}</td>
                    <td>
                      {c.chars.map((x: any) => {
                        const isOpen = open?.[0] === c.column && open?.[1] === x.char;
                        return (
                          <span key={x.char} style={{ display: "inline-flex", alignItems: "center", marginRight: 4 }}>
                            <label className={`char-chip${selected.has(x.char) ? " on" : ""}`}
                              title={`${x.count} occurrence(s), click eye to see cells`}>
                              <input type="checkbox" checked={selected.has(x.char)}
                                onChange={() => toggleChar(c.column, x.char)} />
                              {x.char === " " ? "space" : x.char}
                              <span className="muted">×{x.count}</span>
                            </label>
                            <button
                              className="ws-btn ws-btn-xs"
                              title="Show cells containing this character"
                              onClick={() => setOpen(isOpen ? null : [c.column, x.char])}
                            >{isOpen ? "hide ▾" : "view ▸"}</button>
                          </span>
                        );
                      })}
                    </td>
                    <td>
                      <select value={action} onChange={(e) => set(setAct, c.column, e.target.value)}>
                        {CHAR_ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
                      </select>
                      {action === "replace" && (
                        <input type="text" placeholder="with…" style={{ marginLeft: 6 }}
                          value={repl[c.column] ?? ""}
                          onChange={(e) => set(setRepl, c.column, e.target.value)} />
                      )}
                    </td>
                    <td>
                      {op && <ApplyBar item={item} op={op}
                        onDone={() => { reload(); onChanged(); }} />}
                    </td>
                  </tr>
                  {openChar !== null && (
                    <tr className="peek-row">
                      <td></td>
                      <td colSpan={3}>
                        <div className="muted" style={{ fontSize: "0.85rem" }}>
                          Cells containing <code className="char-chip on">{openChar}</code> in <strong>{c.column}</strong>:
                          <div className="peek-list">
                            {(c.chars.find((x: any) => x.char === openChar)?.samples ?? []).map((s: string, i: number) => (
                              <code key={i} className="peek-sample">
                                <Highlighted text={s} chars={[openChar]} />
                              </code>
                            ))}
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Missingness
// ---------------------------------------------------------------------------

function Missingness({ item, onChanged, refresh = 0, hist }: { item: ProjectItem; onChanged: () => void; refresh?: number; hist?: any }) {
  const { data, error, reload } = useAssessment(item, "missingness", [refresh]);
  const [params, setParams] = useState<Record<string, any>>({});
  if (error) return <div className="error">{error}</div>;
  if (!data) return <p>Assessing…</p>;

  // Columns that had a missingness op applied and haven't been undone
  const treated = new Set(
    (hist?.ops ?? [])
      .slice(0, hist?.pointer ?? 0)
      .filter((o: any) => o.op_type === "treat_missing" && o.stage === "missingness")
      .flatMap((o: any) => o.target_columns ?? [])
  );

  const set = (col: string, k: string, v: any) =>
    setParams((p) => ({ ...p, [col]: { ...p[col], [k]: v } }));

  return (
      <TRoot>
      <TTable>
        <THead>
          <TRow>
            <THeadCell>Column</THeadCell><THeadCell>Missing</THeadCell><THeadCell>Markers</THeadCell><THeadCell>Suggested type</THeadCell>
            <THeadCell>Recommendation</THeadCell><THeadCell>Treatment</THeadCell><THeadCell></THeadCell>
          </TRow>
        </THead>
        <TBody>
          {data.columns.map((c: any) => {
            const p = params[c.column] || {};
            const method = p.method || c.recommendation?.method || "leave";
            const needs = (k: string) => method === k;
            const op: OpRequest | null =
              method === "leave"
                ? null
                : {
                    op_type: "treat_missing",
                    stage: "missingness",
                    params: {
                      column: c.column,
                      method,
                      ...(p.value !== undefined && p.value !== "" ? { value: p.value } : {}),
                      ...(p.group_by ? { group_by: p.group_by.split(",").map((s: string) => s.trim()) } : {}),
                      ...(p.statistic ? { statistic: p.statistic } : {}),
                      ...(p.order_by ? { order_by: p.order_by } : {}),
                      ...(p.seed ? { seed: Number(p.seed) } : {}),
                    },
                    target_columns: [c.column],
                  };
            const clean = c.missing_count === 0;
            return (
              <TRow key={c.column}>
                <TCell>{c.column}</TCell>
                <TCell>{c.missing_count} ({c.missing_pct}%)</TCell>
                <TCell className="muted">
                  {Object.entries(c.marker_breakdown)
                    .filter(([, v]) => (v as number) > 0)
                    .map(([k, v]) => `${k === "null" ? "empty" : k}: ${v}`)
                    .join("  ") || ""}
                </TCell>
                <TCell>{c.suggested_type} <span className="muted">({c.current_dtype})</span></TCell>
                <TCell className="muted">
                  {clean
                    ? <span className="clean-status">{treated.has(c.column) ? "✓ missing values treated" : "✓ no missing values"}</span>
                    : c.recommendation?.reason || ""}
                </TCell>
                <TCell>
                  {clean ? null : (
                    <>
                  <select value={method} onChange={(e) => set(c.column, "method", e.target.value)}>
                    {c.available_methods.map((m: string) => (
                      <option key={m} value={m}>{m === "leave" ? "keep as-is" : m.replace(/_/g, " ")}</option>
                    ))}
                  </select>
                  {needs("constant") && (
                    <input type="text" placeholder="value"
                      onChange={(e) => set(c.column, "value", e.target.value)} />
                  )}
                  {needs("group_impute") && (
                    <>
                      <input type="text" placeholder="group by cols, comma-sep"
                        onChange={(e) => set(c.column, "group_by", e.target.value)} />
                      <select onChange={(e) => set(c.column, "statistic", e.target.value)}>
                        {["median", "mean", "mode", "min", "max"].map((s) => <option key={s}>{s}</option>)}
                      </select>
                    </>
                  )}
                  {(method.includes("fill") || method.includes("interpolate")) && (
                    <input type="text" placeholder="order by col"
                      onChange={(e) => set(c.column, "order_by", e.target.value)} />
                  )}
                    </>
                  )}
                </TCell>
                <TCell>
                  {!clean && <MissingRowsPeek item={item} column={c.column} />}
                  {!clean && (
                    op
                      ? <ApplyBar item={item} op={op} onDone={() => { reload(); onChanged(); }} />
                      : <button className="ws-btn ws-btn-primary ws-btn-sm" disabled
                          title="Choose a treatment from the dropdown, then apply it here">
                          Apply
                        </button>
                  )}
                </TCell>
              </TRow>
            );
          })}
        </TBody>
      </TTable>
      </TRoot>
  );
}

function MissingRowsPeek({ item, column }: { item: ProjectItem; column: string }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && !data) {
      getMissingRows(item.item_id, column, 8).then(setData).catch((e) => setErr(e.message));
    }
  };
  return (
    <span className="missing-peek">
      <button className="ws-btn ws-btn-xs" title="Show rows where this cell is missing" onClick={toggle}>
        {open ? "hide rows ▾" : "show rows ▸"}
      </button>
      {open && err && <span className="error">{err}</span>}
      {open && data && data.rows.length > 0 && (
        <div className="missing-peek-rows">
          <span className="muted">{data.missing_count} rows missing in this column, first {data.rows.length}:</span>
          <div className="peek-table-wrap">
            <table className="peek-table">
              <thead>
                <tr>
                  <th>#</th>
                  {Object.keys(data.rows[0]).filter((k) => k !== "__qdc_row_id").map((k) => (
                    <th key={k} className={k === column ? "peek-missing-col" : ""}>{k}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.rows.map((r: any) => (
                  <tr key={r.__qdc_row_id}>
                    <td className="muted">{r.__qdc_row_id}</td>
                    {Object.keys(r).filter((k) => k !== "__qdc_row_id").map((k) => (
                      <td key={k}>
                        {r[k] === null || r[k] === ""
                          ? <em className="peek-missing">missing</em>
                          : String(r[k])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {open && data && data.rows.length === 0 && <span className="muted"> no missing rows found</span>}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

const TYPE_TARGETS = ["integer", "decimal", "boolean", "string", "categorical", "date", "datetime", "time"];

function Types({ item, onChanged, refresh = 0 }: { item: ProjectItem; onChanged: () => void; refresh?: number }) {
  const { data, error, reload } = useAssessment(item, "types", [refresh]);
  const [choice, setChoice] = useState<Record<string, string>>({});
  const [fmt, setFmt] = useState<Record<string, string>>({});
  if (error) return <div className="error">{error}</div>;
  if (!data) return <p>Inferring types…</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th>Column</th><th>Current</th><th>Suggested</th><th>Compatible</th><th>Incompatible</th><th>Convert to</th><th></th></tr>
        </thead>
        <tbody>
          {data.columns.map((c: any) => {
            const target = choice[c.column] || c.suggested;
            const isTemporal = target === "date" || target === "datetime" || target === "time";
            const ambiguous = c.ambiguous_date_format && isTemporal;
            // Show a format input for any temporal conversion: prefilled with
            // the detected format, editable for formats we can't infer
            // (e.g. dot-separated times like 18.00.00).
            const detectedFmt = c.date_format || "";
            return (
              <tr key={c.column}>
                <td>{c.column}{c.identifier_detected && <span className="badge">id?</span>}</td>
                <td className="muted">{c.current_dtype}</td>
                <td>{c.suggested}</td>
                <td>{c.compatible_pct.toFixed(1)}%</td>
                <td className="muted">
                  {c.incompatible_count > 0 ? `${c.incompatible_count} (e.g. ${c.incompatible_examples.slice(0, 3).join(", ")})` : ""}
                </td>
                <td>
                  <select value={target} onChange={(e) => setChoice({ ...choice, [c.column]: e.target.value })}>
                    {TYPE_TARGETS.map((t) => <option key={t}>{t}</option>)}
                  </select>
                  {isTemporal && (
                    <span style={{ marginLeft: 6, display: "inline-flex", alignItems: "center", gap: 6 }}>
                      <input
                        type="text"
                        list="qdc-fmt-presets"
                        placeholder="format, e.g. %d/%m/%Y or %H.%M.%S"
                        style={{ width: 200 }}
                        value={fmt[c.column] ?? detectedFmt}
                        onChange={(e) => setFmt({ ...fmt, [c.column]: e.target.value })}
                      />
                      <datalist id="qdc-fmt-presets">
                        <option value="%d/%m/%Y">day-first d/m/y</option>
                        <option value="%m/%d/%Y">month-first m/d/y</option>
                        <option value="%Y-%m-%d">ISO date</option>
                        <option value="%d.%m.%Y">day-first d.m.y</option>
                        <option value="%Y-%m-%d %H:%M:%S">ISO datetime</option>
                        <option value="%H.%M.%S">time h.m.s</option>
                        <option value="%H:%M:%S">time h:m:s</option>
                      </datalist>
                      {ambiguous && !fmt[c.column] && (
                        <span className="badge needs_recalculation" title="Both day-first and month-first fit; pick one">ambiguous</span>
                      )}
                    </span>
                  )}
                </td>
                <td>
                  <ApplyBar
                    item={item}
                    op={{
                      op_type: "convert_type", stage: "types",
                      params: { column: c.column, target, ...((fmt[c.column] ?? detectedFmt) ? { format: fmt[c.column] ?? detectedFmt } : {}) },
                      target_columns: [c.column],
                    }}
                    label="Convert"
                    onDone={() => { reload(); onChanged(); }}
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Normalization
// ---------------------------------------------------------------------------

function Normalize({ item, onChanged, refresh = 0 }: { item: ProjectItem; onChanged: () => void; refresh?: number }) {
  const { data, error, reload } = useAssessment(item, "normalization", [refresh]);
  const [ops, setOps] = useState<Record<string, Set<string>>>({});
  const [mappings, setMappings] = useState<Record<string, Record<string, string>>>({});
  if (error) return <div className="error">{error}</div>;
  if (!data) return <p>Assessing…</p>;

  const toggle = (col: string, op: string) => {
    const cur = new Set(ops[col] || []);
    cur.has(op) ? cur.delete(op) : cur.add(op);
    setOps({ ...ops, [col]: cur });
  };

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th>Column</th><th>Safe ops (affected)</th><th>Meaning-changing (affected)</th><th>Label mappings</th><th></th></tr>
        </thead>
        <tbody>
          {data.columns.map((c: any) => {
            const selected = ops[c.column] || new Set<string>();
            const mapping = mappings[c.column];
            const opList = [
              ...[...selected].map((o) => ({ op: o })),
              ...(mapping ? [{ op: "map_values", mapping }] : []),
            ];
            return (
              <tr key={c.column}>
                <td>{c.column}</td>
                <td>
                  {Object.entries(c.safe).map(([k, v]) => (
                    <label key={k} style={{ display: "inline-flex", gap: 4, marginRight: 10 }}>
                      <input type="checkbox" checked={selected.has(k)} onChange={() => toggle(c.column, k)} />
                      {k.replace(/_/g, " ")} ({String(v)})
                    </label>
                  ))}
                </td>
                <td>
                  {Object.entries(c.meaning_changing).map(([k, v]) => (
                    <label key={k} style={{ display: "inline-flex", gap: 4, marginRight: 10 }}>
                      <input type="checkbox" checked={selected.has(k)} onChange={() => toggle(c.column, k)} />
                      {k.replace(/_/g, " ")} ({String(v)})
                    </label>
                  ))}
                </td>
                <td>
                  {c.suggested_mappings.slice(0, 3).map((m: any, i: number) => (
                    <div key={i} className="muted">
                      {m.variants.join(" / ")} → {m.canonical}{" "}
                      <button
                        style={{ padding: "1px 6px", fontSize: "0.75rem" }}
                        onClick={() =>
                          setMappings({
                            ...mappings,
                            [c.column]: Object.fromEntries(m.variants.map((v: string) => [v, m.canonical])),
                          })
                        }
                      >
                        map
                      </button>
                    </div>
                  ))}
                </td>
                <td>
                  {opList.length > 0 && (
                    <ApplyBar
                      item={item}
                      op={{ op_type: "normalize", stage: "normalization", params: { columns: [c.column], operations: opList }, target_columns: [c.column] }}
                      onDone={() => { reload(); onChanged(); }}
                    />
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Basic (names + formats) and invalid values & structures
// ---------------------------------------------------------------------------

function Basic({ item, onChanged, section, refresh = 0 }: { item: ProjectItem; onChanged: () => void; section: "names" | "invalid"; refresh?: number }) {
  const { data, error, reload } = useAssessment(item, "basic", [refresh]);
  const [rule, setRule] = useState<{ col: string; type: string; min: string; max: string; values: string }>({
    col: "", type: "range", min: "", max: "", values: "",
  });
  const [ruleResult, setRuleResult] = useState<any>(null);
  const [colNames, setColNames] = useState<string[]>([]);
  useEffect(() => {
    getPreview(item.item_id).then((p) => setColNames(p.columns.map((c) => c.name))).catch(() => null);
  }, [item.item_id, refresh]);
  if (error) return <div className="error">{error}</div>;
  if (!data) return <p>Assessing…</p>;

  if (section === "names") {
    const cn = data.column_names;
    const resolved: Record<string, string> = {};
    const counts: Record<string, number> = {};
    for (const [orig, prop] of Object.entries(cn.proposed_mapping as Record<string, string>)) {
      counts[prop] = (counts[prop] || 0) + 1;
      resolved[orig] = counts[prop] > 1 ? `${prop}_${counts[prop]}` : prop;
    }
    return (
      <div>
        <h2 style={{ fontSize: "0.95rem" }}>Column names</h2>
        {cn.issues.length === 0 && <p className="muted">No column-name issues.</p>}
        {cn.issues.map((i: any) => (
          <div key={i.column} className="muted">
            “{i.column}” → <strong>{resolved[i.column]}</strong> ({i.problems.join(", ")})
          </div>
        ))}
        {Object.keys(cn.collisions).length > 0 && (
          <div className="notice">Collisions resolved with suffixes: {JSON.stringify(cn.collisions)}</div>
        )}
        {cn.issues.length > 0 && (
          <ApplyBar
            item={item}
            op={{ op_type: "rename_columns", stage: "column_names", params: { mapping: resolved }, target_columns: Object.keys(resolved) }}
            label="Apply names"
            onDone={() => { reload(); onChanged(); }}
          />
        )}
        <h2 style={{ fontSize: "0.95rem", marginTop: 16 }}>Format standardization candidates</h2>
        {data.format_candidates.length === 0 && <p className="muted">None detected.</p>}
        {data.format_candidates.map((f: any) => (
          <div key={f.column} className="muted" style={{ marginBottom: 6 }}>
            {f.column}: {f.note}{" "}
            <ApplyBar
              item={item}
              op={{ op_type: "standardize_format", stage: "column_names", params: { column: f.column, kind: f.kind }, target_columns: [f.column] }}
              label="Standardize"
              onDone={() => { reload(); onChanged(); }}
            />
          </div>
        ))}
      </div>
    );
  }

  // invalid values & structures
  const s = data.structures;
  return (
    <div>
      <h2 style={{ fontSize: "0.95rem" }}>Empty & constant structures</h2>
      <div className="muted">
        {s.empty_row_count} empty rows{" "}
        {s.empty_row_count > 0 && (
          <ApplyBar
            item={item}
            op={{ op_type: "drop_rows", stage: "structures", params: { row_ids: s.empty_row_ids } }}
            label="Remove empty rows"
            onDone={() => { reload(); onChanged(); }}
          />
        )}
      </div>
      <div className="muted">
        Empty columns: {s.empty_columns.join(", ") || "none"}{" "}
        {s.empty_columns.map((c: string) => (
          <ApplyBar key={c} item={item}
            op={{ op_type: "drop_column", stage: "structures", params: { column: c }, target_columns: [c] }}
            label={`Drop ${c}`}
            onDone={() => { reload(); onChanged(); }} />
        ))}
      </div>
      <div className="muted">
        Constant columns: {s.constant_columns.map((c: any) => `${c.column}=${JSON.stringify(c.value)}`).join(", ") || "none"}{" "}
        {s.constant_columns.map((c: any) => (
          <ApplyBar key={c.column} item={item}
            op={{ op_type: "drop_column", stage: "structures", params: { column: c.column }, target_columns: [c.column] }}
            label={`Drop ${c.column}`}
            onDone={() => { reload(); onChanged(); }} />
        ))}
      </div>

      <h2 style={{ fontSize: "0.95rem", marginTop: 16 }}>Invalid-value rules</h2>
      <div className="row" style={{ alignItems: "flex-end", marginBottom: 8 }}>
        <div>
          <label>Column</label>
          <select value={rule.col} onChange={(e) => setRule({ ...rule, col: e.target.value })}>
            <option value="">choose column…</option>
            {colNames.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
        <div>
          <label>Rule</label>
          <select value={rule.type} onChange={(e) => setRule({ ...rule, type: e.target.value })}>
            <option value="range">numeric range</option>
            <option value="not_in">allowed values</option>
            <option value="unparseable_date">unparseable date</option>
            <option value="unparseable_numeric">unparseable number</option>
          </select>
        </div>
        {rule.type === "range" && (
          <>
            <div><label>Min</label><input type="text" value={rule.min} onChange={(e) => setRule({ ...rule, min: e.target.value })} /></div>
            <div><label>Max</label><input type="text" value={rule.max} onChange={(e) => setRule({ ...rule, max: e.target.value })} /></div>
          </>
        )}
        {rule.type === "not_in" && (
          <div><label>Allowed (comma-sep)</label><input type="text" value={rule.values} onChange={(e) => setRule({ ...rule, values: e.target.value })} /></div>
        )}
        <div>
          <button
            disabled={!rule.col}
            title={rule.col ? "Check how many values break this rule" : "Pick a column first"}
            onClick={() => {
              const r: any = { type: rule.type };
              if (rule.type === "range") {
                if (rule.min !== "") r.min = Number(rule.min);
                if (rule.max !== "") r.max = Number(rule.max);
              }
              if (rule.type === "not_in") r.values = rule.values.split(",").map((v) => v.trim());
              assessInvalid(item.item_id, rule.col, r).then(setRuleResult).catch((e) => setRuleResult({ error: e.message }));
            }}
          >
            Evaluate
          </button>
        </div>
      </div>
      {ruleResult && !ruleResult.error && (
        <div className="muted">
          {ruleResult.invalid_count} invalid ({ruleResult.invalid_pct}%); e.g.{" "}
          {ruleResult.examples.slice(0, 3).map((e: any) => JSON.stringify(e[rule.col])).join(", ")}
          <div style={{ marginTop: 6, display: "flex", gap: 8 }}>
            <ApplyBar item={item}
              op={{ op_type: "mask_values", stage: "invalid_values", params: { column: rule.col, rule: ruleResult.rule }, target_columns: [rule.col] }}
              label="Set invalid to missing"
              onDone={() => { reload(); onChanged(); setRuleResult(null); }} />
            <ApplyBar item={item}
              op={{ op_type: "drop_rows", stage: "invalid_values", params: { row_ids: ruleResult.row_ids } }}
              label="Remove invalid rows"
              onDone={() => { reload(); onChanged(); setRuleResult(null); }} />
          </div>
        </div>
      )}
      {ruleResult?.error && <div className="error">{ruleResult.error}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Duplicates
// ---------------------------------------------------------------------------

function Duplicates({ item, onChanged, refresh = 0 }: { item: ProjectItem; onChanged: () => void; refresh?: number }) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [cols, setCols] = useState<Set<string>>(new Set());
  const [blocking, setBlocking] = useState<Set<string>>(new Set());
  const [threshold, setThreshold] = useState(100);
  const [est, setEst] = useState<any>(null);
  const [result, setResult] = useState<any>(null);
  const [progress, setProgress] = useState("");
  const [decisions, setDecisions] = useState<Record<number, { action: string; keep?: number }>>({});
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    getPreview(item.item_id).then(setPreview);
    dupResult(item.item_id).then(setResult).catch(() => null);
  }, [item.item_id, refresh]);

  const allCols = useMemo(() => preview?.columns.map((c) => c.name) ?? [], [preview]);
  // Default: compare on all columns. User can untick selectively.
  useEffect(() => { setCols(new Set(allCols)); setBlocking(new Set()); }, [allCols]);
  const toggle = (set: Set<string>, v: string, fn: (s: Set<string>) => void) => {
    const n = new Set(set);
    n.has(v) ? n.delete(v) : n.add(v);
    fn(n);
  };

  const cfg = { columns: [...cols], blocking_columns: [...blocking], threshold };

  const estimate = () =>
    dupEstimate(item.item_id, cfg).then(setEst).catch((e) => setErr(e.message));

  const analyze = async () => {
    setErr(null);
    const { job_id } = await dupAnalyze(item.item_id, cfg);
    await pollJob(job_id, (j) =>
      setProgress(j.progress.total ? `${j.progress.message} (${j.progress.done}/${j.progress.total})` : j.progress.message || "Analyzing…"),
    );
    setResult(await dupResult(item.item_id));
    setProgress("");
  };

  const resolve = async () => {
    const ds = Object.entries(decisions).map(([k, v]) => ({
      row_ids: JSON.parse(k) as number[],
      action: v.action,
      keep_row_id: v.keep,
    }));
    await dupResolve(item.item_id, ds);
    setDecisions({});
    setResult(null);
    onChanged();
  };

  return (
    <div>
      <div className="row" style={{ alignItems: "flex-start" }}>
        <div>
          <label>Comparison columns (required)</label>
          <div className="muted" style={{ fontSize: "0.8rem", marginBottom: 4 }}>
            Fields compared to decide whether two rows are duplicates.
            {" "}<a className="mini-link" onClick={() => setCols(new Set(allCols))}>all</a>
            {" / "}<a className="mini-link" onClick={() => setCols(new Set())}>none</a>
          </div>
          {allCols.map((c) => (
            <label key={c} style={{ display: "inline-flex", gap: 4, marginRight: 8 }}>
              <input type="checkbox" checked={cols.has(c)} onChange={() => toggle(cols, c, setCols)} />{c}
            </label>
          ))}
        </div>
        <div>
          <label>Blocking columns (optional)</label>
          <div className="muted" style={{ fontSize: "0.8rem", marginBottom: 4 }}>
            Performance only: rows are bucketed by these columns and only compared
            within a bucket. Use at &lt;100% on large data; leave empty for exact match.
            {" "}<a className="mini-link" onClick={() => setBlocking(new Set(allCols))}>all</a>
            {" / "}<a className="mini-link" onClick={() => setBlocking(new Set())}>none</a>
          </div>
          {allCols.map((c) => (
            <label key={c} style={{ display: "inline-flex", gap: 4, marginRight: 8 }}>
              <input type="checkbox" checked={blocking.has(c)} onChange={() => toggle(blocking, c, setBlocking)} />{c}
            </label>
          ))}
        </div>
        <div>
          <label>Min match threshold</label>
          <div className="muted" style={{ fontSize: "0.8rem", marginBottom: 4 }}>
            100% = exact duplicates; 90/80% = near-duplicates (typo-tolerant).
          </div>
          <select value={threshold} onChange={(e) => setThreshold(Number(e.target.value))}>
            {[100, 90, 80].map((t) => <option key={t} value={t}>{t}%</option>)}
          </select>
        </div>
      </div>
      <div style={{ marginTop: 10, display: "flex", gap: 8, alignItems: "center" }}>
        <button onClick={estimate} disabled={cols.size === 0}>Estimate candidate pairs</button>
        <button className="primary" onClick={analyze} disabled={cols.size === 0 || est?.blocked}>
          Run analysis
        </button>
        {progress && <span className="muted">{progress}</span>}
      </div>
      {est && (
        <div className={est.blocked ? "error" : est.warning ? "notice" : "muted"} style={{ marginTop: 8 }}>
          ~{est.candidate_pairs.toLocaleString()} candidate pairs ({est.basis}).
          Attainable match percentages: {est.attainable_pcts.join("%, ")}%.
          {est.blocked && " Blocked: reduce comparison columns or add blocking columns."}
          {est.warning && !est.blocked && " Warning: this may take a while."}
        </div>
      )}
      {err && <div className="error">{err}</div>}

      {result && result.mode === "exact" && (
        <div style={{ marginTop: 14 }}>
          <h2 style={{ fontSize: "0.95rem" }}>
            {result.summary.duplicate_groups} exact-duplicate groups · {result.summary.affected_rows} rows affected
          </h2>
          {result.groups.slice(0, 50).map((g: any, i: number) => (
            <div key={i} className="muted" style={{ marginBottom: 6 }}>
              Rows {g.members.join(", ")} (100% match)
              <select
                style={{ marginLeft: 8 }}
                onChange={(e) => setDecisions({ ...decisions, [JSON.stringify(g.members)]: { action: e.target.value } })}
              >
                <option value="">choose action…</option>
                <option value="keep_all">keep all</option>
                <option value="keep_first">keep first</option>
                <option value="keep_last">keep last</option>
                <option value="keep_most_complete">keep most complete</option>
              </select>
            </div>
          ))}
        </div>
      )}

      {result && result.mode === "near" && (
        <div style={{ marginTop: 14 }}>
          <h2 style={{ fontSize: "0.95rem" }}>
            {result.summary.clusters} near-duplicate clusters · {result.summary.qualifying_pairs} qualifying pairs
          </h2>
          <p className="muted">
            Each cluster is anchored to a representative record; member scores are vs. the representative;
            members are not guaranteed to match each other.
          </p>
          {result.clusters.slice(0, 30).map((c: any, i: number) => (
            <div key={i} className="muted" style={{ marginBottom: 6 }}>
              Rep row {c.representative_row_id}:{" "}
              {c.members.map((m: any) => `row ${m.row_id} (${m.score_pct}%)`).join(", ")}
              <select
                style={{ marginLeft: 8 }}
                onChange={(e) =>
                  setDecisions({
                    ...decisions,
                    [JSON.stringify([c.representative_row_id, ...c.members.map((m: any) => m.row_id)])]:
                      { action: e.target.value, keep: c.representative_row_id },
                  })
                }
              >
                <option value="">choose action…</option>
                <option value="keep_all">keep all</option>
                <option value="keep_selected">keep representative only</option>
                <option value="keep_most_complete">keep most complete</option>
              </select>
            </div>
          ))}
        </div>
      )}

      {result && result.mode === "blocked" && <div className="error">{result.reason}</div>}

      {Object.keys(decisions).length > 0 && (
        <button className="primary" style={{ marginTop: 10 }} onClick={resolve}>
          Apply {Object.keys(decisions).length} resolution(s)
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Units & scales
// ---------------------------------------------------------------------------

function UnitsPanel({ item, onChanged, refresh = 0 }: { item: ProjectItem; onChanged: () => void; refresh?: number }) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [col, setCol] = useState("");
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [factors, setFactors] = useState<Record<string, string>>({});
  const [target, setTarget] = useState("");
  const [scaleCond, setScaleCond] = useState<{ op: string; value: string; factor: string }>({ op: "<=", value: "1", factor: "100" });

  useEffect(() => { getPreview(item.item_id).then(setPreview); }, [item.item_id, refresh]);

  const evaluate = (c: string) => {
    setCol(c); setData(null); setErr(null); setFactors({}); setTarget("");
    if (!c) return;
    assessUnits(item.item_id, c).then(setData).catch((e) => setErr(e.message));
  };

  const rules: any[] = [];
  if (data) {
    for (const g of data.groups) {
      const f = factors[g.canonical];
      if (f !== undefined && f !== "") rules.push({ unit: g.canonical, factor: Number(f) });
    }
    if (factors[""] !== undefined && factors[""] !== "") rules.push({ unit: "", factor: Number(factors[""]) });
    if (data.scale_hint && scaleCond.factor !== "") {
      rules.push({ condition: { op: scaleCond.op, value: Number(scaleCond.value) }, factor: Number(scaleCond.factor) });
    }
  }
  const op: OpRequest | null = col && rules.length > 0
    ? { op_type: "convert_units", stage: "units",
        params: { column: col, rules, target_unit: target || undefined }, target_columns: [col] }
    : null;

  const allCols = preview?.columns.map((c) => c.name) ?? [];
  return (
    <div>
      <div className="row" style={{ alignItems: "flex-end", marginBottom: 8 }}>
        <div>
          <label>Column</label>
          <select value={col} onChange={(e) => evaluate(e.target.value)}>
            <option value="">choose column…</option>
            {allCols.map((c) => <option key={c}>{c}</option>)}
          </select>
        </div>
      </div>
      {err && <div className="error">{err}</div>}
      {data && (
        <div>
          {data.groups.length === 0 && !data.scale_hint && (
            <p className="muted">No unit tokens or mixed-scale hints detected in {col}.</p>
          )}
          {data.groups.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Unit</th><th>Position</th><th>Count</th><th>Range</th><th>Examples</th><th>× factor</th></tr></thead>
                <tbody>
                  {data.groups.map((g: any) => (
                    <tr key={g.canonical}>
                      <td><code className="char-chip">{g.unit}</code>{g.is_currency && <span className="muted"> (currency, enter your own rate)</span>}</td>
                      <td className="muted">{g.position}</td>
                      <td>{g.count}</td>
                      <td className="muted">{g.min} to {g.max}</td>
                      <td className="muted">{g.samples.join(", ")}</td>
                      <td>
                        <input type="text" style={{ width: 90 }} placeholder="skip"
                          value={factors[g.canonical] ?? g.suggested_factors?.[target] ?? ""}
                          onChange={(e) => setFactors({ ...factors, [g.canonical]: e.target.value })} />
                      </td>
                    </tr>
                  ))}
                  {data.plain?.count > 0 && (
                    <tr>
                      <td className="muted">(no unit, plain numbers)</td>
                      <td className="muted"></td>
                      <td>{data.plain.count}</td>
                      <td className="muted">{data.plain.min} to {data.plain.max}</td>
                      <td className="muted"></td>
                      <td>
                        <input type="text" style={{ width: 90 }} placeholder="leave as-is"
                          value={factors[""] ?? ""}
                          onChange={(e) => setFactors({ ...factors, [""]: e.target.value })} />
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
          {data.unmatched?.count > 0 && (
            <p className="muted">
              {data.unmatched.count} value(s) could not be parsed and will be left unchanged:{" "}
              {data.unmatched.samples.map((s: string) => `"${s}"`).join(", ")}
            </p>
          )}
          {data.scale_hint && (
            <div className="notice" style={{ marginBottom: 8 }}>
              <strong>Possible mixed scale:</strong> {data.scale_hint.note}
              <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center" }}>
                <span>Rule: values</span>
                <select value={scaleCond.op} onChange={(e) => setScaleCond({ ...scaleCond, op: e.target.value })}>
                  {["<=", ">=", "<", ">", "==", "all"].map((o) => <option key={o}>{o}</option>)}
                </select>
                {scaleCond.op !== "all" && (
                  <input type="text" style={{ width: 70 }} value={scaleCond.value}
                    onChange={(e) => setScaleCond({ ...scaleCond, value: e.target.value })} />
                )}
                <span>multiply by</span>
                <input type="text" style={{ width: 70 }} value={scaleCond.factor}
                  onChange={(e) => setScaleCond({ ...scaleCond, factor: e.target.value })} />
              </div>
            </div>
          )}
          {data.groups.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <label>Target unit (metadata only; shown in the report, never appended to values)</label>
              <input type="text" style={{ width: 140 }} value={target}
                placeholder="e.g. kg" onChange={(e) => setTarget(e.target.value)} />
            </div>
          )}
          {op && (
            <ApplyBar item={item} op={op} label="Convert units"
              onDone={() => { evaluate(col); onChanged(); }} />
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Patterns
// ---------------------------------------------------------------------------

const PATTERN_LABELS: Record<string, string> = {
  email: "email address",
  phone_intl: "phone number",
  postcode_us: "US ZIP code",
  postcode_uk: "UK postcode",
  digits_only: "digits only",
  alphanumeric_id: "letters/digits/underscore",
};

const PATTERN_ACTIONS = [
  ["set_missing", "set non-matching to missing"],
  ["extract", "extract matching part (capture group)"],
  ["strip_affix", "remove prefix/suffix"],
  ["pad", "pad with leading zeros to length"],
  ["replace", "regex replace"],
];

function PatternsPanel({ item, onChanged, refresh = 0 }: { item: ProjectItem; onChanged: () => void; refresh?: number }) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [presets, setPresets] = useState<string[]>([]);
  const [col, setCol] = useState("");
  const [pattern, setPattern] = useState("");
  const [custom, setCustom] = useState("");
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [action, setAction] = useState("set_missing");
  const [group, setGroup] = useState("1");
  const [affix, setAffix] = useState<{ prefix: string; suffix: string }>({ prefix: "", suffix: "" });
  const [padLen, setPadLen] = useState("");
  const [replacement, setReplacement] = useState("");

  useEffect(() => {
    getPreview(item.item_id).then(setPreview);
    patternPresets(item.item_id).then((r) => setPresets(r.presets)).catch(() => null);
  }, [item.item_id, refresh]);

  const stringCols = useMemo(
    () => preview?.columns.filter((c) => /str|utf8|string|categorical/i.test(c.dtype)).map((c) => c.name) ?? [],
    [preview],
  );

  const effective = custom.trim() || pattern;
  const evaluate = () => {
    setRes(null); setErr(null);
    assessPattern(item.item_id, col, effective).then(setRes).catch((e) => setErr(e.message));
  };

  const params: Record<string, unknown> = { column: col, action };
  if (action !== "strip_affix" && action !== "pad") params.pattern = res?.pattern ?? effective;
  if (action === "extract") params.group = Number(group);
  if (action === "strip_affix") { if (affix.prefix) params.prefix = affix.prefix; if (affix.suffix) params.suffix = affix.suffix; }
  if (action === "pad") params.length = Number(padLen);
  if (action === "replace") params.replacement = replacement;

  const actionReady =
    res && (action === "strip_affix" ? !!(affix.prefix || affix.suffix)
      : action === "pad" ? !!padLen && Number(padLen) > 0
      : true);
  const op: OpRequest | null = actionReady
    ? { op_type: "clean_pattern", stage: "patterns", params, target_columns: [col] }
    : null;

  return (
    <div>
      <div className="row" style={{ alignItems: "flex-end", marginBottom: 8 }}>
        <div>
          <label>Column</label>
          <select value={col} onChange={(e) => { setCol(e.target.value); setRes(null); }} disabled={!!preview && stringCols.length === 0}>
            <option value="">{preview && stringCols.length === 0 ? "no text columns" : "choose column…"}</option>
            {stringCols.map((c) => <option key={c}>{c}</option>)}
          </select>
          {preview && stringCols.length === 0 && (
            <p className="muted" style={{ fontSize: "0.8rem", margin: "4px 0 0", maxWidth: 320 }}>
              This dataset has no text columns; pattern checks apply to string values like IDs, emails, and postcodes.
            </p>
          )}
        </div>
        <div>
          <label>Expected pattern</label>
          <select value={pattern} onChange={(e) => { setPattern(e.target.value); setRes(null); }}>
            <option value="">choose preset…</option>
            {presets.map((p) => <option key={p} value={p}>{PATTERN_LABELS[p] ?? p}</option>)}
          </select>
        </div>
        <div>
          <label>or custom regex</label>
          <input type="text" style={{ width: 220 }} placeholder="e.g. ^[A-Z]{2}\d{4}$"
            value={custom} onChange={(e) => { setCustom(e.target.value); setRes(null); }} />
        </div>
        <div>
          <button className="primary" disabled={!col || !effective}
            title={!col ? "Pick a column" : !effective ? "Pick or write a pattern" : "Count non-matching values"}
            onClick={evaluate}>Evaluate</button>
        </div>
      </div>
      {err && <div className="error">{err}</div>}
      {res && (
        <div>
          <p className="muted">
            {res.matching}/{res.total} values match ({res.pct_matching}%), {res.non_matching} non-matching.
            {" "}<code className="char-chip">{res.pattern}</code>
          </p>
          {res.samples.length > 0 && (
            <p className="muted" style={{ fontSize: "0.82rem" }}>
              Non-matching: {res.samples.map((s: string) => `"${s}"`).join(", ")}
            </p>
          )}
          <div className="row" style={{ alignItems: "flex-end" }}>
            <div>
              <label>Action</label>
              <select value={action} onChange={(e) => setAction(e.target.value)}>
                {PATTERN_ACTIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </div>
            {action === "extract" && (
              <div><label>capture group #</label>
                <input type="text" style={{ width: 60 }} value={group} onChange={(e) => setGroup(e.target.value)} /></div>
            )}
            {action === "strip_affix" && (
              <>
                <div><label>prefix</label>
                  <input type="text" style={{ width: 90 }} value={affix.prefix} onChange={(e) => setAffix({ ...affix, prefix: e.target.value })} /></div>
                <div><label>suffix</label>
                  <input type="text" style={{ width: 90 }} value={affix.suffix} onChange={(e) => setAffix({ ...affix, suffix: e.target.value })} /></div>
              </>
            )}
            {action === "pad" && (
              <div><label>pad to length</label>
                <input type="text" style={{ width: 60 }} value={padLen} onChange={(e) => setPadLen(e.target.value)} /></div>
            )}
            {action === "replace" && (
              <div><label>replace with</label>
                <input type="text" style={{ width: 140 }} value={replacement} onChange={(e) => setReplacement(e.target.value)} /></div>
            )}
            <ApplyBar item={item} op={op} label="Apply"
              onDone={() => { evaluate(); onChanged(); }} />
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Keys & uniqueness
// ---------------------------------------------------------------------------

function KeysPanel({ item, onChanged, refresh = 0 }: { item: ProjectItem; onChanged: () => void; refresh?: number }) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [cols, setCols] = useState<Set<string>>(new Set());
  const [declared, setDeclared] = useState<string[] | null>(null);
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [keep, setKeep] = useState<Record<string, number>>({});
  const [fixCell, setFixCell] = useState<{ row_id: number; value: string } | null>(null);

  useEffect(() => {
    getPreview(item.item_id).then(setPreview);
    getKeyDeclaration(item.item_id).then((r) => {
      const d = r.declared?.columns ?? null;
      setDeclared(d);
      if (d) setCols(new Set(d));
    }).catch(() => null);
  }, [item.item_id, refresh]);

  const allCols = preview?.columns.map((c) => c.name) ?? [];
  const toggle = (c: string) => {
    const n = new Set(cols);
    n.has(c) ? n.delete(c) : n.add(c);
    setCols(n); setRes(null);
  };

  const evaluate = () => {
    setRes(null); setErr(null);
    assessKeys(item.item_id, [...cols]).then(setRes).catch((e) => setErr(e.message));
  };

  const declare = async () => {
    await declareKey(item.item_id, [...cols]);
    setDeclared([...cols]);
  };

  const keyStr = (g: any) => Object.entries(g.key).map(([k, v]) => `${k}=${v}`).join(", ");

  return (
    <div>
      <div style={{ marginBottom: 8 }}>
        <label>Key columns (must be unique together)</label>
        <p className="muted" style={{ fontSize: "0.8rem", margin: "0 0 4px" }}>
          Declaring a key re-checks it during final validation; violations surface as warnings even if introduced later.
        </p>
        {allCols.map((c) => (
          <label key={c} style={{ display: "inline-flex", gap: 4, marginRight: 8 }}>
            <input type="checkbox" checked={cols.has(c)} onChange={() => toggle(c)} />{c}
          </label>
        ))}
      </div>
      <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
        <button disabled={cols.size === 0} onClick={declare}>
          {declared ? `Declared: ${declared.join(" + ")}, update` : "Declare as key"}
        </button>
        <button className="primary" disabled={cols.size === 0} onClick={evaluate}>Evaluate</button>
      </div>
      {err && <div className="error">{err}</div>}
      {res && (() => {
        const distinctKeys = res.unique_rows + res.repeated_identical.count + res.repeated_conflicting.count;
        const rowCount = preview?.row_count ?? 0;
        const lowCard = rowCount > 0 && distinctKeys < rowCount * 0.5;
        return (
        <div>
          <p className="muted">
            {distinctKeys} distinct key value{distinctKeys === 1 ? "" : "s"} across {rowCount} rows:{" "}
            {res.unique_rows} appear{res.unique_rows === 1 ? "s" : ""} exactly once ·{" "}
            {res.missing.count} rows missing a key value ·{" "}
            {res.repeated_identical.count} repeated keys where the rows are fully identical ·{" "}
            {res.repeated_conflicting.count} repeated keys where the rows differ in other columns
          </p>
          {lowCard && (
            <p className="notice">
              This combination has only {distinctKeys} distinct values for {rowCount} rows, so it is unlikely to be a
              unique record key (e.g. an employee or category ID repeated across many records). Pick a different
              column or combination, or add columns until each row is identified uniquely.
            </p>
          )}

          {res.missing.count > 0 && (
            <div className="notice" style={{ marginBottom: 10 }}>
              <strong>{res.missing.count} rows missing a key value</strong>, rows:{" "}
              {res.missing.row_ids.join(", ")}
              {res.missing.samples.map((s: any, i: number) => (
                <div key={i} className="muted" style={{ fontSize: "0.82rem" }}>{JSON.stringify(s)}</div>
              ))}
              <div style={{ marginTop: 6, display: "flex", gap: 8, alignItems: "center" }}>
                <input type="text" placeholder="fill missing keys with…" value={fixCell?.row_id === -1 ? fixCell.value : ""}
                  onChange={(e) => setFixCell({ row_id: -1, value: e.target.value })} style={{ width: 170 }} />
                {fixCell?.row_id === -1 && fixCell.value !== "" && (
                  <ApplyBar item={item}
                    op={{ op_type: "treat_rows", stage: "keys",
                      params: { column: res.columns[0], row_ids: res.missing.row_ids, method: "value", value: fixCell.value },
                      target_columns: res.columns }}
                    label="Set missing keys"
                    onDone={() => { setFixCell(null); evaluate(); onChanged(); }} />
                )}
              </div>
            </div>
          )}

          {res.repeated_identical.groups.map((g: any, i: number) => (
            <div key={i} className="notice" style={{ marginBottom: 8 }}>
              <strong>{keyStr(g)}</strong>, {g.row_ids.length} identical rows: {g.row_ids.join(", ")}
              <div style={{ marginTop: 4 }}>
                <ApplyBar item={item}
                  op={{ op_type: "drop_rows", stage: "keys", params: { row_ids: g.row_ids.slice(1) }, target_columns: res.columns }}
                  label={`Remove ${g.row_ids.length - 1} duplicate(s), keep #${g.row_ids[0]}`}
                  onDone={() => { evaluate(); onChanged(); }} />
              </div>
            </div>
          ))}

          {res.repeated_conflicting.groups.map((g: any, gi: number) => (
            <div key={gi} className="notice" style={{ marginBottom: 8 }}>
              <strong>{keyStr(g)}</strong>, {g.row_ids.length} rows, conflicting columns:{" "}
              {Object.keys(g.conflicts).join(", ")}
              <table style={{ marginTop: 6 }}>
                <thead><tr><th>keep</th><th>row</th>{Object.keys(g.conflicts).map((c) => <th key={c}>{c}</th>)}</tr></thead>
                <tbody>
                  {g.row_ids.map((rid: number, ri: number) => (
                    <tr key={rid}>
                      <td>
                        <input type="radio" name={`keep-${gi}`} checked={keep[`${gi}`] === rid}
                          onChange={() => setKeep({ ...keep, [gi]: rid })} />
                      </td>
                      <td>#{rid}</td>
                      {Object.keys(g.conflicts).map((c) => (
                        <td key={c}>{String(g.conflicts[c][ri] ?? "")}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
              <div style={{ marginTop: 6, display: "flex", gap: 8, alignItems: "center" }}>
                {keep[gi] !== undefined && (
                  <ApplyBar item={item}
                    op={{ op_type: "drop_rows", stage: "keys",
                      params: { row_ids: g.row_ids.filter((r: number) => r !== keep[gi]) },
                      target_columns: res.columns }}
                    label={`Keep #${keep[gi]}, remove others`}
                    onDone={() => { evaluate(); onChanged(); }} />
                )}
                <input type="text" placeholder="correct key value…" value={fixCell?.row_id === gi ? fixCell.value : ""}
                  onChange={(e) => setFixCell({ row_id: gi, value: e.target.value })} style={{ width: 140 }} />
                {fixCell?.row_id === gi && fixCell.value !== "" && (
                  <ApplyBar item={item}
                    op={{ op_type: "treat_rows", stage: "keys",
                      params: { column: res.columns[0], row_ids: g.row_ids.slice(1), method: "value", value: fixCell.value },
                      target_columns: res.columns }}
                    label="Correct key"
                    onDone={() => { setFixCell(null); evaluate(); onChanged(); }} />
                )}
              </div>
            </div>
          ))}
        </div>
        );
      })()}
    </div>
  );
}
