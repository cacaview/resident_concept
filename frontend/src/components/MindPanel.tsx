import type { MindResponse, ThreadEntry } from "../api";
import { fmtTime } from "../api";

interface ThreadView {
  id: string;
  title: string;
}

function toThread(e: ThreadEntry): ThreadView {
  if (typeof e === "string") return { id: e, title: e };
  return { id: e.thread_id, title: e.title || e.thread_id };
}

export function MindPanel({ mind }: { mind: MindResponse | null }) {
  return (
    <section className="card mind">
      <div className="card-head">
        <h2>
          此刻的内心<span className="en">Mind</span>
        </h2>
      </div>
      {mind ? <MindContent mind={mind} /> : <p className="muted empty-p">还没有持久的内心记录。等待下一次醒来。</p>}
    </section>
  );
}

function MindContent({ mind }: { mind: MindResponse }) {
  const empty =
    !mind.mind_brief &&
    mind.threads.length === 0 &&
    mind.active_threads.length === 0 &&
    mind.dormant_threads.length === 0 &&
    mind.open_questions.length === 0 &&
    mind.recent_thoughts.length === 0;

  if (empty) return <p className="muted empty-p">内心暂时是空的 — 等待下一次醒来。</p>;

  // Prefer the explicit active/dormant lists; fall back to `threads` filtered by state.
  const activeRaw =
    mind.active_threads.length > 0
      ? mind.active_threads
      : mind.threads.filter((t) => /active|awake/i.test(t.state));
  const dormantRaw =
    mind.dormant_threads.length > 0
      ? mind.dormant_threads
      : mind.threads.filter((t) => !/active|awake/i.test(t.state));
  const active = activeRaw.map(toThread);
  const dormant = dormantRaw.map(toThread);

  return (
    <>
      {mind.mind_brief && <p className="brief">{mind.mind_brief}</p>}

      {active.length > 0 && (
        <div className="section">
          <p className="subhead">活跃线程</p>
          {active.map((t) => (
            <div className="thread-row" key={t.id} title={t.id}>
              <span className="t-title">{t.title}</span>
              <span className="badge badge-active">活跃</span>
            </div>
          ))}
        </div>
      )}

      {dormant.length > 0 && (
        <div className="section">
          <p className="subhead">沉睡线程</p>
          {dormant.map((t) => (
            <div className="thread-row row-dim" key={t.id} title={t.id}>
              <span className="t-title">{t.title}</span>
              <span className="badge badge-dormant">沉睡</span>
            </div>
          ))}
        </div>
      )}

      {mind.open_questions.length > 0 && (
        <div className="section">
          <p className="subhead">开放问题</p>
          {mind.open_questions.map((q) => (
            <div className="question-row" key={q.question_id}>
              <span className="qmark">?</span>
              <span>{q.text}</span>
            </div>
          ))}
        </div>
      )}

      {mind.recent_thoughts.length > 0 && (
        <div className="section">
          <p className="subhead">近期持久思想</p>
          {mind.recent_thoughts.map((t) => (
            <div className="thought-block" key={t.id}>
              {t.text}
              <small>{fmtTime(t.created_at)}</small>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
