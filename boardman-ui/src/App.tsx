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
import { AppSelect } from "./components/AppSelect";

type Role = "user" | "assistant";

type ChatMessage = {
  role: Role;
  content: string;
};

type PlakyBoardRow = { id: string; name: string };
type PlakyGroupRow = { id: string; name: string };
type PlakyUserRow = { id: string; name: string };
type SupportTeamRow = { login: string; name?: string; html_url?: string };
type LlmModel = { name: string; size?: number; details?: Record<string, unknown> };

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || "",
  headers: { "Content-Type": "application/json" },
});

type StreamSsePayload =
  | { type: "session"; session_id: string }
  | { type: "token"; text: string }
  | { type: "done" }
  | { type: "error"; message: string };

/** Plain chat only: SSE from Ollama → snappier perceived latency (tokens as they generate). */
async function sendChatStream(
  message: string,
  opts: {
    sessionId: string | null;
    repo: string;
    allowWrites: boolean;
    useTools: boolean;
    plakyBoardId: string;
    plakyGroupId: string;
    model?: string;
  },
  onSession: (sessionId: string) => void,
  onToken: (delta: string) => void
): Promise<void> {
  const base = (import.meta.env.VITE_API_BASE || "").replace(/\/$/, "");
  const url = `${base}/api/v1/agent/chat/stream`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({
      message,
      session_id: opts.sessionId || undefined,
      repo: opts.repo || undefined,
      allow_writes: opts.allowWrites,
      use_tools: opts.useTools,
      plaky_board_id: opts.plakyBoardId || undefined,
      plaky_group_id: opts.plakyGroupId || undefined,
      model: opts.model || undefined,
    }),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = (await res.json()) as { detail?: unknown };
      const d = j?.detail;
      if (typeof d === "string") detail = d;
      else if (Array.isArray(d))
        detail = d.map((x) => (typeof x === "object" && x && "msg" in x ? String((x as { msg: string }).msg) : String(x))).join("; ");
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  const reader = res.body?.getReader();
  if (!reader) throw new Error("No response body");
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    for (;;) {
      const nl = buf.indexOf("\n");
      if (nl < 0) break;
      const line = buf.slice(0, nl).replace(/\r$/, "");
      buf = buf.slice(nl + 1);
      if (!line.startsWith("data: ")) continue;
      let j: StreamSsePayload;
      try {
        j = JSON.parse(line.slice(6)) as StreamSsePayload;
      } catch {
        continue;
      }
      if (j.type === "session") onSession(j.session_id);
      else if (j.type === "token") onToken(j.text);
      else if (j.type === "error") throw new Error(j.message || "stream error");
    }
  }
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
  const [selectedRepos, setSelectedRepos] = useState<string[]>([]);
  const [orgRepos, setOrgRepos] = useState<string[]>([]);
  const [orgReposHint, setOrgReposHint] = useState<string | null>(null);
  const [allowWrites, setAllowWrites] = useState(true);
  const [useTools, setUseTools] = useState(true);
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

  const [llmModels, setLlmModels] = useState<LlmModel[]>([]);
  const [selectedModel, setSelectedModel] = useState("");
  const [llmModelsHint, setLlmModelsHint] = useState<string | null>(null);

  const [workspaceUsers, setWorkspaceUsers] = useState<PlakyUserRow[]>([]);
  const [usersHint, setUsersHint] = useState<string | null>(null);
  const [createTitle, setCreateTitle] = useState("");
  const [createBody, setCreateBody] = useState("");
  const [createBusy, setCreateBusy] = useState(false);
  const [createMsg, setCreateMsg] = useState<string | null>(null);
  const [engPick, setEngPick] = useState("");
  const [qaPick, setQaPick] = useState("");
  const [supportTeam, setSupportTeam] = useState<SupportTeamRow[]>([]);
  const [supportTeamHint, setSupportTeamHint] = useState<string | null>(null);
  const [supportTeamSpec, setSupportTeamSpec] = useState("Team-Deepiri/support-team");

  const [classifyBusy, setClassifyBusy] = useState(false);
  const [classifyMsg, setClassifyMsg] = useState<string | null>(null);

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
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api.get<{
          ok?: boolean;
          repos?: string[];
          message?: string;
        }>("/api/v1/repos/org");
        if (cancelled) return;
        if (data.ok && Array.isArray(data.repos)) {
          setOrgRepos(data.repos);
          setOrgReposHint(null);
        } else {
          setOrgRepos([]);
          setOrgReposHint(data.message || "Could not load org repositories.");
        }
      } catch {
        if (!cancelled) {
          setOrgRepos([]);
          setOrgReposHint("Could not reach org repositories endpoint.");
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

  // Fetch available LLM models
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api.get<{
          ok?: boolean;
          provider?: string;
          models?: LlmModel[];
          current?: string;
          error?: string;
        }>("/api/v1/llm/models");
        if (cancelled) return;
        if (data.ok && Array.isArray(data.models)) {
          setLlmModels(data.models);
          setSelectedModel(data.current || "");
          setLlmModelsHint(null);
        } else {
          setLlmModels([]);
          setLlmModelsHint(data.error || "Could not load models.");
        }
      } catch {
        if (!cancelled) {
          setLlmModels([]);
          setLlmModelsHint("Could not reach LLM models endpoint.");
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
        priority: "Medium",
        github_repos: selectedRepos.length > 0 ? selectedRepos : undefined,
        plaky_board_id: plakyBoardId || undefined,
        plaky_group_id: plakyGroupId || undefined,
        engineer_plaky_id: engPick || undefined,
        qa_plaky_id: qaPick || undefined,
        auto_assign_team: true,
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
    selectedRepos,
    plakyBoardId,
    plakyGroupId,
    engPick,
    qaPick,
  ]);

  const onSend = useCallback(async () => {
    const text = input.trim();
    if (!text || loading) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", content: text }]);
    setLoading(true);
    try {
      let acc = "";
      await sendChatStream(
        text,
        {
          sessionId,
          repo: selectedRepos[0] || "",
          allowWrites,
          useTools,
          plakyBoardId,
          plakyGroupId,
          model: selectedModel || undefined,
        },
        (sid) => setSessionId(sid),
        (delta) => {
          if (!acc) {
            // First token: add the assistant message to the list
            setMessages((m) => [...m, { role: "assistant", content: delta }]);
          } else {
            // Subsequent tokens: update the last assistant message
            setMessages((m) => {
              const next = [...m];
              const last = next[next.length - 1];
              if (last && last.role === "assistant") {
                next[next.length - 1] = { ...last, content: acc + delta };
              }
              return next;
            });
          }
          acc += delta;
        }
      );
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
      setMessages((m) => {
        if (m.length === 0) return [{ role: "assistant", content: `Request failed: ${msg}` }];
        const last = m[m.length - 1];
        if (last?.role === "assistant") {
          const copy = [...m];
          copy[copy.length - 1] = { role: "assistant", content: `Request failed: ${msg}` };
          return copy;
        }
        return [...m, { role: "assistant", content: `Request failed: ${msg}` }];
      });
    } finally {
      setLoading(false);
      textareaRef.current?.focus();
    }
  }, [input, loading, sessionId, selectedRepos, allowWrites, useTools, plakyBoardId, plakyGroupId, selectedModel]);

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
          <img src="/logo_squared_2_10.png" className="sidebar__brand-icon" alt="" />
          <div>
            <div className="sidebar__brand-name sidebar__brand-name--gradient">Deepiri Board Manager</div>
            <div className="sidebar__brand-sub">Plaky · GitHub · delivery</div>
          </div>
        </div>

        <div className="field">
          <button
            type="button"
            className="field__button field__button--secondary"
            disabled={classifyBusy}
            onClick={async () => {
              setClassifyBusy(true);
              setClassifyMsg(null);
              try {
                const { data } = await api.post<{ ok?: boolean; classified?: number; error?: string }>(
                  "/api/v1/repos/classify"
                );
                if (data.ok) {
                  setClassifyMsg(`Classified ${data.classified || 0} repos.`);
                } else {
                  setClassifyMsg(data.error || "Classification failed.");
                }
              } catch (e: unknown) {
                setClassifyMsg(axios.isAxiosError(e) ? e.message : String(e));
              } finally {
                setClassifyBusy(false);
              }
            }}
          >
            {classifyBusy ? "Classifying..." : "Re-classify repos"}
          </button>
          {classifyMsg ? <p className="field__hint">{classifyMsg}</p> : null}
        </div>

        <div className="field">
          <label className="field__label" htmlFor="repo-select">
            <IconRepo className="field__label-icon" />
            Repositories
          </label>
          <select
            id="repo-select"
            className="field__input field__select ui-scroll--translucent"
            multiple
            size={8}
            value={selectedRepos}
            onChange={(e) => {
              const values = Array.from(e.target.selectedOptions).map((o) => o.value);
              setSelectedRepos(values);
            }}
          >
            {orgRepos.map((full) => {
              const i = full.indexOf("/");
              const short = i >= 0 ? full.slice(i + 1) : full;
              return (
                <option key={full} value={full}>
                  {short}
                </option>
              );
            })}
          </select>
          {orgReposHint ? <p className="field__hint field__hint--warn">{orgReposHint}</p> : null}
          <p className="field__hint">
            Multi-select. Used for task creation (`github_repos`); first selected repo is used for chat scoping. Use CTRL+CLICK to select multiple.
          </p>
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

        <div className="toggle-field">
          <div className="toggle-field__text">
            <span className="toggle-field__title">Multi-step agent (tools)</span>
            <span className="toggle-field__desc">
              Off: single LLM completion (fast). On: multi-step tool-calling agent (slower, but both stream).
            </span>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={useTools}
            className={`switch ${useTools ? "switch--on" : ""}`}
            onClick={() => setUseTools((v) => !v)}
          >
            <span className="switch__thumb" />
          </button>
        </div>

        {llmModels.length > 0 && (
          <div className="field">
            <label className="field__label" htmlFor="llm-model-select">
              <IconAgent className="field__label-icon" />
              LLM Model
            </label>
            <AppSelect
              id="llm-model-select"
              value={selectedModel}
              onChange={setSelectedModel}
              emptyLabel="Default (server config)"
              options={llmModels.map((m) => ({ value: m.name, label: m.name }))}
            />
            {llmModelsHint ? <p className="field__hint field__hint--warn">{llmModelsHint}</p> : null}
            <p className="field__hint">
              Override the model for this session. Default uses server LLM_MODEL or auto-selects.
            </p>
          </div>
        )}

        <div className="field">
          <label className="field__label" htmlFor="plaky-board-select">
            <IconBoard className="field__label-icon" />
            Plaky board
          </label>
          <AppSelect
            id="plaky-board-select"
            value={plakyBoardId}
            onChange={(v) => {
              setPlakyBoardId(v);
              setPlakyGroupId("");
            }}
            emptyLabel="Default (env / server)"
            options={boards.map((b) => ({ value: b.id, label: b.name || b.id }))}
          />
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
          <AppSelect
            id="plaky-group-select"
            value={plakyGroupId}
            onChange={setPlakyGroupId}
            disabled={!plakyBoardId}
            emptyLabel={
              plakyBoardId ? "Default for board / env" : "Pick a board first"
            }
            options={groups.map((g) => ({ value: g.id, label: g.name || g.id }))}
          />
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
          <div className="support-roster__label" id="support-roster-label">
            <span className="field__label field__label--sub support-roster__heading-primary">
              GitHub support team
            </span>
            <span className="support-roster__spec">({supportTeamSpec})</span>
          </div>
          {supportTeamHint ? (
            <p className="field__hint field__hint--warn" role="status">
              {supportTeamHint}
            </p>
          ) : supportTeam.length > 0 ? (
            <ul
              className="support-roster ui-scroll--translucent"
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
            Contributor (Plaky member)
          </label>
          <AppSelect
            id="eng-assign"
            value={engPick}
            onChange={setEngPick}
            emptyLabel="None"
            options={workspaceUsers.map((u) => ({
              value: u.id,
              label: u.name?.trim() || u.id,
            }))}
          />
          <label className="field__label field__label--sub" htmlFor="qa-assign">
            QA (Plaky member)
          </label>
          <AppSelect
            id="qa-assign"
            value={qaPick}
            onChange={setQaPick}
            emptyLabel="None / use auto only"
            options={workspaceUsers.map((u) => ({
              value: u.id,
              label: u.name?.trim() || u.id,
            }))}
          />
          {usersHint ? <p className="field__hint field__hint--warn">{usersHint}</p> : null}
          <button
            type="button"
            className="field__button"
            disabled={createBusy || !createTitle.trim() || selectedRepos.length === 0}
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
              <strong>Deepiri</strong> Board Manager Agent
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
                {loading && messages[messages.length - 1]?.role !== "assistant" ? (
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
                {loading && messages[messages.length - 1]?.role !== "assistant" ? (
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
