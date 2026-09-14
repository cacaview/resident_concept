import type { CSSProperties } from "react";
import type { LifeEvent, LifeResponse, LifeStats, WakeSummary } from "../api";
import { fmtTime } from "../api";
import { routeMeta } from "../routes";

type Row =
  | { key: string; at: number; iso: string; kind: "wake"; wake: WakeSummary }
  | { key: string; at: number; iso: string; kind: "event"; evt: LifeEvent };

function parseTime(iso: string): number {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : t;
}

/**
 * Merge wake summaries and life events into one descending timeline.
 * Raw wake/sleep events (types starting with "wake." or "sleep.") are
 * skipped: wake cycles are rendered from the `wakes` summary array so
 * they are not shown twice.
 */
function buildRows(life: LifeResponse): Row[] {
  const rows: Row[] = [];
  for (const w of life.wakes) {
    rows.push({ key: "wake-" + w.run_id, at: parseTime(w.created_at), iso: w.created_at, kind: "wake", wake: w });
  }
  for (const e of life.events) {
    if (e.type.startsWith("wake.") || e.type.startsWith("sleep.")) continue;
    rows.push({ key: "evt-" + e.id, at: parseTime(e.created_at), iso: e.created_at, kind: "event", evt: e });
  }
  rows.sort((a, b) => b.at - a.at);
  return rows;
}

function badgeStyle(color: string): CSSProperties {
  return { color, background: color + "1f", border: `1px solid ${color}55` };
}

function shortText(s: string): string {
  return s.length > 100 ? s.slice(0, 100) + "…" : s;
}

function RowView({ row }: { row: Row }) {
  const time = <span className="tl-time">{fmtTime(row.iso)}</span>;

  if (row.kind === "wake") {
    const w = row.wake;
    if (w.result === "noop") {
      return (
        <div className="tl-row noop">
          {time}
          <span className="tl-noop">这次醒来，什么也没发生 · {w.route || "rest"}（合法）</span>
        </div>
      );
    }
    const meta = routeMeta(w.route);
    return (
      <div className="tl-row">
        {time}
        <span className="route-badge" style={badgeStyle(meta.color)} title={`${meta.label}（${w.route}）`}>
          {w.route || "未知"}
        </span>
        <span className="tl-label">醒来 · 留下了痕迹</span>
      </div>
    );
  }

  const e = row.evt;
  const isConv = e.type.startsWith("conversation.");
  const text = isConv ? shortText(e.text) : e.text;
  return (
    <div className={`tl-row${isConv ? " conv" : ""}`}>
      {time}
      <span className="type" title={e.type}>
        {e.type}
      </span>
      {text && <span className={isConv ? "tl-text short" : "tl-text"}>{text}</span>}
    </div>
  );
}

function StatsStrip({ stats }: { stats: LifeStats }) {
  const routes = Object.entries(stats.route_distribution ?? {});
  return (
    <div className="stats">
      <span>{stats.events} 事件</span>
      <span>{stats.wakes} 醒来</span>
      <span>{stats.noops} 空醒</span>
      {routes.map(([r, n]) => (
        <span className="route-stat" key={r} title={routeMeta(r).label}>
          <i className="swatch" style={{ background: routeMeta(r).color }} />
          {r} <b>{n}</b>
        </span>
      ))}
    </div>
  );
}

export function LifeTimeline({ life }: { life: LifeResponse | null }) {
  const rows = life ? buildRows(life) : [];
  return (
    <section className="card timeline">
      <div className="card-head">
        <h2>
          时间线<span className="en">Life Timeline</span>
        </h2>
      </div>
      {life && <StatsStrip stats={life.stats} />}
      <div className="timeline-body">
        {rows.length === 0 ? (
          <p className="muted empty-p">还没有时间线。等待第一次醒来。</p>
        ) : (
          rows.map((r) => <RowView key={r.key} row={r} />)
        )}
      </div>
    </section>
  );
}
