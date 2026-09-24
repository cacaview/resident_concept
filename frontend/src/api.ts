// API client + typed response shapes for the Resident backend.
// Use the page's origin. The dev server proxies /api to residentd.
export const API_BASE = "";

// ---------- /api/health ----------

export interface HealthInfo {
  ok: boolean;
  db: string;
  events: number;
  scheduler_running: boolean;
}

// ---------- /api/chat ----------

export interface ChatReply {
  reply: string;
  user_event_id: string;
  resident_event_id: string;
}

// ---------- /api/wake ----------

export interface WakeResult {
  run_id: string;
  trigger: string;
  route: string;
  decision: string;
  result: "noop" | "thought";
  event_id: string;
  reason?: string;
  text?: string;
  thread_id?: string;
}

// ---------- /api/life ----------

export interface LifeEvent {
  id: string;
  type: string;
  text: string;
  created_at: string;
  visibility: string;
  thread_id?: string;
  route?: string;
}

export interface WakeSummary {
  run_id: string;
  route: string;
  result: "noop" | "persisted";
  created_at: string;
}

export interface LifeStats {
  events: number;
  wakes: number;
  noops: number;
  route_distribution: Record<string, number>;
}

export interface LifeResponse {
  events: LifeEvent[];
  wakes: WakeSummary[];
  stats: LifeStats;
}

// ---------- /api/mind ----------

export interface ThreadInfo {
  thread_id: string;
  title: string;
  state: string;
  last_event_id: string;
  last_event_at: string;
}

/**
 * active_threads / dormant_threads entries are full thread objects per the
 * contract; we also tolerate plain thread-id strings defensively.
 */
export type ThreadEntry = ThreadInfo | string;

export interface OpenQuestion {
  question_id: string;
  text: string;
  thread_id: string;
}

export interface RecentThought {
  id: string;
  text: string;
  created_at: string;
  visibility: string;
}

export interface MindResponse {
  mind_brief: string;
  threads: ThreadInfo[];
  active_threads: ThreadEntry[];
  dormant_threads: ThreadEntry[];
  open_questions: OpenQuestion[];
  recent_thoughts: RecentThought[];
}

// ---------- /api/reentry ----------

export type ReentryDecision = "share_now" | "mention_later" | "nothing_to_share";

export interface ReentryCandidate {
  claim: string;
  event_ids: string[];
  type: string;
  created_at: string;
  strength: number | string;
}

export interface ReentryResponse {
  decision: ReentryDecision;
  candidates: ReentryCandidate[];
  baseline: string | null;
  evaluated_count: number;
  evaluated_event_id: string;
  // The contract allows additional fields; they are intentionally not surfaced.
  [key: string]: unknown;
}

// ---------- /api/events ----------

export interface StoredEvent {
  id: string;
  type: string;
  actor: string;
  visibility: string;
  content: Record<string, unknown>;
  links: Record<string, unknown>;
  created_at: string;
}

// ---------- /api/monitor/* (v0.2 Step 5 — strictly read-only) ----------

export interface MonitorEvent {
  id: string;
  seq: number | null;
  type: string;
  visibility: string;
  created_at: string;
  text: string;
  thread_id?: string | null;
  origin?: string | null;
  links: Record<string, number>;
  depth?: number;
}

export interface MonitorTimeline {
  events: MonitorEvent[];
  counts: Record<string, number>;
}

export interface MonitorProvenance {
  root?: string;
  error?: string;
  nodes: MonitorEvent[];
  edges: { from: string; to: string; kind: string }[];
}

export interface MonitorThread {
  thread_id: string;
  title: string;
  origin: string | null;
  created_at: string | null;
  state: string;
  last_event_at: string | null;
  n_events: number;
  age_days: number | null;
  revisits: { at: string; reason: string }[];
}

export interface MonitorThreads {
  threads: MonitorThread[];
  summary: { total: number; active: number; dormant: number };
}

export interface MonitorQuestion {
  question_id: string;
  kind: string | null;
  topic: string | null;
  text: string | null;
  created_at: string;
  last_touched_at: string;
  revisits: number;
  status: string;
  evidence: string[];
  evidence_resolved: { id: string; type: string }[];
}

export interface MonitorQuestions {
  questions: MonitorQuestion[];
  summary: { total: number; open: number; dormant: number; revisits: number };
}

export interface MonitorContinuityWindow {
  detected_at: string;
  gap_s: number | null;
  since: { id: string; at: string | null; text: string | null };
  return: { id: string | null; at: string | null; text: string | null };
  candidates: { cls: string | null; quote: string | null; at: string; evidence: string[] }[];
  decision: "selected" | "noop" | null;
  selected_classes: string[];
}

