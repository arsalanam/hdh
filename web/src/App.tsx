import { useEffect, useState, type FormEvent } from "react";
import {
  askStream,
  getConversation,
  listConversations,
  type Answer,
  type ConversationSummary,
} from "./api";

// One question and what came back — the unit the chat column renders.
type Exchange = {
  question: string;
  answer?: string;
  status?: string;
  verdict?: { valid: boolean; reason?: string } | null;
  error?: string;
};

export default function App() {
  const [question, setQuestion] = useState("");
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [stage, setStage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState<ConversationSummary[]>([]);
  const [viewing, setViewing] = useState<string | null>(null);

  const refreshHistory = () => listConversations().then(setHistory).catch(() => undefined);
  useEffect(() => {
    refreshHistory();
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const q = question.trim();
    if (!q || busy) return;
    setViewing(null);
    setQuestion("");
    setBusy(true);
    setStage("Starting…");
    const index = exchanges.length;
    setExchanges((xs) => [...xs, { question: q }]);

    const patch = (fields: Partial<Exchange>) =>
      setExchanges((xs) => xs.map((x, i) => (i === index ? { ...x, ...fields } : x)));

    await askStream(q, {
      onStage: (s) => setStage(s.label),
      onAnswer: (a: Answer) => {
        patch({ answer: a.answer, status: a.status, verdict: a.verdict });
        setStage(null);
        setBusy(false);
        refreshHistory();
      },
      onError: (e) => {
        patch({ error: e.detail });
        setStage(null);
        setBusy(false);
      },
    });
  }

  async function openConversation(id: string) {
    const transcript = await getConversation(id);
    if (!transcript) return;
    setViewing(id);
    setExchanges(
      transcript.turns.map((t) => ({
        question: t.question,
        answer: t.answer ?? undefined,
        status: t.status,
      })),
    );
  }

  function startNew() {
    setExchanges([]);
    setViewing(null);
    setStage(null);
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">HDH Agent</div>
        <button className="new" onClick={startNew}>
          + New conversation
        </button>
        <div className="history">
          {history.map((c) => (
            <button
              key={c.conversation_id}
              className={"conv" + (viewing === c.conversation_id ? " active" : "")}
              onClick={() => openConversation(c.conversation_id)}
            >
              <div className="conv-title">{c.title}</div>
              <div className="conv-meta">
                {c.turns} turn{c.turns === 1 ? "" : "s"} · {c.started_at}
              </div>
            </button>
          ))}
          {history.length === 0 && <div className="empty">No conversations yet.</div>}
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
              viewing
                ? "Viewing a past conversation — type to start a new one"
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
