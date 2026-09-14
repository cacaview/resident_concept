import { useCallback, useEffect, useState } from "react";
import type { HealthInfo, LifeResponse, MindResponse, ReentryResponse } from "./api";
import { api } from "./api";
import { Header } from "./components/Header";
import { ChatPanel, type ChatMessage } from "./components/ChatPanel";
import { MindPanel } from "./components/MindPanel";
import { ReentryPanel } from "./components/ReentryPanel";
import { LifeTimeline } from "./components/LifeTimeline";
import { MonitorPage } from "./components/MonitorPage";

const POLL_MS = 3000;

let uidCounter = 0;
function uid(): string {
  uidCounter += 1;
  return `local-${Date.now().toString(36)}-${uidCounter}`;
}

interface BusyState {
  wake: boolean;
  reentry: boolean;
}

export function App() {
  const [online, setOnline] = useState<boolean | null>(null);
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [life, setLife] = useState<LifeResponse | null>(null);
  const [mind, setMind] = useState<MindResponse | null>(null);
  const [reentry, setReentry] = useState<ReentryResponse | null>(null);
  const [reentryError, setReentryError] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [busy, setBusy] = useState<BusyState>({ wake: false, reentry: false });
  // v0.2 Step 5: two views — Resident's face (the conversation) and the
  // Monitor (the microscope). The monitor is strictly read-only.
  const [view, setView] = useState<"face" | "monitor">("face");

  // Poll durable state. On failure we keep the last data and surface a
  // muted offline hint in the header — the page never crashes.
  const refresh = useCallback(async () => {
    try {
      const [h, l, m] = await Promise.all([api.health(), api.life(40), api.mind()]);
      setHealth(h);
      setLife(l);
      setMind(m);
      setOnline(true);
    } catch {
      setOnline(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(t);
  }, [refresh]);

  // Hydrate the conversation from the event store once on mount: the
  // interface forgets nothing the second timeline has kept. New messages in
  // this session are layered on top; a mid-flight reload only loses the one
  // in-flight exchange, which the store already recorded on the server side
  // (the user message is persisted before the reply is generated).
  useEffect(() => {
    let cancelled = false;
    api
      .events({ limit: 100, type: "conversation", order: "desc" })
      .then((evts) => {
        if (cancelled) return;
        const msgs: ChatMessage[] = evts
          .slice()
          .reverse() // store returns newest-first; the panel reads oldest-first
          .map((e) => ({
            id: e.id,
            role: (e.type === "conversation.user_message" ? "user" : "resident") as "user" | "resident",
            text: String(e.content?.text ?? ""),
          }))
          .filter((m) => m.text !== "");
        setMessages((cur) => (cur.length > 0 ? cur : msgs));
      })
      .catch(() => {
        // backend offline: the header shows the offline hint; nothing to hydrate
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSend() {
    const text = draft.trim();
    if (!text || sending) return;
    setDraft("");
    setSending(true);
    const pendingId = uid();
    setMessages((m) => [
      ...m,
      { id: uid(), role: "user", text },
      { id: pendingId, role: "resident", text: "", pending: true },
    ]);
    try {
      const r = await api.chat(text);
      setMessages((m) => m.map((msg) => (msg.id === pendingId ? { ...msg, text: r.reply, pending: false } : msg)));
    } catch {
      setMessages((m) =>
        m.map((msg) =>
          msg.id === pendingId ? { ...msg, text: "（backend 离线，这条消息没有送达）", pending: false, error: true } : msg,
        ),
      );
    } finally {
      setSending(false);
      void refresh();
    }
  }

  async function handleWake() {
    if (busy.wake) return;
    setBusy((b) => ({ ...b, wake: true }));
    try {
      await api.wake();
    } catch {
      // Offline state is surfaced by the header; nothing else to do.
    } finally {
      setBusy((b) => ({ ...b, wake: false }));
      void refresh();
    }
  }

  async function handleReentry() {
    if (busy.reentry) return;
    setBusy((b) => ({ ...b, reentry: true }));
    setReentryError(null);
    try {
      setReentry(await api.reentry(null));
    } catch {
      setReentryError("评估失败 — backend 可能离线。");
    } finally {
      setBusy((b) => ({ ...b, reentry: false }));
      void refresh();
    }
  }

  return (
    <div className="shell">
      <Header
        health={health}
        online={online}
        busy={busy}
        onWake={() => void handleWake()}
        onReentry={() => void handleReentry()}
      />
      <div className="viewbar">
        <button className={`btn${view === "face" ? "" : " btn-ghost"}`} onClick={() => setView("face")}>
          Resident
        </button>
        <button
          className={`btn${view === "monitor" ? "" : " btn-ghost"}`}
          onClick={() => setView("monitor")}
        >
          显微镜 · Monitor
        </button>
        <span className="viewbar-note">
          {view === "monitor" ? "只读视图 — 不触发检索/固化/醒来" : "对话视图 — 与 Resident 见面"}
        </span>
      </div>
      {view === "monitor" ? (
        <MonitorPage online={online} />
      ) : (
        <main className="grid">
          <ChatPanel messages={messages} draft={draft} onDraft={setDraft} onSend={() => void handleSend()} sending={sending} />
          <div className="sidecol">
            <MindPanel mind={mind} />
            <ReentryPanel reentry={reentry} error={reentryError} loading={busy.reentry} />
          </div>
          <LifeTimeline life={life} />
        </main>
      )}
      <footer>Resident · v0.2 — From Simulation to Habitat</footer>
    </div>
  );
}
