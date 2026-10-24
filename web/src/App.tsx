import { useEffect, useState, type FormEvent } from "react";
import {
  askStream,
  getConversation,
  getMe,
  getPatient,
  listThreads,
  speak,
  uploadNote,
  type Answer,
  type Me,
  type PatientInfo,
  type ThreadSummary,
} from "./api";
import { currentUser, login, logout } from "./auth";
import Encounter from "./Encounter";
import MicButton from "./MicButton";

// Append dictated text to a field's current value, spacing it from what's there.
const appendText = (current: string, dictated: string) =>
  current.trim() ? `${current.trimEnd()} ${dictated}` : dictated;

// One question and what came back — the unit the chat column renders.
type Exchange = {
  question: string;
  answer?: string;
  status?: string;
  spoken?: string | null;
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
  // the selected patient (the active-patient context), the MRN being typed, and lookup state
  const [patient, setPatient] = useState<PatientInfo | null>(null);
  const [mrnInput, setMrnInput] = useState("");
  const [lookingUp, setLookingUp] = useState(false);
  const [uploading, setUploading] = useState(false);
  // voice output: when on, the agent returns a concise `spoken` line and we voice it
  const [voiceOn, setVoiceOn] = useState(false);

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

  // Run one agent ask and render it live. Shared by the composer and the
  // encounter's "update the chart" action; carries the active patient's MRN so
  // the agent resolves "this patient" and the right recorded encounter.
  async function runAsk(q: string) {
    const question = q.trim();
    if (!question || busy) return;
    setViewingRun(null);
    setBusy(true);
    setStage("Starting…");
    setExpanded((e) => new Set(e).add(activeThread));
    const index = exchanges.length;
    setExchanges((xs) => [...xs, { question }]);
    const patch = (fields: Partial<Exchange>) =>
      setExchanges((xs) => xs.map((x, i) => (i === index ? { ...x, ...fields } : x)));

    await askStream(
      question,
      activeThread,
      {
        onStage: (s) => setStage(s.label),
        onAnswer: (a: Answer) => {
          patch({ answer: a.answer, status: a.status, spoken: a.spoken, verdict: a.verdict });
          setStage(null);
          setBusy(false);
          refreshThreads();
          if (voiceOn && a.spoken) speak(a.spoken);
        },
        onError: (e) => {
          patch({ error: e.detail });
          setStage(null);
          setBusy(false);
        },
      },
      voiceOn,
      patient?.mrn ?? null,
    );
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const q = question.trim();
    if (!q || busy) return;
    setQuestion("");
    runAsk(q);
  }

  // A one-line notice in the chat stream (encounter save confirmations / errors).
  function pushNotice(text: string, status?: string, isError = false) {
    setViewingRun(null);
    setExchanges((xs) => [
      ...xs,
      isError ? { question: "🎙️ Encounter", error: text } : { question: "🎙️ Encounter", answer: text, status },
    ]);
  }

  async function selectPatient(event: FormEvent) {
    event.preventDefault();
    const mrn = mrnInput.trim();
    if (!mrn || lookingUp) return;
    setLookingUp(true);
    const found = await getPatient(mrn);
    setLookingUp(false);
    if (!found) {
      pushNotice(`No patient with MRN ${mrn}.`, undefined, true);
      return;
    }
    setPatient(found);
    setMrnInput("");
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

  async function onUploadNote(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = ""; // let the same file be re-picked later
    if (!file || !patient) return;
    setViewingRun(null);
    setUploading(true);
    const label = `📎 ${file.name} → ${patient.name}`;
    const result = await uploadNote(patient.mrn, file);
    setUploading(false);
    if ("error" in result) {
      setExchanges((xs) => [...xs, { question: label, error: result.error }]);
      return;
    }
    const kinds = result.verdicts.map((v) => `${v.action} ${v.kind}`).join(", ") || "nothing new";
    const summary =
      `Charted onto visit #${result.visit_id}${result.created_visit ? " (new encounter)" : ""}: ${kinds}` +
      (result.needs_review ? " — some items need review" : "");
    setExchanges((xs) => [
      ...xs,
      { question: label, answer: summary, status: result.needs_review ? "review" : "validated" },
    ]);
    refreshThreads();
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
        <div className="patient-bar">
          {patient ? (
            <>
              <span className="patient-chip">
                <span className="patient-name">{patient.name}</span>
                <span className="patient-meta">
                  MRN {patient.mrn}
                  {patient.age != null ? ` · ${patient.age}y` : ""}
                  {patient.sex ? ` · ${patient.sex}` : ""}
                </span>
              </span>
              <button type="button" className="patient-clear" onClick={() => setPatient(null)}>
                Change patient
              </button>
            </>
          ) : (
            <form className="patient-select" onSubmit={selectPatient}>
              <input
                className="mrn"
                placeholder="Select a patient — enter MRN"
                value={mrnInput}
                onChange={(e) => setMrnInput(e.target.value)}
              />
              <button type="submit" disabled={!mrnInput.trim() || lookingUp}>
                {lookingUp ? "…" : "Select"}
              </button>
            </form>
          )}
        </div>

        <div className="stream">
          {exchanges.length === 0 && (
            <div className="placeholder">
              {patient
                ? `Record an encounter for ${patient.name}, attach a note, or ask the agent.`
                : "Select a patient to begin, or ask the agent about the panel or the dataset."}
            </div>
          )}
          {exchanges.map((x, i) => (
            <div key={i} className="exchange">
              <div className="bubble q">{x.question}</div>
              {x.answer !== undefined && (
                <div className={"bubble a status-" + (x.status ?? "")}>
                  <div className="answer-text">{x.answer}</div>
                  {x.spoken && (
                    <button className="spoken-line" onClick={() => speak(x.spoken as string)} title="Play">
                      🔊 {x.spoken}
                    </button>
                  )}
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
          {patient && <Encounter patient={patient} onAsk={runAsk} onNotice={pushNotice} />}
        </div>

        {patient && (
          <div className="attach">
            <label className={"attach-btn" + (uploading ? " disabled" : "")}>
              {uploading ? "Uploading…" : "📎 Attach note"}
              <input
                type="file"
                hidden
                accept="image/*,application/pdf,.txt,text/plain"
                disabled={uploading}
                onChange={onUploadNote}
              />
            </label>
            <button
              type="button"
              className={"voice-btn" + (voiceOn ? " on" : "")}
              onClick={() => setVoiceOn((v) => !v)}
              aria-pressed={voiceOn}
              title="Voice the agent's answers aloud"
            >
              {voiceOn ? "🔊 Voice on" : "🔈 Voice off"}
            </button>
          </div>
        )}

        <form className="composer" onSubmit={submit}>
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder={
              viewingRun
                ? "Viewing a past run — type to ask in the current conversation"
                : "Ask the agent — type or hold 🎤 to dictate…"
            }
            disabled={busy}
            autoFocus
          />
          <MicButton
            onText={(t) => setQuestion((q) => appendText(q, t))}
            disabled={busy}
            title="Hold to dictate your question"
          />
          <button type="submit" disabled={busy || !question.trim()}>
            {busy ? "…" : "Ask"}
          </button>
        </form>
      </main>
    </div>
  );
}
