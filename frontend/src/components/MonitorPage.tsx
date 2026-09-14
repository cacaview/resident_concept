import { useCallback, useEffect, useState } from "react";
import type { CSSProperties } from "react";
import type {
  MonitorContinuity,
  MonitorEvent,
  MonitorProvenance,
  MonitorQuestions,
  MonitorRetrieval,
  MonitorThreads,
} from "../api";
import { api, fmtTime } from "../api";

/**
 * The Self Monitor (v0.2 Step 5, ADR-0013) — 显微镜，不是 Resident 的脸.
 *
 * Everything here is a READ-ONLY projection of the event store: GET only, no
 * chat box, no wake/consolidation buttons, nothing that could shake the
 * petri dish. It shows verifiable facts only — no mood/curiosity numbers;
 * where a "score" would go, the events themselves are shown instead.
 */

const POLL_MS = 5000;

const FILTERS: { label: string; prefix: string | undefined }[] = [
  { label: "全部", prefix: undefined },
  { label: "thought", prefix: "thought" },
  { label: "question", prefix: "question" },
  { label: "world", prefix: "world" },
  { label: "impression", prefix: "impression" },
  { label: "memory", prefix: "memory" },
  { label: "absence", prefix: "absence" },
  { label: "reentry", prefix: "reentry" },
  { label: "wake", prefix: "wake" },
  { label: "sleep", prefix: "sleep" },
  { label: "conversation", prefix: "conversation" },
  { label: "thread", prefix: "thread" },
];

const FAMILY_COLOR: Record<string, string> = {
  thought: "#7aa2f7", question: "#bb9af7", world: "#9ece6a",
  impression: "#e0af68", memory: "#e0af68", absence: "#f7768e",
  reentry: "#f7768e", wake: "#6d7484", sleep: "#6d7484",
  conversation: "#7dcfff", thread: "#73daca", telemetry: "#4b5263",
};

function familyOf(type: string): string {
  return type.split(".", 1)[0] ?? type;
}

function badgeStyle(type: string): CSSProperties {
  const c = FAMILY_COLOR[familyOf(type)] ?? "#a9b0bf";
  return { color: c, background: c + "1a", border: `1px solid ${c}44` };
}

function TypeBadge({ type }: { type: string }) {
  return (
    <span className="m-type" style={badgeStyle(type)} title={type}>
      {type}
    </span>
  );
}

// ---------------------------------------------------------------- timeline

function TimelinePanel({
  events,
  counts,
  onSelect,
  selected,
}: {
  events: MonitorEvent[];
  counts: Record<string, number>;
  onSelect: (id: string) => void;
  selected: string | null;
}) {
  const [filter, setFilter] = useState<string | undefined>(undefined);
  const shown = filter ? events.filter((e) => e.type.startsWith(filter)) : events;
  return (
    <section className="card mon-card mon-wide">
      <div className="card-head">
        <h2>
          生命时间线<span className="en">Life Timeline</span>
        </h2>
        <span className="m-counts">
          {counts.total ?? 0} 事件 · {counts.noops ?? 0} 空醒 · {counts.questions ?? 0} 问题 ·{" "}
          {counts.absences ?? 0} 缺席
        </span>
      </div>
      <div className="m-chips">
        {FILTERS.map((f) => (
          <button
            key={f.label}
            className={`m-chip${filter === f.prefix ? " on" : ""}`}
            onClick={() => setFilter(f.prefix)}
          >
            {f.label}
          </button>
        ))}
      </div>
      <div className="m-list">
        {shown.map((e) => (
          <div
            key={e.id}
            className={`m-row${selected === e.id ? " on" : ""}`}
            onClick={() => onSelect(e.id)}
            title="点击查看 provenance 链"
          >
            <span className="m-time">{fmtTime(e.created_at)}</span>
            <TypeBadge type={e.type} />
            {e.visibility === "system" && <span className="m-sys">system</span>}
            {e.text && <span className="m-text">{e.text.slice(0, 90)}</span>}
          </div>
        ))}
      </div>
    </section>
  );
}

// -------------------------------------------------------------- provenance