export interface MonitorContinuity {
  last_user_seen: string | null;
  windows: MonitorContinuityWindow[];
  summary: { absences: number; selected: number; noop: number };
}

export interface MonitorTraceUsed {
  id: string;
  type: string;
  origin: string | null;
  age_days: number | null;
  text: string;
}

export interface MonitorTraceThought {
  id: string;
  created_at: string;
  route: string | null;
  origin: string | null;
  thread_id?: string | null;
  text: string;
  used: MonitorTraceUsed[];
}

export interface MonitorRetrieval {
  thoughts: MonitorTraceThought[];
  shadow: {
    path: string;
    records: {
      route: string | null;
      query: string | null;
      legacy_top: [string, number][];
      semantic_top: [string, number][];
      overlap: number | null;
      top1_differs: boolean | null;
    }[];
  } | null;
  note: string;
}

// ---------- /api/agency (ADR-0024: the ask → authorization channel) ----------

/** One live `can_use_tool` ask awaiting the owner's answer. */
export interface PermissionAsk {
  ask_id: string;
  action_id: string;
  tool: string;
  argument_digest: string;
  created_at: string;
  expires_at: string;
  age_s: number;
}

export interface PermissionAsksResponse {
  enabled: boolean;
  asks: PermissionAsk[];
}

export interface PermissionAnswerResult {
  ask_id: string;
  answer: string;
}

// ---------- client ----------

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(API_BASE + path, init);
  if (!res.ok) throw new Error(`HTTP ${res.status} ${path}`);
  return (await res.json()) as T;
}

const JSON_HEADERS: Record<string, string> = { "Content-Type": "application/json" };

export const api = {
  health(): Promise<HealthInfo> {
    return request<HealthInfo>("/api/health");
  },
  life(limit = 40): Promise<LifeResponse> {
    return request<LifeResponse>(`/api/life?limit=${limit}`);
  },
  events(params: { limit?: number; type?: string; order?: "asc" | "desc" }): Promise<StoredEvent[]> {
    const q = new URLSearchParams();
    if (params.limit !== undefined) q.set("limit", String(params.limit));
    if (params.type !== undefined) q.set("type", params.type);
    if (params.order !== undefined) q.set("order", params.order);
    return request<StoredEvent[]>(`/api/events?${q.toString()}`);
  },
  mind(): Promise<MindResponse> {
    return request<MindResponse>("/api/mind");
  },
  chat(message: string): Promise<ChatReply> {
    return request<ChatReply>("/api/chat", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ message }),
    });
  },
  wake(trigger?: string): Promise<WakeResult> {
    return request<WakeResult>("/api/wake", {
      method: "POST",
      headers: trigger ? JSON_HEADERS : undefined,
      body: trigger ? JSON.stringify({ trigger }) : undefined,
    });
  },
  reentry(since: string | null = null): Promise<ReentryResponse> {
    return request<ReentryResponse>("/api/reentry", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ since }),
    });
  },
  // ---- monitor (GET only — the microscope never touches the petri dish) ----
  monitorTimeline(limit = 200, type?: string): Promise<MonitorTimeline> {
    const q = new URLSearchParams({ limit: String(limit) });
    if (type) q.set("type", type);
    return request<MonitorTimeline>(`/api/monitor/timeline?${q.toString()}`);
  },
  monitorProvenance(eventId: string, depth = 8): Promise<MonitorProvenance> {
    return request<MonitorProvenance>(`/api/monitor/provenance/${eventId}?depth=${depth}`);
  },
  monitorThreads(): Promise<MonitorThreads> {
    return request<MonitorThreads>("/api/monitor/threads");
  },
  monitorQuestions(): Promise<MonitorQuestions> {
    return request<MonitorQuestions>("/api/monitor/questions");
  },
  monitorContinuity(): Promise<MonitorContinuity> {
    return request<MonitorContinuity>("/api/monitor/continuity");
  },
  monitorRetrieval(limit = 40): Promise<MonitorRetrieval> {
    return request<MonitorRetrieval>(`/api/monitor/retrieval?limit=${limit}`);
  },
  // ---- agency (ADR-0024: the ask → authorization channel, owner-side) ----
  permissionAsks(): Promise<PermissionAsksResponse> {
    return request<PermissionAsksResponse>("/api/agency/permission-asks");
  },
  answerPermissionAsk(askId: string, answer: "allow" | "deny"): Promise<PermissionAnswerResult> {
    return request<PermissionAnswerResult>("/api/agency/permission-asks/answer", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ ask_id: askId, answer }),
    });
  },
};

// ---------- small helpers ----------

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
