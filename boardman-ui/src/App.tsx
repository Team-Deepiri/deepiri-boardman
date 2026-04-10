import axios from "axios";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  IconAgent,
  IconBoard,
  IconChat,
  IconClose,
  IconRepo,
  IconSend,
  IconSession,
  IconSpark,
  IconUser,
} from "./components/Icons";

type Role = "user" | "assistant";

type ChatMessage = {
  role: Role;
  content: string;
};

type PlakyBoardRow = { id: string; name: string };
type PlakyGroupRow = { id: string; name: string };
type PlakyUserRow = { id: string; name: string };
type SupportTeamRow = { login: string; name?: string; html_url?: string };

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || "",
  headers: { "Content-Type": "application/json" },
});

async function sendChat(
  message: string,
  opts: {
    sessionId: string | null;
    repo: string;
    allowWrites: boolean;
    plakyBoardId: string;
    plakyGroupId: string;
  }
): Promise<{ reply: string; session_id: string }> {
  const { data } = await api.post("/api/v1/agent/chat", {
    message,
    session_id: opts.sessionId || undefined,
    repo: opts.repo || undefined,
    allow_writes: opts.allowWrites,
    plaky_board_id: opts.plakyBoardId || undefined,
    plaky_group_id: opts.plakyGroupId || undefined,
  });
  return { reply: data.reply, session_id: data.session_id };
}

function EmptyState() {
  return (
    <div className="empty-state" role="status">
      <div className="empty-state__mark" aria-hidden>
        <IconSpark className="empty-state__icon" />
      </div>
      <h3 className="empty-state__title">Start a conversation</h3>
      <p className="empty-state__text">
        Ask about priorities, Plaky tasks, or repo context. Optional: set a GitHub repo in the panel so
        replies stay scoped to that project — built for <strong>Deepiri</strong> delivery workflows.
      </p>
    </div>
  );
}

