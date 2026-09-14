import type { ReentryResponse } from "../api";

interface Props {
  reentry: ReentryResponse | null;
  error: string | null;
  loading: boolean;
}

function truncateId(id: string): string {
  return id.length > 18 ? id.slice(0, 18) + "…" : id;
}

function DecisionLine({ d }: { d: ReentryResponse["decision"] }) {
  switch (d) {
    case "share_now":
      return <p className="decision share">Resident 想和你分享</p>;
    case "mention_later":
      return <p className="decision later">稍后再提</p>;
    case "nothing_to_share":
      return <p className="decision nothing">没有什么值得说的 — 这也是真实的</p>;
    default:
      return <p className="decision nothing">{d}</p>;
  }
}

export function ReentryPanel({ reentry, error, loading }: Props) {
  return (
    <section className="card reentry">
      <div className="card-head">
        <h2>
          Re-entry<span className="en">回来时，想说什么</span>
        </h2>
      </div>

      {loading && <p className="muted">正在评估这段时间发生的事…</p>}

      {!loading && error && <p className="err-hint">{error}</p>}

      {!loading && !error && !reentry && (
        <p className="muted">尚未评估。点右上角「评估 Re-entry」，看看 Resident 想分享什么。</p>
      )}

      {reentry && !loading && (
        <>
          <DecisionLine d={reentry.decision} />

          {reentry.candidates.length > 0 && (
            <div className="candidates">
              {reentry.candidates.map((c, i) => (
                <div className="candidate" key={`${i}-${c.event_ids.join(",")}`}>
                  <p className="claim">{c.claim}</p>
                  {c.event_ids.length > 0 && (
                    <div className="chips">
                      {c.event_ids.map((id) => (
                        <span className="chip" key={id} title={id}>
                          {truncateId(id)}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          <p className="muted meta">已评估 {reentry.evaluated_count} 个事件</p>
        </>
      )}
    </section>
  );
}
