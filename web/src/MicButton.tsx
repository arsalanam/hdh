import { useRef, useState } from "react";
import { transcribeClip } from "./api";

type Props = {
  // Receives the transcribed text; the parent decides how to insert it.
  onText: (text: string) => void;
  disabled?: boolean;
  title?: string;
};

type State = "idle" | "recording" | "transcribing";

/** Push-to-talk: hold to record a clip, release to transcribe it (via /stt) and
 *  hand the text to the parent. The same speech-to-text path everywhere a field
 *  takes typed input — dictation is additive, never a separate mode. A missing
 *  mic or a failed transcription is a silent no-op; typing is unaffected. */
export default function MicButton({ onText, disabled, title }: Props) {
  const [state, setState] = useState<State>("idle");
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  async function startHold() {
    if (disabled || state !== "idle") return;
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      return; // mic unavailable — dictation is simply off, typing still works
    }
    chunksRef.current = [];
    const recorder = new MediaRecorder(stream);
    recorder.ondataavailable = (e) => {
      if (e.data.size) chunksRef.current.push(e.data);
    };
    recorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(chunksRef.current, { type: recorder.mimeType || "audio/webm" });
      if (!blob.size) {
        setState("idle");
        return;
      }
      setState("transcribing");
      const text = await transcribeClip(blob);
      if (text) onText(text);
      setState("idle");
    };
    recorderRef.current = recorder;
    recorder.start();
    setState("recording");
  }

  function endHold() {
    if (state === "recording") recorderRef.current?.stop(); // onstop transcribes
  }

  const glyph = state === "recording" ? "●" : state === "transcribing" ? "…" : "🎤";
  return (
    <button
      type="button"
      className={"mic-btn" + (state === "recording" ? " recording" : "")}
      onMouseDown={startHold}
      onMouseUp={endHold}
      onMouseLeave={endHold}
      onTouchStart={(e) => {
        e.preventDefault();
        startHold();
      }}
      onTouchEnd={(e) => {
        e.preventDefault();
        endHold();
      }}
      disabled={disabled || state === "transcribing"}
      title={title ?? "Hold to dictate"}
      aria-label="Hold to dictate"
    >
      {glyph}
    </button>
  );
}
