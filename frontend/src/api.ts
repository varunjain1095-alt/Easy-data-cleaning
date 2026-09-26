const TOKEN_KEY = "qdc_session_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token);
}

async function request(path: string, init: RequestInit = {}) {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("X-Session-Token", token);
  const res = await fetch(path, { ...init, headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = body.detail;
    throw new ApiError(
      res.status,
      typeof detail === "object" && detail ? detail.kind : undefined,
      typeof detail === "object" && detail ? detail.detail : detail || res.statusText,
    );
  }
  return res.json();
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public kind: string | undefined,
    message: string,
  ) {
    super(message);
  }
}

export async function createSession(): Promise<{ token: string; expires_at: number }> {
  const data = await request("/api/sessions", { method: "POST" });
  setToken(data.token);
  return data;
}

export async function ensureSession(): Promise<void> {
  const token = getToken();
  if (token) {
    try {
      await request("/api/session");
      return;
    } catch {
      // expired/invalid -> fall through to create
    }
  }
  await createSession();
}

export function upload(file: File): Promise<{ job_id: string; filename: string }> {
  const form = new FormData();
  form.append("file", file);
  return request("/api/upload", { method: "POST", body: form });
}

export function getJob(jobId: string): Promise<JobStatus> {
  return request(`/api/jobs/${jobId}`);
}

export function getProject(): Promise<Project> {
  return request("/api/project");
}

export function getPreview(itemId: string): Promise<Preview> {
  return request(`/api/items/${itemId}/preview`);
}

export function reparseItem(
  itemId: string,
  encoding?: string,
  delimiter?: string,
): Promise<Preview> {
  return request(`/api/items/${itemId}/reparse`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ encoding, delimiter }),
  });
}

export async function pollJob(
  jobId: string,
  onUpdate: (job: JobStatus) => void,
  intervalMs = 400,
): Promise<JobStatus> {
  for (;;) {
    const job = await getJob(jobId);
    onUpdate(job);
    if (job.status === "completed") return job;
    if (job.status === "failed") throw new ApiError(500, job.status, job.error || "Job failed");
    if (job.status === "interrupted")
      throw new ApiError(500, job.status, job.error || "Job was interrupted");
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

// ---- Phase 2: cleaning endpoints ----

export function assess(itemId: string, stage: string): Promise<any> {
  return request(`/api/items/${itemId}/assess/${stage}`);
}

export function getMissingRows(itemId: string, column: string, n = 10): Promise<any> {
  return request(`/api/items/${itemId}/missing-rows?column=${encodeURIComponent(column)}&n=${n}`);
}

export function assessInvalid(itemId: string, column: string, rule: any): Promise<any> {
  return request(`/api/items/${itemId}/assess/invalid`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ column, rule }),
  });
}

export function assessGroupImpute(itemId: string, column: string, groupBy: string[]): Promise<any> {
  return request(`/api/items/${itemId}/assess/group-impute`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ column, group_by: groupBy }),
  });
}

export function previewOp(itemId: string, op: OpRequest): Promise<OpPreview> {
  return request(`/api/items/${itemId}/ops/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(op),
  });
}

export function applyOp(itemId: string, op: OpRequest): Promise<OpApplyResult> {
  return request(`/api/items/${itemId}/ops`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(op),
  });
}

export function undo(itemId: string): Promise<any> {
  return request(`/api/items/${itemId}/undo`, { method: "POST" });
}

export function redo(itemId: string): Promise<any> {
  return request(`/api/items/${itemId}/redo`, { method: "POST" });
}

export function getHistory(itemId: string): Promise<{ ops: any[]; pointer: number; can_undo: boolean; can_redo: boolean }> {
  return request(`/api/items/${itemId}/history`);
}

export function setStage(itemId: string, stage: string, state: string): Promise<any> {
  return request(`/api/items/${itemId}/stage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ stage, state }),
  });
}

export function dupEstimate(itemId: string, cfg: DupConfig): Promise<any> {
  return request(`/api/items/${itemId}/duplicates/estimate`, post(cfg));
}

export function dupAnalyze(itemId: string, cfg: DupConfig): Promise<{ job_id: string }> {
  return request(`/api/items/${itemId}/duplicates/analyze`, post(cfg));
}

export function dupResult(itemId: string): Promise<any> {
  return request(`/api/items/${itemId}/duplicates/result`);
}

export function dupResolve(itemId: string, decisions: DupDecision[]): Promise<any> {
  return request(`/api/items/${itemId}/duplicates/resolve`, post({ decisions }));
}

export function assessUnits(itemId: string, column?: string): Promise<any> {
  return request(`/api/items/${itemId}/assess/units${column ? `?column=${encodeURIComponent(column)}` : ""}`);
}

export function patternPresets(itemId: string): Promise<{ presets: string[] }> {
  return request(`/api/items/${itemId}/assess/pattern-presets`);
}

export function assessPattern(itemId: string, column: string, pattern: string): Promise<any> {
  return request(`/api/items/${itemId}/assess/patterns?column=${encodeURIComponent(column)}&pattern=${encodeURIComponent(pattern)}`);
}

