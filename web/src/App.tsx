import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  askStream,
  getConversation,
  getMe,
  listThreads,
  recordEncounter,
  saveEncounter,
  speak,
  uploadNote,
  type Answer,
  type Encounter,
  type Me,
  type StreamSegment,
  type ThreadSummary,
} from "./api";
import { currentUser, login, logout } from "./auth";

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
  // note upload: which patient the note is for, and whether one is in flight
  const [noteMrn, setNoteMrn] = useState("");
  const [uploading, setUploading] = useState(false);
  // live encounter: the running recorder and the diarized turns seen so far
  const [encounter, setEncounter] = useState<Encounter | null>(null);
  const [liveSegments, setLiveSegments] = useState<StreamSegment[]>([]);
  const segmentsRef = useRef<StreamSegment[]>([]); // the latest, for the save on stop
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

    await askStream(
      q,
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
    );
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
    const mrn = noteMrn.trim();
    if (!file || !mrn) return;
    setViewingRun(null);
    setUploading(true);
    const label = `📎 ${file.name} → ${mrn}`;
    const result = await uploadNote(mrn, file);
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

  async function toggleRecording() {
    if (encounter) {
      encounter.stop(); // the done frame fires onDone, which saves and clears
      return;
    }
    setViewingRun(null);
    segmentsRef.current = [];
    setLiveSegments([]);
    const mrn = noteMrn.trim();
    const session = await recordEncounter({
      onSegment: (s) => {
        segmentsRef.current = [...segmentsRef.current, s];
        setLiveSegments(segmentsRef.current);
      },
      onDone: async (result) => {
        setEncounter(null);
        setLiveSegments([]);
        const label = "🎙️ Encounter recorded";
        if (!result.text) {
          setExchanges((xs) => [...xs, { question: label, error: "nothing was transcribed" }]);
          return;
        }
        const saved = await saveEncounter(result, mrn, segmentsRef.current);
        if ("error" in saved) {
          setExchanges((xs) => [...xs, { question: label, error: saved.error }]);
          return;
        }
        const note =
          `Saved as the current dictation (${Math.round(result.duration_s)}s, ` +
          `${result.speakers.join(" + ") || "one speaker"})` +
          (result.needs_review ? " — some turns need review" : "") +
          '. Ask "update the chart now" to draft history and orders.';
        setExchanges((xs) => [
          ...xs,
          { question: label, answer: note, status: result.needs_review ? "review" : "validated" },
        ]);
        refreshThreads();
      },
      onError: (detail) => {
        setEncounter(null);
        setLiveSegments([]);
        setExchanges((xs) => [...xs, { question: "🎙️ Encounter", error: detail }]);
      },
    });
    if (session) setEncounter(session);
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
          {encounter && (
            <div className="encounter">
              <div className="encounter-head">
                <span className="rec-dot" /> Recording — diarized live
              </div>
              {liveSegments.length === 0 && <div className="placeholder">Listening…</div>}
              {liveSegments.map((s, i) => (
                <div key={i} className={"turn speaker-" + s.speaker.toLowerCase()}>
                  <span className="who">{s.speaker}</span>
                  <span className="said">{s.text}</span>
                  {s.needs_review && <span className="flag">review</span>}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="attach">
          <input
            className="mrn"
            placeholder="MRN"
            value={noteMrn}
            onChange={(e) => setNoteMrn(e.target.value)}
          />
          <label className={"attach-btn" + (!noteMrn.trim() || uploading ? " disabled" : "")}>
            {uploading ? "Uploading…" : "📎 Attach note"}
            <input
              type="file"
              hidden
              accept="image/*,application/pdf,.txt,text/plain"
              disabled={!noteMrn.trim() || uploading}
              onChange={onUploadNote}
            />
          </label>
          <button
            type="button"
            className={"record-btn" + (encounter ? " recording" : "")}
            onClick={toggleRecording}
            title={noteMrn.trim() ? "Record an encounter for this patient" : "Record an encounter"}
          >
            {encounter ? "⏹ Stop" : "🎙️ Record"}
          </button>
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
