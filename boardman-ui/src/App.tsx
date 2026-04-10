import axios from "axios";
import { useCallback, useState } from "react";

type Role = "user" | "assistant";

type ChatMessage = {
  role: Role;
  content: string;
};

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || "",
  headers: { "Content-Type": "application/json" },
});

async function sendChat(
  message: string,
  opts: { sessionId: string | null; repo: string; allowWrites: boolean }
): Promise<{ reply: string; session_id: string }> {
  const { data } = await api.post("/api/v1/agent/chat", {
    message,
    session_id: opts.sessionId || undefined,
    repo: opts.repo || undefined,
    allow_writes: opts.allowWrites,
  });
  return { reply: data.reply, session_id: data.session_id };
}

export default function App() {
  const [repo, setRepo] = useState("");
  const [allowWrites, setAllowWrites] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [widgetOpen, setWidgetOpen] = useState(false);
  const [widgetMessages, setWidgetMessages] = useState<ChatMessage[]>([]);
  const [widgetInput, setWidgetInput] = useState("");
  const [widgetLoading, setWidgetLoading] = useState(false);

  const onSendMain = useCallback(async () => {
    const text = input.trim();
    if (!text || loading) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", content: text }]);
    setLoading(true);
    try {
      const { reply, session_id } = await sendChat(text, { sessionId, repo, allowWrites });
      setSessionId(session_id);
      setMessages((m) => [...m, { role: "assistant", content: reply }]);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setMessages((m) => [...m, { role: "assistant", content: `Error: ${msg}` }]);
    } finally {
      setLoading(false);
    }
  }, [input, loading, sessionId, repo, allowWrites]);

  const onSendWidget = useCallback(async () => {
    const text = widgetInput.trim();
    if (!text || widgetLoading) return;
    setWidgetInput("");
    setWidgetMessages((m) => [...m, { role: "user", content: text }]);
    setWidgetLoading(true);
    try {
      const { reply, session_id } = await sendChat(text, {
        sessionId,
        repo,
        allowWrites,
      });
      setSessionId(session_id);
      setWidgetMessages((m) => [...m, { role: "assistant", content: reply }]);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setWidgetMessages((m) => [...m, { role: "assistant", content: `Error: ${msg}` }]);
    } finally {
      setWidgetLoading(false);
    }
  }, [widgetInput, widgetLoading, sessionId, repo, allowWrites]);

  return (
    <div className="layout">
      <aside className="sidebar">
        <h1>Board Manager</h1>
        <label>Repo context (owner/name)</label>
        <input value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="org/repo" />
        <div className="row">
          <input
            type="checkbox"
            id="aw"
            checked={allowWrites}
            onChange={(e) => setAllowWrites(e.target.checked)}
          />
          <label htmlFor="aw" style={{ margin: 0 }}>
            Allow Plaky writes
          </label>
        </div>
        <label>Session</label>
        <input readOnly value={sessionId || "(new)"} title="session id" />
        <p style={{ fontSize: "0.7rem", color: "#666", marginTop: "1rem" }}>
          API: same origin or VITE_API_BASE. Dev proxy <code>/api</code> → boardman :8090.
        </p>
      </aside>
      <main>
        <div className="chat-header">
          <h2 style={{ margin: 0 }}>Interactive Chat</h2>
        </div>
        <div className="chat-box">
          {messages.length === 0 ? (
            <div style={{ color: "#666", textAlign: "center", padding: "2rem" }}>
              Message the Board Manager agent.
            </div>
          ) : (
            messages.map((msg, i) => (
              <div key={i} className={`msg ${msg.role}`}>
                <div className="msg-role">{msg.role.toUpperCase()}</div>
                <div style={{ whiteSpace: "pre-wrap" }}>{msg.content}</div>
              </div>
            ))
          )}
        </div>
        <div className="composer">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && onSendMain()}
            placeholder="Type a message…"
          />
          <button type="button" disabled={loading || !input.trim()} onClick={onSendMain}>
            Send
          </button>
        </div>
      </main>

      <button type="button" className="widget-toggle" onClick={() => setWidgetOpen((o) => !o)} title="Messages">
        💬
      </button>
      {widgetOpen && (
        <div className="widget-panel">
          <header>
            <span>Messages (same session)</span>
            <button type="button" onClick={() => setWidgetOpen(false)} style={{ background: "none", border: "none", color: "#999", cursor: "pointer" }}>
              ✕
            </button>
          </header>
          <div className="widget-messages">
            {widgetMessages.map((msg, i) => (
              <div key={i} className={`msg ${msg.role}`} style={{ fontSize: "0.8rem" }}>
                <div className="msg-role">{msg.role}</div>
                <div style={{ whiteSpace: "pre-wrap" }}>{msg.content}</div>
              </div>
            ))}
          </div>
          <div className="widget-composer">
            <input
              value={widgetInput}
              onChange={(e) => setWidgetInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && onSendWidget()}
              placeholder="Quick message…"
            />
            <button type="button" disabled={widgetLoading} onClick={onSendWidget}>
              Send
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