export function assessKeys(itemId: string, columns: string[]): Promise<any> {
  return request(`/api/items/${itemId}/assess/keys?columns=${columns.map(encodeURIComponent).join(",")}`);
}

export function declareKey(itemId: string, columns: string[] | null): Promise<any> {
  return request(`/api/items/${itemId}/keys/declare`, post({ columns }));
}

export function getKeyDeclaration(itemId: string): Promise<any> {
  return request(`/api/items/${itemId}/keys/declaration`);
}

function post(body: unknown): RequestInit {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

export interface OpRequest {
  op_type: string;
  stage: string;
  params: Record<string, unknown>;
  target_columns?: string[];
}

export interface OpPreview {
  affected_rows: number;
  row_count_before: number;
  row_count_after: number;
  columns_before: string[];
  columns_after: string[];
  before_sample: Record<string, unknown>[];
  after_sample: Record<string, unknown>[];
}

export interface OpApplyResult {
  applied: unknown;
  invalidated: string[];
  row_count: number;
  column_count: number;
  can_undo: boolean;
  can_redo: boolean;
}

export interface DupConfig {
  columns: string[];
  blocking_columns: string[];
  threshold: number;
}

// ---- Phase 3A: export + validation ----

export function startExport(formats: string[], excludeItems: string[], acknowledgeWarnings: boolean): Promise<{ job_id: string }> {
  return request("/api/export", post({ formats, exclude_items: excludeItems, acknowledge_warnings: acknowledgeWarnings }));
}

export function exportWarnings(): Promise<{ warnings: string[] }> {
  return request("/api/export/warnings");
}

export function listExports(): Promise<{ files: { name: string; size: number; kind: string }[] }> {
  return request("/api/exports");
}

export async function downloadExport(filename: string): Promise<void> {
  const token = getToken();
  const res = await fetch(`/api/exports/${encodeURIComponent(filename)}`, {
    headers: token ? { "X-Session-Token": token } : {},
  });
  if (!res.ok) throw new ApiError(res.status, undefined, "Download failed");
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function runValidation(itemId: string): Promise<{ job_id: string }> {
  return request(`/api/items/${itemId}/validate`, { method: "POST" });
}

export function getValidation(itemId: string): Promise<any> {
  return request(`/api/items/${itemId}/validation`);
}

export function sessionInfo(): Promise<{ token: string; expires_at: number; created_at: number }> {
  return request("/api/session");
}

// ---- Phase 3B: outliers ----

export function declareOutcome(itemId: string, body: { column: string | null; meaning?: string; positive_class?: string }): Promise<any> {
  return request(`/api/items/${itemId}/outcome`, post(body));
}

export function getOutcome(itemId: string): Promise<{ outcome: any }> {
  return request(`/api/items/${itemId}/outcome`);
}

export function screenOutliers(itemId: string, col: string, method: string, params: Record<string, number> = {}): Promise<any> {
  const q = new URLSearchParams({ method, ...Object.fromEntries(Object.entries(params).map(([k, v]) => [k, String(v)])) });
  return request(`/api/items/${itemId}/outliers/screen/${encodeURIComponent(col)}?${q}`);
}

export function markOutlier(itemId: string, column: string, action: string): Promise<any> {
  return request(`/api/items/${itemId}/outliers/mark`, post({ column, action }));
}

export function contextCandidates(itemId: string, col: string): Promise<{ candidates: any[]; outcome: any }> {
  return request(`/api/items/${itemId}/outliers/context/${encodeURIComponent(col)}`);
}

export function bivariate(itemId: string, column: string, context: string): Promise<any> {
  return request(`/api/items/${itemId}/outliers/bivariate`, post({ column, context }));
}

export function classifyOutlier(itemId: string, column: string, rowId: number, classification: string): Promise<any> {
  return request(`/api/items/${itemId}/outliers/classify`, post({ column, row_id: rowId, classification }));
}

export function treatOutliers(itemId: string, body: Record<string, unknown>): Promise<any> {
  return request(`/api/items/${itemId}/outliers/treat`, post(body));
}

export function outlierState(itemId: string): Promise<any> {
  return request(`/api/items/${itemId}/outliers/state`);
}

export interface DupDecision {
  row_ids: number[];
  action: string;
  keep_row_id?: number;
  field_values?: Record<string, unknown>;
}

export interface JobStatus {
  id: string;
  kind: string;
  status: "queued" | "running" | "completed" | "failed" | "interrupted";
  progress: { message?: string; done?: number; total?: number | null };
  result: unknown;
  error: string | null;
}

export interface ProjectItem {
  item_id: string;
  name: string;
  item_type: string;
  status: string;
  rejected_reason: string | null;
  row_count: number | null;
  column_count: number | null;
  stage_states: Record<string, string>;
}

export interface Project {
  project_id: string;
  kind: string;
  source_filename: string;
  items: ProjectItem[];
  detected?: { csv_settings?: { encoding: string; delimiter: string }; xls_notice?: string };
  rejected_items?: string[];
}

export interface Preview {
  item_id: string;
  name: string;
  row_count: number;
  column_count: number;
  columns: { name: string; dtype: string }[];
  rows: Record<string, unknown>[];
}
