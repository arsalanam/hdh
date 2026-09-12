import { useEffect, useState, type FormEvent } from "react";
import {
  askStream,
  getConversation,
  getMe,
  listThreads,
  type Answer,
  type Me,
  type ThreadSummary,
} from "./api";
import { currentUser, login, logout } from "./auth";

// One question and what came back — the unit the chat column renders.
type Exchange = {
  question: string;
  answer?: string;
  status?: string;
  verdict?: { valid: boolean; reason?: string } | null;
  error?: string;
};

function newThreadId(): string {
  return (globalThis.crypto?.randomUUID?.() ?? `t-${Date.now()}-${Math.random().toString(16).slice(2)}`);
}

export default function App() {
  const [question, setQuestion] = useState("");
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [stage, setStage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  // the thread new questions are filed under (a fresh one is always ready)
  const [activeThread, setActiveThread] = useState<string>(newThreadId);
  // which threads are expanded in the tree; the active one is, by default
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  // the run currently shown in the main panel, if viewing history
  const [viewingRun, setViewingRun] = useState<string | null>(null);
  // auth: who is signed in, and whether the sign-in check has completed
  const [me, setMe] = useState<Me | null>(null);
  const [authReady, setAuthReady] = useState(false);

  const refreshThreads = () => listThreads().then(setThreads).catch(() => undefined);
  useEffect(() => {
    currentUser()
      .then(async (user) => {
        if (user) {
          setMe(await getMe());
          refreshThreads();
        }
      })
      .finally(() => setAuthReady(true));
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const q = question.trim();
    if (!q || busy) return;
    setViewingRun(null);
    setQuestion("");
    setBusy(true);
    setStage("Starting…");
    setExpanded((e) => new Set(e).add(activeThread)); // keep the active thread open
    const index = exchanges.length;
    setExchanges((xs) => [...xs, { question: q }]);

    const patch = (fields: Partial<Exchange>) =>
      setExchanges((xs) => xs.map((x, i) => (i === index ? { ...x, ...fields } : x)));

    await askStream(q, activeThread, {
      onStage: (s) => setStage(s.label),
      onAnswer: (a: Answer) => {
        patch({ answer: a.answer, status: a.status, verdict: a.verdict });
        setStage(null);
        setBusy(false);
        refreshThreads();
      },
      onError: (e) => {
        patch({ error: e.detail });
        setStage(null);
        setBusy(false);
      },
    });
  }

  async function openRun(runId: string) {
    const transcript = await getConversation(runId);
    if (!transcript) return;
    setViewingRun(runId);
    setExchanges(
      transcript.turns.map((t) => ({
        question: t.question,
        answer: t.answer ?? undefined,
        status: t.status,
      })),
    );
  }

  function startNewConversation() {
    setActiveThread(newThreadId());
    setExchanges([]);
    setViewingRun(null);
    setStage(null);
  }

  function toggleThread(threadId: string) {
    setExpanded((e) => {
      const next = new Set(e);
      next.has(threadId) ? next.delete(threadId) : next.add(threadId);
      return next;
    });
  }

  const isOpen = (threadId: string) => expanded.has(threadId) || threadId === activeThread;

  if (!authReady) {
    return <div className="gate">Loading…</div>;
  }
  if (!me) {
    return (
      <div className="gate">
        <div className="gate-card">
          <div className="brand">HDH Agent</div>
          <p>Sign in with your provider account to continue.</p>
          <button className="signin" onClick={() => login()}>
            Sign in
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">HDH Agent</div>
        <button className="new" onClick={startNewConversation}>
          + New conversation
        </button>
        <div className="history">
          {threads.map((t) => (
            <div key={t.thread_id} className="thread">
              <button
                className={"thread-head" + (t.thread_id === activeThread ? " current" : "")}
                onClick={() => toggleThread(t.thread_id)}
                aria-expanded={isOpen(t.thread_id)}
              >
                <span className="caret">{isOpen(t.thread_id) ? "▾" : "▸"}</span>
                <span className="thread-title">{t.title}</span>
                <span className="thread-count">{t.runs.length}</span>
              </button>
              {isOpen(t.thread_id) && (
                <div className="runs">
                  {t.runs.map((r) => (
                    <button
                      key={r.conversation_id}
                      className={"run" + (viewingRun === r.conversation_id ? " active" : "")}
                      onClick={() => openRun(r.conversation_id)}
                      title={r.started_at}
                    >
                      {r.title}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ))}
          {threads.length === 0 && <div className="empty">No conversations yet.</div>}
        </div>
        <div className="account">
          <span className="who">{me.username}</span>
          <button className="signout" onClick={() => logout()}>
            Sign out
          </button>
        </div>
      </aside>

      <main className="chat">
        <div className="stream">
          {exchanges.length === 0 && (
            <div className="placeholder">
              Ask about a patient, the panel, or how care is being delivered.
            </div>
          )}
          {exchanges.map((x, i) => (
            <div key={i} className="exchange">
              <div className="bubble q">{x.question}</div>
              {x.answer !== undefined && (
                <div className={"bubble a status-" + (x.status ?? "")}>
                  <div className="answer-text">{x.answer}</div>
                  {x.verdict && (
                    <div className={"verdict" + (x.verdict.valid ? " ok" : " warn")}>
                      {x.verdict.valid ? "grounded ✓" : "unvalidated — treat with caution"}
                    </div>
                  )}
                </div>
              )}
              {x.error && <div className="bubble a error">⚠ {x.error}</div>}
              {x.answer === undefined && !x.error && i === exchanges.length - 1 && busy && (
                <div className="working">
                  <span className="dot" /> {stage ?? "…"}
                </div>
              )}
            </div>
          ))}
        </div>

        <form className="composer" onSubmit={submit}>
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder={
              viewingRun
                ? "Viewing a past run — type to ask in the current conversation"
                : "Ask the agent…"
            }
            disabled={busy}
            autoFocus
          />
          <button type="submit" disabled={busy || !question.trim()}>
            {busy ? "…" : "Ask"}
          </button>
        </form>
      </main>
    </div>
  );
}
