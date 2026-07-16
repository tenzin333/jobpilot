// Typed client for the FastAPI JSON API (app/web/api.py).

export type Tone = "neutral" | "success" | "warning" | "danger" | "accent";

export interface StatusState {
  app_id: number;
  status: string;
  label: string;
  elapsed: number;
  running: boolean;
  polling: boolean;
  tone: Tone;
}

export interface DashboardStats {
  jobs: number;
  ranked: number;
  tailored: number;
  queued: number;
  submitted: number;
  needs_human: number;
  failed: number;
}

export interface PipelineSnapshot {
  running: boolean;
  stage: string;
  stats: Record<string, number>;
  logs: string[];
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface DashboardData {
  stats: DashboardStats;
  settings: {
    dry_run: boolean;
    submit_kill_switch: boolean;
    daily_submit_cap: number;
    match_threshold: number;
  };
  profile_configured: boolean;
  pipeline: PipelineSnapshot;
}

export interface JobApplication {
  id: number;
  status: string;
  match_score: number | null;
  score_rationale: string;
  state: StatusState;
  can_apply: boolean;
}

export interface JobRow {
  id: number;
  title: string;
  company: string;
  location: string;
  source: string;
  remote: boolean;
  apply_url: string;
  application: JobApplication | null;
}

export interface SourceInfo {
  name: string;
  kind: "ats" | "search" | "career";
  enabled: boolean;
  ready: boolean;
  detail: string;
}

export interface JobsData {
  running: boolean;
  jobs: JobRow[];
  sources: SourceInfo[];
}

export interface SetupProfile {
  full_name: string;
  email: string;
  phone: string;
  skills: number;
  experience: number;
  education: number;
  resume_filename: string;
}

export interface SetupPreferences {
  desired_roles: string[];
  locations: string[];
  remote_preference: string;
  min_salary: number | null;
  salary_currency: string;
  require_sponsorship: boolean;
  work_authorization: string;
  greenhouse_companies: string[];
  lever_companies: string[];
}

export interface SetupData {
  profile: SetupProfile | null;
  preferences: SetupPreferences;
  answer_bank: Record<string, string>;
  remote_options: string[];
}

export interface SetupPayload {
  preferences: SetupPreferences;
  answer_bank: Record<string, string>;
}

export interface SettingsData {
  dry_run: boolean;
  submit_kill_switch: boolean;
  scheduler_enabled: boolean;
  daily_submit_cap: number;
  match_threshold: number;
  cycle_interval_minutes: number;
}

export interface ApplicationRow {
  id: number;
  title: string;
  company: string;
  location: string;
  source: string;
  apply_url: string;
  status: string;
  match_score: number | null;
  score_rationale: string;
  error: string;
  has_resume: boolean;
  has_cover_letter: boolean;
  submitted_at: string | null;
  state: StatusState;
  can_retry: boolean;
}

export interface InterventionItem {
  id: number;
  title: string;
  company: string;
  apply_url: string;
  source: string;
  ats_type: string;
  reason: string;
  has_resume: boolean;
}

export interface AssistSnapshot {
  stage?: "queued" | "opening" | "live" | "done" | "error";
  queue_pos?: number;
  filled?: number;
  missed?: string[];
  done?: boolean;
  error?: string;
}

export interface SummaryData {
  day: string;
  discovered: number;
  ranked: number;
  tailored: number;
  submitted: number;
  failed_today: number;
  needs_human_today: number;
  needs_human_open: number;
  top_matches: { score: number | null; company: string; title: string; status: string }[];
  email_configured: boolean;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  dashboard: () => request<DashboardData>("/dashboard"),
  pipelineStatus: () => request<PipelineSnapshot>("/pipeline/status"),
  jobs: () => request<JobsData>("/jobs"),
  discover: () => request<{ ok: boolean; running: boolean }>("/jobs/discover", { method: "POST" }),
  toggleSource: (name: string, enabled: boolean) =>
    request<{ ok: boolean; sources: SourceInfo[] }>(`/sources/${name}`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
  clearJobs: () => request<{ ok: boolean }>("/jobs/clear", { method: "POST" }),
  rank: () => request<{ ok: boolean }>("/applications/rank", { method: "POST" }),
  apply: (appId: number) => request<StatusState>(`/matches/${appId}/apply`, { method: "POST" }),
  applyStatus: (appId: number) => request<StatusState>(`/matches/${appId}/status`),
  retry: (appId: number) => request<StatusState>(`/matches/${appId}/retry`, { method: "POST" }),

  getSetup: () => request<SetupData>("/setup"),
  saveSetup: (payload: SetupPayload) =>
    request<{ ok: boolean }>("/setup", { method: "POST", body: JSON.stringify(payload) }),
  uploadResume: async (file: File) => {
    const form = new FormData();
    form.append("resume", file);
    // No Content-Type header: the browser sets the multipart boundary.
    const res = await fetch("/api/setup/resume", { method: "POST", body: form });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = (await res.json()).detail ?? detail;
      } catch {
        /* non-JSON error body */
      }
      throw new Error(detail || `Upload failed (${res.status})`);
    }
    return res.json() as Promise<{ profile: SetupProfile | null; answer_bank: Record<string, string> }>;
  },

  getSettings: () => request<SettingsData>("/settings"),
  saveSettings: (payload: SettingsData) =>
    request<SettingsData>("/settings", { method: "POST", body: JSON.stringify(payload) }),

  // Pipeline
  pipelineRun: () =>
    request<{ ok: boolean; started: boolean; running: boolean }>("/pipeline/run", { method: "POST" }),

  // Applications
  applications: () => request<{ applications: ApplicationRow[] }>("/applications"),
  tailor: () => request<{ ok: boolean; tailored: number }>("/applications/tailor", { method: "POST" }),
  submit: () => request<{ ok: boolean; submitted: number }>("/applications/submit", { method: "POST" }),
  artifactUrl: (appId: number, artifact: "resume" | "cover_letter") =>
    `/api/applications/${appId}/${artifact}`,

  // Intervention
  intervention: () => request<{ items: InterventionItem[] }>("/intervention"),
  interventionDone: (appId: number) =>
    request<{ ok: boolean }>(`/intervention/${appId}/done`, { method: "POST" }),

  // Assist co-browse
  assistStart: (appId: number) =>
    request<AssistSnapshot>(`/intervention/${appId}/assist`, { method: "POST" }),
  assistStatus: (appId: number) =>
    request<AssistSnapshot>(`/intervention/${appId}/assist-status`),
  assistWsUrl: (appId: number) => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${window.location.host}/ws/assist/${appId}`;
  },

  // Summary
  summary: () => request<SummaryData>("/summary"),
  emailSummary: () => request<{ ok: boolean; sent: boolean }>("/summary/email", { method: "POST" }),
};
