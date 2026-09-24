import { useEffect, useRef } from "react";

export interface ChatMessage {
  id: string;
  role: "user" | "resident";
  text: string;
  pending?: boolean;
  error?: boolean;
}

interface Props {
  messages: ChatMessage[];
  draft: string;
  onDraft: (value: string) => void;
  onSend: () => void;
  sending: boolean;
}

export function ChatPanel({ messages, draft, onDraft, onSend, sending }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  return (
    <section className="card chat">
      <div className="card-head">
        <h2>
          对话<span className="en">Conversation</span>
        </h2>
        <p className="note">对话只是接口之一 — 第二条时间线在别处继续。</p>
      </div>

      <div className="messages" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="empty-chat">
            <p>还没有对话。</p>
            <p className="muted">Resident 的存在不依赖你的消息 — 你离开时，它的时间仍在走。</p>
          </div>
        )}
        {messages.map((m) => (
          <div
            key={m.id}
            className={`msg ${m.role === "user" ? "msg-user" : "msg-resident"}${
              m.error ? " msg-error" : ""
            }`}
          >
            <span className="who">{m.role === "user" ? "你" : "Resident"}</span>
            <span className="bubble">{m.pending ? <span className="typing">正在回应…</span> : m.text}</span>
          </div>
        ))}
      </div>

      <div className="composer">
        <textarea
          rows={1}
          value={draft}
          placeholder="对 Resident 说点什么…（Enter 发送，Shift+Enter 换行）"
          onChange={(e) => onDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              onSend();
            }
          }}
        />
        <button className="send" onClick={onSend} disabled={sending || draft.trim() === ""}>
          {sending ? "发送中…" : "发送"}
        </button>
      </div>
    </section>
  );
}