function ProvenancePanel({ prov }: { prov: MonitorProvenance | null }) {
  if (!prov) {
    return (
      <section className="card mon-card">
        <div className="card-head">
          <h2>
            Provenance 溯源<span className="en">Inspector</span>
          </h2>
        </div>
        <p className="muted empty-p">点击左侧任何事件，沿 caused_by / related_to / recalled 走回它的来处。</p>
      </section>
    );
  }
  if (prov.error) {
    return (
      <section className="card mon-card">
        <div className="card-head">
          <h2>
            Provenance 溯源<span className="en">Inspector</span>
          </h2>
        </div>
        <p className="muted empty-p">{prov.error}</p>
      </section>
    );
  }
  const kindLabel: Record<string, string> = {
    caused_by: "← 由…引起",
    related_to: "← 关联",
    recalled: "← 检索自",
  };
  return (
    <section className="card mon-card">
      <div className="card-head">
        <h2>
          Provenance 溯源<span className="en">Inspector</span>
        </h2>
        <span className="m-counts">{prov.nodes.length} 节点</span>
      </div>
      <div className="m-chain">
        {prov.nodes.map((n) => {
          const ek = edgeTo(prov, n.id);
          const label = (n.depth ?? 0) === 0 ? "◉" : (ek ? kindLabel[ek] ?? "←" : "←");
          return (
            <div key={n.id} className="m-node" style={{ marginLeft: (n.depth ?? 0) * 14 }}>
              <span className="m-edge">{label}</span>
              <TypeBadge type={n.type} />
              <span className="m-time">{fmtTime(n.created_at)}</span>
              {n.text && <span className="m-text">{n.text.slice(0, 70)}</span>}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function edgeTo(prov: MonitorProvenance, id: string): string | undefined {
  return prov.edges.find((e) => e.from === id)?.kind;
}

// ------------------------------------------------------- threads & questions

function ThreadsPanel({ threads }: { threads: MonitorThreads | null }) {
  return (
    <section className="card mon-card">
      <div className="card-head">
        <h2>
          线程<span className="en">Threads</span>
        </h2>
        {threads && (
          <span className="m-counts">
            {threads.summary.active} active · {threads.summary.dormant} dormant
          </span>
        )}
      </div>
      <div className="m-list">
        {(threads?.threads ?? []).map((t) => (
          <div key={t.thread_id} className="m-row col">
            <span className="m-rowline">
              <span className={`m-state ${t.state}`}>{t.state}</span>
              <b>{t.title || t.thread_id}</b>
              {t.origin && <span className="m-origin">{t.origin}</span>}
              <span className="m-dim">{t.n_events} 事件</span>
              {t.age_days !== null && <span className="m-dim">{t.age_days}d</span>}
            </span>
            {t.revisits.length > 0 && (
              <span className="m-sub">
                重新浮现 ×{t.revisits.length}（最近 {fmtTime(t.revisits[t.revisits.length - 1].at)}）
              </span>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

function QuestionsPanel({ questions }: { questions: MonitorQuestions | null }) {
  return (
    <section className="card mon-card">
      <div className="card-head">
        <h2>
          问题<span className="en">Questions</span>
        </h2>
        {questions && (
          <span className="m-counts">
            {questions.summary.open} open · {questions.summary.dormant} dormant ·{" "}
            {questions.summary.revisits} 次重遇
          </span>
        )}
      </div>
      <div className="m-list">
        {(questions?.questions ?? []).map((q) => (
          <div key={q.question_id} className="m-row col">
            <span className="m-rowline">
              <span className={`m-state ${q.status}`}>{q.status}</span>
              {q.kind && <span className="m-origin">{q.kind}</span>}
              {q.topic && <span className="m-dim">{q.topic}</span>}
              <span className="m-dim">重遇 ×{q.revisits}</span>
            </span>
            {q.text && <span className="m-sub">{q.text.slice(0, 110)}</span>}
          </div>
        ))}
        {questions && questions.questions.length === 0 && (
          <p className="muted empty-p">还没有问题被留下来。</p>
        )}
      </div>
    </section>
  );
}

// ------------------------------------------------------------ retrieval trace

function RetrievalPanel({ trace }: { trace: MonitorRetrieval | null }) {
  return (
    <section className="card mon-card mon-wide">
      <div className="card-head">
        <h2>
          检索轨迹<span className="en">Memory / Retrieval Trace</span>
        </h2>
        <span className="m-counts">只读 — 每个思想实际用到的材料</span>
      </div>
      <div className="m-list">
        {(trace?.thoughts ?? []).map((t) => (
          <div key={t.id} className="m-row col">
            <span className="m-rowline">
              <span className="m-time">{fmtTime(t.created_at)}</span>
              {t.route && <span className="m-origin">{t.route}</span>}
              {t.origin && <span className={`m-origin o-${t.origin}`}>{t.origin}</span>}
              <span className="m-text">{t.text.slice(0, 80)}</span>
            </span>
            {t.used.length > 0 && (
              <span className="m-sub">
                用到：{" "}
                {t.used.map((u) => `${u.type}(${u.age_days ?? "?"}d)`).join(" · ")}
              </span>
            )}
          </div>
        ))}
      </div>
      {trace?.shadow && (
        <div className="m-shadow">
          <div className="m-shadow-head">
            semantic shadow（{trace.shadow.records.length} 条）— legacy vs semantic 同题对比
          </div>
          {trace.shadow.records.slice(-6).reverse().map((r, i) => (
            <div key={i} className="m-row col">
              <span className="m-rowline">
                <span className="m-origin">{r.route}</span>
                <span className="m-text">「{r.query}」</span>
                <span className="m-dim">overlap {r.overlap ?? "—"}</span>
                {r.top1_differs && <span className="m-diff">top-1 分歧</span>}
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- continuity

function ContinuityPanel({ continuity }: { continuity: MonitorContinuity | null }) {
  return (
    <section className="card mon-card mon-wide">
      <div className="card-head">
        <h2>
          缺席与回归<span className="en">Continuity</span>
        </h2>
        {continuity && (
          <span className="m-counts">
            {continuity.summary.absences} 次缺席 · {continuity.summary.selected} selected ·{" "}
            {continuity.summary.noop} noop
          </span>
        )}
      </div>
      <div className="m-list">
        {(continuity?.windows ?? []).map((w, i) => (
          <div key={i} className="m-row col m-window">
            <span className="m-rowline">
              <span className="m-time">{fmtTime(w.detected_at)}</span>
              <span className="m-origin">
                缺席 {w.gap_s != null ? Math.round(w.gap_s / 360) / 10 + "h" : "?"}
              </span>
              <span className={`m-state ${w.decision === "selected" ? "activated" : "dormant"}`}>
                {w.decision ?? "—"}
              </span>
              {w.selected_classes.map((c) => (
                <span key={c} className="m-origin">{c}</span>
              ))}
            </span>
            <span className="m-sub">
              离开：「{(w.since.text ?? "").slice(0, 40)}」→ 回来：「{(w.return.text ?? "").slice(0, 40)}」
            </span>
            {w.candidates.length > 0 && (
              <span className="m-sub">
                候选：{" "}
                {w.candidates.map((c, j) => (
                  <span key={j} className="m-cand">
                    <i className={`m-candcls c-${c.cls}`}>{c.cls}</i> {(c.quote ?? "").slice(0, 46)}
                  </span>
                ))}
              </span>
            )}
          </div>
        ))}
        {continuity && continuity.windows.length === 0 && (
          <p className="muted empty-p">还没有被记录的缺席。连续性层默认关闭（RESIDENT_CONTINUITY=on）。</p>
        )}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------- page

export function MonitorPage({ online }: { online: boolean | null }) {
  const [timeline, setTimeline] = useState<import("../api").MonitorTimeline | null>(null);
  const [prov, setProv] = useState<MonitorProvenance | null>(null);
  const [threads, setThreads] = useState<MonitorThreads | null>(null);
  const [questions, setQuestions] = useState<MonitorQuestions | null>(null);
  const [continuity, setContinuity] = useState<MonitorContinuity | null>(null);
  const [trace, setTrace] = useState<MonitorRetrieval | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [t, th, q, c, r] = await Promise.all([
        api.monitorTimeline(200),
        api.monitorThreads(),
        api.monitorQuestions(),
        api.monitorContinuity(),
        api.monitorRetrieval(40),
      ]);
      setTimeline(t);
      setThreads(th);
      setQuestions(q);
      setContinuity(c);
      setTrace(r);
    } catch {
      // offline: the previous data stays on screen; nothing to do
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(t);
  }, [refresh]);

  const select = useCallback(
    (id: string) => {
      setSelected(id);
      api.monitorProvenance(id).then(setProv).catch(() => setProv(null));
    },
    [],
  );

  return (
    <main className="monitor">
      <div className={`m-principle${online === false ? " off" : ""}`}>
        显微镜 · 只读 — 打开此页不产生任何心智事件：不检索、不固化、不醒来。
      </div>
      <div className="mon-grid">
        <TimelinePanel
          events={timeline?.events ?? []}
          counts={timeline?.counts ?? {}}
          onSelect={select}
          selected={selected}
        />
        <ProvenancePanel prov={prov} />
        <ThreadsPanel threads={threads} />
        <QuestionsPanel questions={questions} />
        <RetrievalPanel trace={trace} />
        <ContinuityPanel continuity={continuity} />
      </div>
    </main>
  );
}