export default function App() {
  const [repo, setRepo] = useState("");
  const [allowWrites, setAllowWrites] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const [boards, setBoards] = useState<PlakyBoardRow[]>([]);
  const [groups, setGroups] = useState<PlakyGroupRow[]>([]);
  const [plakyBoardId, setPlakyBoardId] = useState("");
  const [plakyGroupId, setPlakyGroupId] = useState("");
  const [plakyBoardsHint, setPlakyBoardsHint] = useState<string | null>(null);
  const [plakyGroupsHint, setPlakyGroupsHint] = useState<string | null>(null);

  const [workspaceUsers, setWorkspaceUsers] = useState<PlakyUserRow[]>([]);
  const [usersHint, setUsersHint] = useState<string | null>(null);
  const [createTitle, setCreateTitle] = useState("");
  const [createBody, setCreateBody] = useState("");
  const [createBusy, setCreateBusy] = useState(false);
  const [createMsg, setCreateMsg] = useState<string | null>(null);
  const [engPick, setEngPick] = useState("");
  const [qaPick, setQaPick] = useState("");
  const [autoTeam, setAutoTeam] = useState(true);
  const [supportTeam, setSupportTeam] = useState<SupportTeamRow[]>([]);
  const [supportTeamHint, setSupportTeamHint] = useState<string | null>(null);
  const [supportTeamSpec, setSupportTeamSpec] = useState("Team-Deepiri/support-team");

  const scrollRef = useRef<HTMLDivElement>(null);
  const drawerScrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, loading]);

  useEffect(() => {
    const el = drawerScrollRef.current;
    if (el && drawerOpen) el.scrollTop = el.scrollHeight;
  }, [messages, loading, drawerOpen]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api.get<{
          ok?: boolean;
          boards?: PlakyBoardRow[];
          message?: string;
        }>("/api/v1/plaky/boards");
        if (cancelled) return;
        if (data.ok && Array.isArray(data.boards)) {
          setBoards(data.boards);
          setPlakyBoardsHint(null);
        } else {
          setBoards([]);
          setPlakyBoardsHint(data.message || "Could not load boards (check API key and base URL).");
        }
      } catch {
        if (!cancelled) {
          setBoards([]);
          setPlakyBoardsHint("Could not reach Plaky boards endpoint.");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!plakyBoardId) {
      setGroups([]);
      setPlakyGroupId("");
      setPlakyGroupsHint(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api.get<{
          ok?: boolean;
          groups?: PlakyGroupRow[];
          message?: string;
        }>(`/api/v1/plaky/boards/${encodeURIComponent(plakyBoardId)}/groups`);
        if (cancelled) return;
        if (data.ok && Array.isArray(data.groups)) {
          setGroups(data.groups);
          setPlakyGroupsHint(null);
        } else {
          setGroups([]);
          setPlakyGroupsHint(data.message || "Could not load groups for this board.");
        }
      } catch {
        if (!cancelled) {
          setGroups([]);
          setPlakyGroupsHint("Could not load groups.");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [plakyBoardId]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api.get<{
          ok?: boolean;
          users?: PlakyUserRow[];
          message?: string;
        }>("/api/v1/plaky/users");
        if (cancelled) return;
        if (data.ok && Array.isArray(data.users)) {
          setWorkspaceUsers(data.users);
          setUsersHint(null);
        } else {
          setWorkspaceUsers([]);
          setUsersHint(data.message || "Could not load Plaky workspace users.");
        }
      } catch {
        if (!cancelled) {
          setUsersHint("Could not reach Plaky users endpoint.");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api.get<{
          ok?: boolean;
          members?: SupportTeamRow[];
          team?: string;
          message?: string;
        }>("/api/v1/github/support-team/members");
        if (cancelled) return;
        if (data.team) {
          setSupportTeamSpec(data.team);
        }
        if (data.ok && Array.isArray(data.members)) {
          setSupportTeam(data.members);
          setSupportTeamHint(null);
        } else {
          setSupportTeam([]);
          setSupportTeamHint(data.message || "Could not load GitHub support team.");
        }
      } catch {
        if (!cancelled) {
          setSupportTeam([]);
          setSupportTeamHint("Could not reach GitHub support-team endpoint.");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const onCreateTask = useCallback(async () => {
    const t = createTitle.trim();
    if (!t || createBusy) return;
    setCreateBusy(true);
    setCreateMsg(null);
    try {
      const { data } = await api.post<{
        ok?: boolean;
        message?: string;
        task_url?: string;
        task?: { url?: string };
      }>("/api/v1/tasks", {
        title: t,
        description: createBody,
        priority: "medium",
        repo: repo.trim() || undefined,
        plaky_board_id: plakyBoardId || undefined,
        plaky_group_id: plakyGroupId || undefined,
        engineer_plaky_id: engPick || undefined,
        qa_plaky_id: qaPick || undefined,
        auto_assign_team: autoTeam,
      });
      if (data.ok) {
        setCreateMsg(data.task_url || data.task?.url || "Task created.");
        setCreateTitle("");
        setCreateBody("");
      } else {
        setCreateMsg(data.message || "Create failed.");
      }
    } catch (e: unknown) {
      setCreateMsg(axios.isAxiosError(e) ? e.message : String(e));
    } finally {
      setCreateBusy(false);
    }
  }, [
    createTitle,
    createBody,
    createBusy,
    repo,
    plakyBoardId,
    plakyGroupId,
    engPick,
    qaPick,
    autoTeam,
  ]);

  const onSend = useCallback(async () => {
    const text = input.trim();
    if (!text || loading) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", content: text }]);
    setLoading(true);
    try {
      const { reply, session_id } = await sendChat(text, {
        sessionId,
        repo,
        allowWrites,
        plakyBoardId,
        plakyGroupId,
      });
      setSessionId(session_id);
      setMessages((m) => [...m, { role: "assistant", content: reply }]);
    } catch (e: unknown) {
      let msg: string;
      if (axios.isAxiosError(e)) {
        const data = e.response?.data as { detail?: unknown; message?: string } | undefined;
        const detail = data?.detail;
        if (typeof detail === "string") {
          msg = detail;
        } else if (Array.isArray(detail)) {
          msg = detail.map((x) => (typeof x === "object" && x && "msg" in x ? String((x as { msg: string }).msg) : String(x))).join("; ");
        } else {
          msg = data?.message || e.message;
        }
      } else {
        msg = e instanceof Error ? e.message : String(e);
      }
      setMessages((m) => [...m, { role: "assistant", content: `Request failed: ${msg}` }]);
    } finally {
      setLoading(false);
      textareaRef.current?.focus();
    }
  }, [input, loading, sessionId, repo, allowWrites, plakyBoardId, plakyGroupId]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      onSend();
    }
  };

  return (
    <div className="app">
      <aside className="sidebar" aria-label="Settings">
        <div className="sidebar__brand">
          <IconBoard className="sidebar__brand-icon" title="" />
          <div>
            <div className="sidebar__brand-name sidebar__brand-name--gradient">Deepiri Board Manager</div>
            <div className="sidebar__brand-sub">Plaky · GitHub · delivery</div>
          </div>
        </div>

        <div className="field">
          <label className="field__label" htmlFor="repo-input">
            <IconRepo className="field__label-icon" />
            Repository context
          </label>
          <input
            id="repo-input"
            className="field__input"
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            placeholder="owner/repo"
            autoComplete="off"
          />
          <p className="field__hint">Optional. Passed to the agent for scoped answers.</p>
        </div>

        <div className="toggle-field">
          <div className="toggle-field__text">
            <span className="toggle-field__title">Plaky write tools</span>
            <span className="toggle-field__desc">Allow create, update, and comments in Plaky</span>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={allowWrites}
            className={`switch ${allowWrites ? "switch--on" : ""}`}
            onClick={() => setAllowWrites((v) => !v)}
          >
            <span className="switch__thumb" />
          </button>
        </div>

        <div className="field">
          <label className="field__label" htmlFor="plaky-board-select">
            <IconBoard className="field__label-icon" />
            Plaky board
          </label>
          <select
            id="plaky-board-select"
            className="field__input field__select"
            value={plakyBoardId}
            onChange={(e) => {
              setPlakyBoardId(e.target.value);
              setPlakyGroupId("");
            }}
          >
            <option value="">Default (env / server)</option>
            {boards.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name || b.id}
              </option>
            ))}
          </select>
          {plakyBoardsHint ? <p className="field__hint field__hint--warn">{plakyBoardsHint}</p> : null}
          <p className="field__hint">
            API uses a board (project) and a group (section). There is no separate &quot;table&quot; id.
          </p>
        </div>

        <div className="field">
          <label className="field__label" htmlFor="plaky-group-select">
            <IconBoard className="field__label-icon" />
            Plaky group
          </label>
          <select
            id="plaky-group-select"
            className="field__input field__select"
            value={plakyGroupId}
            disabled={!plakyBoardId}
            onChange={(e) => setPlakyGroupId(e.target.value)}
          >
            <option value="">
              {plakyBoardId ? "Default for board / env" : "Pick a board first"}
            </option>
            {groups.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name || g.id}
              </option>
            ))}
          </select>
          {plakyGroupsHint && plakyBoardId ? (
            <p className="field__hint field__hint--warn">{plakyGroupsHint}</p>
          ) : null}
        </div>

        <div className="field">
          <label className="field__label" htmlFor="session-display">
            <IconSession className="field__label-icon" />
            Session
          </label>
          <input
            id="session-display"
            className="field__input field__input--readonly"
            readOnly
            value={sessionId || "New session"}
            title="Conversation session id"
          />
        </div>

        <div className="field field--create-task">
          <label className="field__label" htmlFor="create-title">
            <IconBoard className="field__label-icon" />
            Create Plaky task
          </label>
          <input
            id="create-title"
            className="field__input"
            value={createTitle}
            onChange={(e) => setCreateTitle(e.target.value)}
            placeholder="Title"
            autoComplete="off"
          />
          <textarea
            className="field__input field__textarea"
            rows={2}
            value={createBody}
            onChange={(e) => setCreateBody(e.target.value)}
            placeholder="Description (optional)"
          />
          <div className="toggle-field toggle-field--compact">
            <div className="toggle-field__text">
              <span className="toggle-field__title">Auto-assign engineer + QA</span>
              <span className="toggle-field__desc">Uses team_assignments.yml for this repo</span>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={autoTeam}
              className={`switch ${autoTeam ? "switch--on" : ""}`}
              onClick={() => setAutoTeam((v) => !v)}
            >
              <span className="switch__thumb" />
            </button>
          </div>
          <p className="field__label field__label--sub" id="support-roster-label">
            GitHub support team <span className="support-roster__spec">({supportTeamSpec})</span>
          </p>
          {supportTeamHint ? (
            <p className="field__hint field__hint--warn" role="status">
              {supportTeamHint}
            </p>
          ) : supportTeam.length > 0 ? (
            <ul
              className="support-roster"
              aria-labelledby="support-roster-label"
            >
              {supportTeam.map((m) => (
                <li key={m.login} className="support-roster__item">
                  <span className="support-roster__name">{m.name?.trim() || m.login}</span>
                  {m.name?.trim() ? (
                    <span className="support-roster__login"> @{m.login}</span>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <p className="field__hint">No members yet (configure GITHUB_PAT with read:org).</p>
          )}
          <label className="field__label field__label--sub" htmlFor="eng-assign">
            Engineer (Plaky member)
          </label>
          <select
            id="eng-assign"
            className="field__input field__select"
            value={engPick}
            onChange={(e) => setEngPick(e.target.value)}
          >
            <option value="">None / use auto only</option>
            {workspaceUsers.map((u) => (
              <option key={u.id} value={u.id}>
                {u.name || u.id}
              </option>
            ))}
          </select>
          <label className="field__label field__label--sub" htmlFor="qa-assign">
            QA (Plaky member)
          </label>
          <select
            id="qa-assign"
            className="field__input field__select"
            value={qaPick}
            onChange={(e) => setQaPick(e.target.value)}
          >
            <option value="">None / use auto only</option>
            {workspaceUsers.map((u) => (
              <option key={`qa-${u.id}`} value={u.id}>
                {u.name || u.id}
              </option>
            ))}
          </select>
          {usersHint ? <p className="field__hint field__hint--warn">{usersHint}</p> : null}
          <button
            type="button"
            className="field__button"
            disabled={createBusy || !createTitle.trim()}
            onClick={onCreateTask}
          >
            {createBusy ? "Creating…" : "Create task"}
          </button>
          {createMsg ? <p className="field__hint">{createMsg}</p> : null}
        </div>

        <footer className="sidebar__foot">
          <p>
            API requests use this origin or <code className="code">VITE_API_BASE</code>. In dev, Vite
            proxies <code className="code">/api</code> to the boardman service.
          </p>
        </footer>
      </aside>

      <main className="main">
        <header className="main__header">
          <div>
            <h1 className="main__title">Chat</h1>
            <p className="main__subtitle">
              <strong>Deepiri</strong> Board Manager agent
            </p>
          </div>
        </header>

        <div className="chat-shell">
          <div className="chat-scroll" ref={scrollRef}>
            {messages.length === 0 ? (
              <EmptyState />
            ) : (
              <ul className="message-list">
                {messages.map((msg, i) => (
                  <li
                    key={i}
                    className={`message message--${msg.role}`}
                  >
                    <div className="message__avatar" aria-hidden>
                      {msg.role === "user" ? (
                        <IconUser className="message__avatar-icon" />
                      ) : (
                        <IconAgent className="message__avatar-icon" />
                      )}
                    </div>
                    <div className="message__body">
                      <div className="message__meta">
                        {msg.role === "user" ? "You" : "Assistant"}
                      </div>
                      <div className="message__content">{msg.content}</div>
                    </div>
                  </li>
                ))}
                {loading ? (
                  <li className="message message--assistant message--pending" aria-live="polite">
                    <div className="message__avatar" aria-hidden>
                      <IconAgent className="message__avatar-icon" />
                    </div>
                    <div className="message__body">
                      <div className="message__meta">Assistant</div>
                      <div className="typing" aria-label="Waiting for response">
                        <span />
                        <span />
                        <span />
                      </div>
                    </div>
                  </li>
                ) : null}
              </ul>
            )}
          </div>

          <div className="composer">
            <textarea
              ref={textareaRef}
              className="composer__input"
              rows={2}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="Message the agent…"
              disabled={loading}
            />
            <button
              type="button"
              className="composer__send"
              disabled={loading || !input.trim()}
              onClick={onSend}
              aria-label="Send message"
            >
              <IconSend className="composer__send-icon" title="Send" />
            </button>
          </div>
        </div>
      </main>

      <button
        type="button"
        className="fab"
        onClick={() => setDrawerOpen((o) => !o)}
        aria-expanded={drawerOpen}
        aria-controls="quick-chat-drawer"
        title="Open compact chat"
      >
        <IconChat className="fab__icon" title="" />
      </button>

      {drawerOpen ? (
        <div
          id="quick-chat-drawer"
          className="drawer"
          role="dialog"
          aria-label="Compact chat"
        >
          <header className="drawer__header">
            <span className="drawer__title">Same conversation</span>
            <button
              type="button"
              className="drawer__close"
              onClick={() => setDrawerOpen(false)}
              aria-label="Close panel"
            >
              <IconClose className="drawer__close-icon" title="" />
            </button>
          </header>
          <div className="drawer__scroll" ref={drawerScrollRef}>
            {messages.length === 0 ? (
              <p className="drawer__empty">No messages yet. Send from the main area or below.</p>
            ) : (
              <ul className="drawer__list">
                {messages.map((msg, i) => (
                  <li key={i} className={`drawer__msg drawer__msg--${msg.role}`}>
                    <span className="drawer__msg-role">{msg.role === "user" ? "You" : "Assistant"}</span>
                    <div className="drawer__msg-text">{msg.content}</div>
                  </li>
                ))}
                {loading ? (
                  <li className="drawer__msg drawer__msg--assistant" aria-live="polite">
                    <span className="drawer__msg-role">Assistant</span>
                    <div className="typing typing--sm">
                      <span />
                      <span />
                      <span />
                    </div>
                  </li>
                ) : null}
              </ul>
            )}
          </div>
          <div className="drawer__composer">
            <textarea
              className="drawer__input"
              rows={2}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="Reply…"
              disabled={loading}
            />
            <button
              type="button"
              className="drawer__send"
              disabled={loading || !input.trim()}
              onClick={onSend}
              aria-label="Send"
            >
              <IconSend className="drawer__send-icon" title="" />
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
