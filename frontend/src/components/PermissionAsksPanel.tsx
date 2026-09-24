import { useEffect, useState } from "react";
import { api, type PermissionAsk } from "../api";

/**
 * ADR-0024: the owner's view of the ask → authorization channel.
 *
 * While a deed runs, py-claw may hit a permission `ask` (a tool not in the
 * policy-granted rule set). The Runner records it (`action.permission_asked`)
 * and parks the executor waiting for the owner. This panel lists the LIVE
 * pending asks (tool, argument digest — bounded/redacted, never secrets —
 * elapsed, expires-in) with allow / deny buttons. The answer is delivered to
 * the runner's host loop over the in-process channel and recorded as
 * `action.permission_answered(answered_by="owner-webui")`. An ask nobody
 * answers before its expiry auto-denies (the deed lands
 * `action.failed(reason=permission_timeout)`).
 *
 * Shown only when the Agency is enabled; otherwise nothing to answer.
 */
export function PermissionAsksPanel() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [asks, setAsks] = useState<PermissionAsk[]>([]);
  const [offline, setOffline] = useState(false);
  const [busyAsk, setBusyAsk] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Poll the pending asks (the panel is the only live thing in the sidecol —
  // the other panels refresh on the app-wide 3s poll).
  useEffect(() => {
    let cancelled = false;
    const poll = () => {
      api
        .permissionAsks()
        .then((r) => {
          if (cancelled) return;
          setEnabled(r.enabled);
          setAsks(r.asks);
          setOffline(false);
        })
        .catch(() => {
          if (!cancelled) setOffline(true);
        });
    };
    poll();
    const t = window.setInterval(poll, 2500);
    return () => {
      cancelled = true;
      window.clearInterval(t);
    };
  }, []);

  async function answer(askId: string, value: "allow" | "deny") {
    if (busyAsk) return;
    setBusyAsk(askId);
    setError(null);
    try {
      await api.answerPermissionAsk(askId, value);
      // Optimistic: the ask leaves the list on the next poll; drop it now.
      setAsks((cur) => cur.filter((a) => a.ask_id !== askId));
    } catch (e) {
      setError(e instanceof Error ? e.message : "回答失败");
    } finally {
      setBusyAsk(null);
    }
  }

  if (enabled === false) return null; // the Agency is off — no asks can exist

  return (
    <section className="card asks">
      <div className="card-head">
        <h2>
          权限请求<span className="en">Permission asks</span>
        </h2>
      </div>

      {offline && <p className="err-hint">backend 可能离线</p>}

      {!offline && asks.length === 0 && (
        <p className="muted">没有等待回答的请求 — 被授予的工具直接运行，未授予的会先问你。</p>
      )}

      {!offline && asks.length > 0 && (
        <div className="asks-list">
          {asks.map((a) => (
            <div className="ask" key={a.ask_id}>
              <div className="ask-head">
                <span className="ask-tool">{a.tool}</span>
                <span className="ask-age muted">{Math.floor(a.age_s)}s · 剩 {Math.max(0, Math.ceil((new Date(a.expires_at).getTime() - Date.now()) / 1000))}s</span>
              </div>
              <p className="ask-digest" title={a.argument_digest}>
                {a.argument_digest}
              </p>
              <div className="ask-actions">
                <button
                  className="btn ask-allow"
                  disabled={busyAsk !== null}
                  onClick={() => void answer(a.ask_id, "allow")}
                >
                  允许
                </button>
                <button
                  className="btn btn-ghost ask-deny"
                  disabled={busyAsk !== null}
                  onClick={() => void answer(a.ask_id, "deny")}
                >
                  拒绝
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {error && <p className="err-hint">{error}</p>}
    </section>
  );
}
