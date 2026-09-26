import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  ensureSession,
  getPreview,
  getProject,
  pollJob,
  Preview,
  Project,
  ProjectItem,
  reparseItem,
  upload,
} from "./api";
import { ArrowLeftIcon, FileTextIcon, Table2Icon, UploadIcon } from "lucide-react";
import EntryPage, { EntryHeader } from "./EntryPage";
import CleaningPanel from "./CleaningPanel";
import ExportPanel from "./ExportPanel";
import { sessionInfo } from "./api";

type Phase = "upload" | "processing" | "checklist";

export default function App() {
  const [phase, setPhase] = useState<Phase>("upload");
  const [project, setProject] = useState<Project | null>(null);
  const [dataVersion, setDataVersion] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [progress, setProgress] = useState("");
  const [fileName, setFileName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [expiresAt, setExpiresAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    ensureSession()
      .then(() => getProject().then(setProject).catch(() => null))
      .then(() => setReady(true))
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    if (project && phase === "upload") setPhase("checklist");
  }, [project, phase]);

  // Session-expiry countdown (1-hour sliding window; server sync + 1s local tick).
  useEffect(() => {
    const sync = () =>
      sessionInfo()
        .then((s) => setExpiresAt(s.expires_at))
        .catch(() => setExpiresAt(null));
    sync();
    const poll = setInterval(sync, 30_000);
    const tick = setInterval(() => setNow(Date.now()), 1_000);
    return () => { clearInterval(poll); clearInterval(tick); };
  }, [ready]);

  const expiresIn =
    expiresAt !== null ? Math.max(0, Math.floor(expiresAt - now / 1000)) : null;

  const onFile = useCallback(async (file: File) => {
    setError(null);
    setFileName(file.name);
    setPhase("processing");
    setProgress("Uploading…");
    try {
      const { job_id } = await upload(file);
      const job = await pollJob(job_id, (j) => {
        const p = j.progress;
        setProgress(
          p.total ? `${p.message || "Working"} (${p.done}/${p.total})` : p.message || "Working…",
        );
      });
      setProject(job.result as Project);
      setPhase("checklist");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      setPhase("upload");
    }
  }, []);

  if (!ready) return <div className="app">Loading…</div>;

  if (phase !== "checklist") {
    return (
      <EntryPage
        onFile={onFile}
        busy={phase === "processing"}
        progress={progress}
        error={error}
        fileName={fileName}
        expiresIn={expiresIn}
      />
    );
  }

  const selectedItem = project?.items.find((i) => i.item_id === selected) ?? null;

  return (
    <div className="ws">
      <EntryHeader expiresIn={expiresIn} />
      {error && (
        <div className="entry-inner"><div className="ws-error" role="alert">{error}</div></div>
      )}

      {project && (
        <div className="entry-inner workspace-layout">
          <aside className="ws-sidebar">
            <Checklist
              project={project}
              selected={selected}
              onSelect={setSelected}
              onReset={() => {
                setProject(null);
                setSelected(null);
                setPhase("upload");
              }}
            />
          </aside>
          <main className="ws-main">
            {selectedItem ? (
              <>
                <PreviewCard item={selectedItem} projectKind={project.kind} detected={project.detected} refresh={dataVersion} />
                <CleaningPanel
                  item={selectedItem}
                  refresh={dataVersion}
                  onChanged={() => { setDataVersion((v) => v + 1); getProject().then(setProject); }}
                />
              </>
            ) : (
              <div className="ws-card ws-empty">
                <div className="ws-empty-cue" aria-hidden="true"><ArrowLeftIcon /></div>
                <div className="ws-empty-icon" aria-hidden="true"><Table2Icon /></div>
                <h2>Select a dataset</h2>
                <p>Choose a sheet or table from the project navigator to preview and clean it.</p>
              </div>
            )}
            <ExportPanel project={project} />
          </main>
        </div>
      )}
    </div>
  );
}

