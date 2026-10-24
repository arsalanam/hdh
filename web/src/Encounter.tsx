import { useRef, useState } from "react";
import {
  recordEncounter,
  saveEncounter,
  saveNote,
  type Encounter as Recorder,
  type EncounterResult,
  type StreamSegment,
} from "./api";
import MicButton from "./MicButton";

// The patient this encounter is for — the UI's selected-patient context.
type Patient = { mrn: string; name: string };

type Props = {
  patient: Patient;
  // Run an agent ask in the chat stream (App carries the active MRN).
  onAsk: (question: string) => void;
  // Push a one-line notice into the chat stream (save confirmations / errors).
  onNotice: (text: string, status?: string, isError?: boolean) => void;
};

// One diarized turn, editable in the review stage.
type Turn = { speaker: string; text: string; needs_review: boolean };

type Phase = "idle" | "recording" | "review" | "saving" | "saved";

const PROVIDER = "Provider";
const PATIENT = "Patient";

/** The encounter workspace: record a diarized conversation, stop and edit it,
 *  add an optional provider note, save it as the patient's current encounter,
 *  then ask the agent to update the chart from it. Every step is a click here;
 *  dictation into the note field arrives in phase B. */
export default function Encounter({ patient, onAsk, onNotice }: Props) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [recorder, setRecorder] = useState<Recorder | null>(null);
  const [turns, setTurnsState] = useState<Turn[]>([]);
  const turnsRef = useRef<Turn[]>([]); // the latest turns, for the streaming + save closures
  const [result, setResult] = useState<EncounterResult | null>(null);
  const [note, setNote] = useState("");

  const setTurns = (next: Turn[]) => {
    turnsRef.current = next;
    setTurnsState(next);
  };

  async function start() {
    setTurns([]);
    setResult(null);
    setNote("");
    const session = await recordEncounter({
      onSegment: (s: StreamSegment) =>
        setTurns([...turnsRef.current, { speaker: s.speaker, text: s.text, needs_review: s.needs_review }]),
      onDone: (r: EncounterResult) => {
        setRecorder(null);
        setResult(r);
        if (turnsRef.current.length) {
          setPhase("review"); // stop → edit, never auto-save
        } else {
          setPhase("idle");
          onNotice("Nothing was transcribed.", undefined, true);
        }
      },
      onError: (detail: string) => {
        setRecorder(null);
        setPhase("idle");
        onNotice(detail, undefined, true);
      },
    });
    if (session) {
      setRecorder(session);
      setPhase("recording");
    }
  }

  const stop = () => recorder?.stop(); // the done frame flips us to review

  // ── review edits ──
  const editTurn = (i: number, text: string) =>
    setTurns(turnsRef.current.map((t, j) => (j === i ? { ...t, text } : t)));
  const relabel = (i: number) =>
    setTurns(
      turnsRef.current.map((t, j) =>
        j === i ? { ...t, speaker: t.speaker === PROVIDER ? PATIENT : PROVIDER } : t,
      ),
    );
  const dropTurn = (i: number) => setTurns(turnsRef.current.filter((_, j) => j !== i));

  function reset() {
    setPhase("idle");
    setTurns([]);
    setResult(null);
    setNote("");
  }

  async function confirmSave() {
    if (!result) return;
    const kept = turnsRef.current.filter((t) => t.text.trim());
    if (!kept.length) {
      onNotice("Nothing to save — every turn is empty.", undefined, true);
      return;
    }
    const transcript = kept.map((t) => `${t.speaker}: ${t.text.trim()}`).join("\n");
    const trimmedNote = note.trim();
    const combined = trimmedNote ? `${transcript}\n\n--- PROVIDER NOTE ---\n${trimmedNote}` : transcript;
    setPhase("saving");
    // Save the standalone note FIRST (older), so the combined encounter stays the
    // newest dictation — the one `use_last_dictation` pulls to chart.
    if (trimmedNote) {
      const saved = await saveNote(trimmedNote, patient.mrn);
      if ("error" in saved) {
        onNotice(`Could not save the note: ${saved.error}`, undefined, true);
        setPhase("review");
        return;
      }
    }
    const segments: StreamSegment[] = kept.map((t) => ({
      speaker: t.speaker,
      text: t.text.trim(),
      t0: 0,
      t1: 0,
      final: true,
      confidence: 1,
      needs_review: t.needs_review,
    }));
    const saved = await saveEncounter({ ...result, text: combined }, patient.mrn, segments);
    if ("error" in saved) {
      onNotice(`Could not save the encounter: ${saved.error}`, undefined, true);
      setPhase("review");
      return;
    }
    const needsReview = kept.some((t) => t.needs_review);
    onNotice(
      `Encounter saved for ${patient.name} (${kept.length} turn${kept.length === 1 ? "" : "s"}` +
        (trimmedNote ? " + note" : "") +
        ")" +
        (needsReview ? " — some turns need review" : "") +
        ".",
      needsReview ? "review" : "validated",
    );
    setPhase("saved");
  }

  function updateChart() {
    onAsk(
      `Update ${patient.name}'s chart from the latest recorded encounter (MRN ${patient.mrn}): ` +
        `chart the history and draft any orders for review.`,
    );
    reset();
  }

  const recording = phase === "recording";
  const editing = phase === "review" || phase === "saving";

  return (
    <section className="encounter-panel">
      <header className="encounter-head">
        {recording && <span className="rec-dot" />}
        <span className="encounter-title">
          Encounter · {patient.name}
          {recording ? " — recording, diarized live" : editing ? " — review & edit" : ""}
        </span>
        {phase === "idle" && (
          <button type="button" className="record-btn" onClick={start}>
            🎙️ Record encounter
          </button>
        )}
        {recording && (
          <button type="button" className="record-btn recording" onClick={stop}>
            ⏹ Stop
          </button>
        )}
      </header>

      {(recording || editing) && (
        <div className="encounter-scroll">
          {turns.length === 0 && <div className="placeholder">Listening…</div>}
          {turns.map((t, i) => (
            <div key={i} className={"turn speaker-" + t.speaker.toLowerCase()}>
              {editing ? (
                <>
                  <button
                    type="button"
                    className="who relabel"
                    title="Switch Provider / Patient"
                    onClick={() => relabel(i)}
                  >
                    {t.speaker}
                  </button>
                  <textarea
                    className="turn-edit"
                    rows={1}
                    value={t.text}
                    onChange={(e) => editTurn(i, e.target.value)}
                  />
                  <button type="button" className="turn-drop" title="Delete turn" onClick={() => dropTurn(i)}>
                    ✕
                  </button>
                </>
              ) : (
                <>
                  <span className="who">{t.speaker}</span>
                  <span className="said">{t.text}</span>
                  {t.needs_review && <span className="flag">review</span>}
                </>
              )}
            </div>
          ))}
        </div>
      )}

      {editing && (
        <div className="encounter-note">
          <div className="encounter-note-head">
            <label htmlFor="provider-note">Provider note (optional)</label>
            <MicButton
              onText={(t) => setNote((n) => (n.trim() ? `${n.trimEnd()} ${t}` : t))}
              disabled={phase === "saving"}
              title="Hold to dictate the note"
            />
          </div>
          <textarea
            id="provider-note"
            rows={2}
            placeholder="Add a note — type or hold 🎤 to dictate…"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
        </div>
      )}

      {phase === "review" && (
        <div className="encounter-actions">
          <button type="button" className="confirm" onClick={confirmSave}>
            Save encounter
          </button>
          <button type="button" className="ghost" onClick={reset}>
            Discard
          </button>
        </div>
      )}
      {phase === "saving" && <div className="encounter-actions muted">Saving…</div>}

      {phase === "saved" && (
        <div className="encounter-actions">
          <button type="button" className="confirm" onClick={updateChart}>
            Update the chart from this encounter
          </button>
          <button type="button" className="ghost" onClick={reset}>
            New recording
          </button>
        </div>
      )}
    </section>
  );
}
