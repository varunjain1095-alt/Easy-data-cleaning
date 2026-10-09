import { useEffect, useState } from "react";
import { DownloadIcon, ShieldCheckIcon } from "lucide-react";
import {
  ApiError,
  downloadExport,
  exportWarnings,
  listExports,
  pollJob,
  Project,
  runValidation,
  startExport,
} from "./api";

const FORMAT_GROUPS = [
  {
    title: "Export cleaned data",
    formats: [
      { v: "csv", label: "CSV" },
      { v: "xlsx", label: "XLSX" },
      { v: "flagged", label: "Flagged records" },
      { v: "zip", label: "ZIP bundle" },
    ],
  },
  {
    title: "Export cleaning report",
    formats: [{ v: "report", label: "HTML report" }],
  },
  {
    title: "Export workflow",
    formats: [
      { v: "json", label: "JSON config" },
      { v: "script", label: "Python script" },
    ],
  },
];

export default function ExportPanel({ project }: { project: Project }) {
  const [formats, setFormats] = useState<Set<string>>(new Set(["csv", "xlsx"]));
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [progress, setProgress] = useState("");
  const [files, setFiles] = useState<{ name: string; size: number; kind: string }[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [validation, setValidation] = useState<Record<string, any>>({});
  const [warnings, setWarnings] = useState<string[]>([]);
  const [ack, setAck] = useState(false);

  const multi = project.items.length > 1;

  useEffect(() => {
    listExports().then((r) => setFiles(r.files)).catch(() => null);
    exportWarnings().then((r) => setWarnings(r.warnings)).catch(() => null);
  }, [project]);

  const toggle = (set: Set<string>, v: string, fn: (s: Set<string>) => void) => {
    const n = new Set(set);
    n.has(v) ? n.delete(v) : n.add(v);
    fn(n);
  };

  const runExport = async () => {
    setErr(null);
    try {
      const { job_id } = await startExport([...formats], [...excluded], ack);
      const job = await pollJob(job_id, (j) =>
        setProgress(j.progress.message || "Exporting…"));
      const manifest = job.result as { files: { name: string; size: number; kind: string }[] };
      setFiles(manifest.files);
      setProgress("");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
      setProgress("");
    }
  };

  const validate = async (itemId: string) => {
    const { job_id } = await runValidation(itemId);
    const job = await pollJob(job_id, (j) => setProgress(j.progress.message || "Validating…"));
    setValidation((v) => ({ ...v, [itemId]: job.result }));
    setProgress("");
  };

  return (
    <div className="ws-card ws-export">
      <h2><ShieldCheckIcon aria-hidden="true" /> Validate &amp; export</h2>
      <p className="muted ws-export-note">
        Workbook export recreates cleaned sheets as value-based tables; formatting, formulas,
        charts, merged cells, and named ranges in cleaned sheets are not preserved.
        {project.kind === "xls" && " This .xls upload exports as .xlsx."}
      </p>

      <div className="ws-export-section">
        {project.items.map((item) => (
          <div key={item.item_id} className="ws-dataset-row">
            <div className="ws-dataset-head">
              <strong>{item.name}</strong>
              <span className={`badge ${item.status}`}>{item.status.replace(/_/g, " ")}</span>
              {item.status !== "rejected" && (
                <>
                  <button className="ws-btn ws-btn-outline ws-btn-xs"
                    onClick={() => validate(item.item_id)}>
                    Run validation
                  </button>
                  {multi && (
                    <label className="ws-check">
                      <input type="checkbox" checked={excluded.has(item.item_id)}
                        onChange={() => toggle(excluded, item.item_id, setExcluded)} />
                      exclude from export
                    </label>
                  )}
                </>
              )}
            </div>
            {validation[item.item_id] && <ValidationTable v={validation[item.item_id]} />}
          </div>
        ))}
      </div>

      {warnings.length > 0 && (
        <div className="ws-notice">
          Unresolved warnings: {warnings.join("; ")}
          <label className="ws-check" style={{ display: "flex", marginTop: 6 }}>
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /> I
            acknowledge exporting with unresolved issues
          </label>
        </div>
      )}

      <div className="ws-export-section">
        {FORMAT_GROUPS.map((g) => (
          <div key={g.title} className="ws-export-group">
            <div className="ws-export-group-label">{g.title}</div>
            <div className="ws-formats">
              {g.formats.map((f) => (
                <label key={f.v} className="ws-check">
                  <input type="checkbox" checked={formats.has(f.v)}
                    onChange={() => toggle(formats, f.v, setFormats)} />
                  {f.label}
                </label>
              ))}
            </div>
          </div>
        ))}
        <div className="ws-export-actions">
          <button className="ws-btn ws-btn-primary" onClick={runExport}
            disabled={formats.size === 0 || (warnings.length > 0 && !ack)}>
            <DownloadIcon aria-hidden="true" /> Export
          </button>
          {progress && <span className="muted">{progress}</span>}
        </div>
      </div>
      {err && <div className="ws-error">{err}</div>}

      {files.length > 0 && (
        <div className="ws-export-files">
          {files.map((f) => (
            <div key={f.name}>
              <a href="#" onClick={(e) => { e.preventDefault(); downloadExport(f.name); }}>
                {f.name}
              </a>{" "}
              <span className="muted">({(f.size / 1024).toFixed(1)} KB)</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ValidationTable({ v }: { v: any }) {
  const rows: [string, string, string][] = [
    ["Rows", fmt(v.row_count?.before), fmt(v.row_count?.after)],
    ["Columns", fmt(v.column_count?.before), fmt(v.column_count?.after)],
    ["Missing values", fmt(v.missing_values?.before), fmt(v.missing_values?.after)],
    ["Duplicate rows", fmt(v.duplicate_rows?.before), fmt(v.duplicate_rows?.after)],
    ["Cells changed", "", fmt(v.cells_changed)],
    ["Rows removed", "", fmt(v.rows_removed)],
  ];
  return (
    <table style={{ marginTop: 8, width: "auto" }}>
      <thead><tr><th></th><th>Before</th><th>After</th></tr></thead>
      <tbody>
        {rows.map(([label, b, a]) => (
          <tr key={label}><td className="muted">{label}</td><td>{b}</td><td>{a}</td></tr>
        ))}
      </tbody>
    </table>
  );
}

function fmt(x: unknown): string {
  return x == null ? "" : String(x);
}