function Checklist({
  project,
  selected,
  onSelect,
  onReset,
}: {
  project: Project;
  selected: string | null;
  onSelect: (id: string | null) => void;
  onReset: () => void;
}) {
  return (
    <div className="ws-card ws-nav">
      <h2 className="ws-nav-title" title={project.source_filename}>
        <FileTextIcon aria-hidden="true" />
        <span>{project.source_filename}</span>
      </h2>
      {project.detected?.xls_notice && <div className="ws-notice">{project.detected.xls_notice}</div>}
      {project.rejected_items && project.rejected_items.length > 0 && (
        <div className="ws-error">
          Rejected: {project.rejected_items.join(", ")} (see reasons below)
        </div>
      )}
      <ul className="checklist">
        {project.items.map((item) => (
          <li
            key={item.item_id}
            className={`${selected === item.item_id ? "selected" : ""} ${item.status === "rejected" ? "rejected" : ""}`}
            onClick={() => item.status !== "rejected" && onSelect(item.item_id)}
          >
            <strong>{item.name}</strong>
            <span className={`badge ${item.status}`}>{item.status.replace(/_/g, " ")}</span>
            <span className="muted">
              {item.row_count != null && `${item.row_count.toLocaleString()} rows · ${item.column_count} cols`}
              {item.rejected_reason && `, ${item.rejected_reason}`}
            </span>
          </li>
        ))}
      </ul>
      <button className="ws-btn ws-btn-outline ws-upload-alt" onClick={onReset}>
        <UploadIcon aria-hidden="true" /> Upload a different file
      </button>
    </div>
  );
}

function PreviewCard({
  item,
  projectKind,
  detected,
  refresh = 0,
}: {
  item: ProjectItem;
  projectKind: string;
  detected?: Project["detected"];
  refresh?: number;
}) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [encoding, setEncoding] = useState(detected?.csv_settings?.encoding ?? "");
  const [delimiter, setDelimiter] = useState(detected?.csv_settings?.delimiter ?? "");
  const [showReparse, setShowReparse] = useState(false);

  const load = useCallback(() => {
    setError(null);
    getPreview(item.item_id)
      .then(setPreview)
      .catch((e) => setError(e.message));
  }, [item.item_id, refresh]);

  useEffect(load, [load]);

  const applyCsvSettings = async () => {
    try {
      setPreview(await reparseItem(item.item_id, encoding || undefined, delimiter || undefined));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };

  if (error) return <div className="ws-card"><div className="ws-error">{error}</div></div>;
  if (!preview) return <div className="ws-card">Loading preview…</div>;

  return (
    <div className="ws-card">
      <div className="ws-card-head">
        <div>
          <h2>Preview: {preview.name}</h2>
          <p className="muted">
            {preview.row_count.toLocaleString()} rows · {preview.column_count} columns
          </p>
        </div>
        {projectKind === "csv" && (
          <button className="ws-btn ws-btn-ghost" onClick={() => setShowReparse(!showReparse)}
            aria-expanded={showReparse}>
            {showReparse ? "▾" : "▸"} Columns look wrong or text garbled?
          </button>
        )}
      </div>
      {projectKind === "csv" && showReparse && (
        <div className="reparse-box">
          <p className="muted" style={{ margin: "0 0 8px", fontSize: "0.85rem" }}>
            The file was auto-read with the settings below. If the preview above looks wrong,
            columns merged into one, or characters like é showing as Ã©, correct them and re-read the file.
          </p>
          <div className="row">
            <div>
              <label>Text encoding (detected: {detected?.csv_settings?.encoding})</label>
              <input type="text" value={encoding} onChange={(e) => setEncoding(e.target.value)} />
            </div>
            <div>
              <label>Column separator (detected: {JSON.stringify(detected?.csv_settings?.delimiter)})</label>
              <select value={delimiter} onChange={(e) => setDelimiter(e.target.value)}>
                {[",", ";", "\t", "|"].map((d) => (
                  <option key={d} value={d}>{JSON.stringify(d)}</option>
                ))}
              </select>
            </div>
            <div style={{ alignSelf: "flex-end" }}>
              <button className="ws-btn ws-btn-primary" onClick={applyCsvSettings}>Re-read file</button>
            </div>
          </div>
        </div>
      )}
      <div className="table-wrap ws-table">
        <table>
          <thead>
            <tr>
              {preview.columns.map((c) => (
                <th key={c.name}>
                  {c.name}
                  <div className="muted" style={{ fontWeight: "normal" }}>{c.dtype}</div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {preview.rows.map((row, i) => (
              <tr key={i}>
                {preview.columns.map((c) => (
                  <td key={c.name}>{row[c.name] == null ? "" : String(row[c.name])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
