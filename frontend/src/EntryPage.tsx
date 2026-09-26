import { useRef, useState } from "react";
import {
  CheckCircle2Icon,
  ChevronRightIcon,
  ClockIcon,
  FileOutputIcon,
  FileSpreadsheetIcon,
  FileUpIcon,
  Loader2Icon,
  LockIcon,
  SearchIcon,
  SparklesIcon,
} from "lucide-react";

export interface EntryPageProps {
  onFile: (f: File) => void;
  busy: boolean;
  progress: string;
  error: string | null;
  fileName: string | null;
  expiresIn: number | null;
}

export function EntryHeader({ expiresIn }: { expiresIn: number | null }) {
  return (
    <header className="entry-header">
      <div className="entry-inner entry-header-inner">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>
          <span className="brand-name">Quick Data Cleaner</span>
        </div>
        {expiresIn !== null && (
          <span className="session-pill">
            <ClockIcon aria-hidden="true" />
            Session expires in {Math.floor(expiresIn / 60)}:{String(expiresIn % 60).padStart(2, "0")}
          </span>
        )}
      </div>
    </header>
  );
}

export default function EntryPage({ onFile, busy, progress, error, fileName, expiresIn }: EntryPageProps) {
  return (
    <div className="entry-page">
      <div className="grid-decor grid-tr" aria-hidden="true" />
      <div className="grid-decor grid-bl" aria-hidden="true" />

      <EntryHeader expiresIn={expiresIn} />

      <main className="entry-inner hero">
        <section className="intro">
          <span className="accent-line" aria-hidden="true" />
          <h1 className="intro-headline">
            Clean messy datasets<br />
            without rewriting<br />
            the same scripts.
          </h1>
          <p className="intro-lede">
            Review every issue. Preview every change.<br />
            Export a reproducible workflow.
          </p>
          <WorkflowSteps />
          <p className="workflow-text">Upload → Review → Clean → Validate → Export</p>
        </section>

        <UploadCard onFile={onFile} busy={busy} progress={progress} error={error} fileName={fileName} />
      </main>
    </div>
  );
}

const STEPS = [
  { icon: FileUpIcon, label: "Upload" },
  { icon: SearchIcon, label: "Review" },
  { icon: SparklesIcon, label: "Clean" },
  { icon: CheckCircle2Icon, label: "Validate" },
  { icon: FileOutputIcon, label: "Export" },
];

function WorkflowSteps() {
  return (
    <ol className="workflow-steps" aria-label="Workflow">
      {STEPS.map(({ icon: Icon, label }) => (
        <li key={label} className="workflow-step">
          <span className="step-circle"><Icon aria-hidden="true" /></span>
          <span className="step-label">{label}</span>
        </li>
      )).flatMap((el, i) =>
        i === 0 ? [el] : [
          <li key={`sep-${i}`} className="step-sep" aria-hidden="true"><ChevronRightIcon className="step-arrow" /></li>,
          el,
        ],
      )}
    </ol>
  );
}

function UploadCard({ onFile, busy, progress, error, fileName }: Omit<EntryPageProps, "expiresIn">) {
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f && !busy) onFile(f);
  };

  return (
    <section className="upload-card" aria-labelledby="upload-title">
      <h2 id="upload-title">Start with a dataset</h2>
      <div
        className={`dz${dragging ? " dz-active" : ""}${busy ? " dz-busy" : ""}`}
        onDragOver={(e) => { e.preventDefault(); if (!busy) setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        aria-describedby="dz-limits"
      >
        {busy ? (
          <div className="dz-progress">
            <Loader2Icon className="spin" aria-hidden="true" />
            {fileName && <div className="dz-filename">{fileName}</div>}
            <div className="dz-progress-text">{progress || "Working…"}</div>
          </div>
        ) : (
          <>
            <div className="dz-icon"><FileSpreadsheetIcon aria-hidden="true" /></div>
            <div className="dz-title">{dragging ? "Drop the file to upload" : "Drop CSV or Excel here"}</div>
            <button type="button" className="choose-btn" onClick={() => inputRef.current?.click()}>
              Choose file
            </button>
            <div className="dz-chips" aria-label="Supported formats">
              <span className="fmt-chip">CSV</span>
              <span className="fmt-chip">XLS</span>
              <span className="fmt-chip">XLSX</span>
            </div>
            <div className="dz-limits" id="dz-limits">Up to 100 MB · 100,000 rows per sheet</div>
          </>
        )}
        <input
          ref={inputRef}
          type="file"
          accept=".csv,.xls,.xlsx,.txt"
          hidden
          disabled={busy}
          onChange={(e) => e.target.files?.[0] && !busy && onFile(e.target.files[0])}
        />
      </div>

      {error && <div className="upload-error" role="alert">{error}</div>}

      <div className="card-foot">
        <div className="assurance">
          <LockIcon aria-hidden="true" />
          Original file remains unchanged
        </div>
        <details className="limits-disclosure">
          <summary>Excel export limitations</summary>
          <p>
            Cleaned sheets are recreated as value-based tables. Formatting, formulas, charts,
            merged cells, and named ranges are not preserved. Legacy .xls files are exported
            as .xlsx.
          </p>
        </details>
      </div>
    </section>
  );
}
