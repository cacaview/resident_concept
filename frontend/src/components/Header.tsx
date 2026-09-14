import type { HealthInfo } from "../api";

interface Props {
  health: HealthInfo | null;
  online: boolean | null;
  busy: { wake: boolean; reentry: boolean };
  onWake: () => void;
  onReentry: () => void;
}

export function Header({ health, online, busy, onWake, onReentry }: Props) {
  const dbBase = health?.db ? health.db.split("/").pop() : "";

  return (
    <header>
      <div className="head-left">
        <h1>Resident</h1>
        <div className="statusline">
          {online === false ? (
            <span className="item off">
              <i className="dot dot-off" />
              backend 离线
            </span>
          ) : online === null ? (
            <span className="item dim">
              <i className="dot dot-dim" />
              连接中…
            </span>
          ) : (
            <>
              <span className={health?.scheduler_running ? "item" : "item dim"}>
                <i className={health?.scheduler_running ? "dot dot-on" : "dot dot-dim"} />
                {health?.scheduler_running ? "scheduler 运行中" : "scheduler 已停止"}
              </span>
              <span className="item dim">{health?.events ?? 0} events</span>
              {dbBase ? (
                <span className="status-db" title={health?.db ?? ""}>
                  {dbBase}
                </span>
              ) : null}
            </>
          )}
        </div>
      </div>
      <div className="head-actions">
        <button className="btn" onClick={onWake} disabled={busy.wake}>
          {busy.wake ? "唤醒中…" : "手动唤醒一次"}
        </button>
        <button className="btn btn-ghost" onClick={onReentry} disabled={busy.reentry}>
          {busy.reentry ? "评估中…" : "评估 Re-entry"}
        </button>
      </div>
    </header>
  );
}
