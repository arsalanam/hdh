// The browser's view of the hdh agent API (design agentic-ui-module.md §4).
// Same-origin: in production FastAPI serves this SPA and the API together; in
// dev, vite proxies the API routes to the backend.

import { accessToken } from "./auth";

// Every call carries the signed-in provider's bearer token; the backend
// verifies it and refuses anything without a valid provider-realm token.
async function authHeaders(extra: Record<string, string> = {}): Promise<HeadersInit> {
  const token = await accessToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra;
}

export type Me = { subject: string; username: string; roles: string[] };

export type Stage = { stage: string; label: string; detail?: string };
export type Answer = {
  answer: string;
  status: string; // validated | unvalidated | rejected
  spoken?: string | null; // concise grounded rendering to voice, when requested
  intent?: unknown;
  verdict?: { valid: boolean; reason?: string } | null;
  usage?: Record<string, number>;
  trace_id?: string;
  thread_id?: string;
};
export type ConversationSummary = {
  conversation_id: string;
  started_at: string;
  title: string;
  turns: number;
};
export type ThreadRun = {
  conversation_id: string;
  started_at: string;
  title: string;
  turns: number;
};
export type ThreadSummary = {
  thread_id: string;
  title: string;
  started_at: string;
  runs: ThreadRun[];
};
export type TranscriptTurn = {
  turn_index: number;
  question: string;
  answer: string | null;
  status: string;
};
export type Transcript = {
  conversation_id: string;
  started_at: string;
  model?: string;
  turns: TranscriptTurn[];
};

type StreamHandlers = {
  onStage: (s: Stage) => void;
  onAnswer: (a: Answer) => void;
  onError: (e: { detail: string }) => void;
};

// POST /ask/stream, reading the SSE frames off the response body. EventSource
// only does GET, so we stream the POST response ourselves.
export async function askStream(
  question: string,
  threadId: string,
  handlers: StreamHandlers,
  wantSpoken = false,
  mrn: string | null = null,
): Promise<void> {
  let resp: Response;
  try {
    resp = await fetch("/ask/stream", {
      method: "POST",
      headers: await authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ question, thread_id: threadId, want_spoken: wantSpoken, mrn }),
    });
  } catch (err) {
    handlers.onError({ detail: `network error: ${String(err)}` });
    return;
  }
  if (!resp.ok || !resp.body) {
    handlers.onError({ detail: `HTTP ${resp.status}` });
    return;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() ?? "";
    for (const block of blocks) dispatch(block, handlers);
  }
  if (buffer.trim()) dispatch(buffer, handlers);
}

function dispatch(block: string, handlers: StreamHandlers): void {
  let event = "message";
  let data = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("event: ")) event = line.slice(7).trim();
    else if (line.startsWith("data: ")) data = line.slice(6);
  }
  if (!data) return;
  const payload = JSON.parse(data);
  if (event === "stage") handlers.onStage(payload as Stage);
  else if (event === "answer") handlers.onAnswer(payload as Answer);
  else if (event === "error") handlers.onError(payload as { detail: string });
}

export type NoteVerdict = { action: string; kind: string; detail: string };
export type NoteUploadResult = {
  mrn: string;
  visit_id: number;
  created_visit: boolean;
  needs_review: boolean;
  chars: number;
  verdicts: NoteVerdict[];
};

/** Upload a note (image/PDF/text) onto a patient's chart — multipart; the
 *  browser sets the boundary, so we attach only the bearer token. */
export async function uploadNote(mrn: string, file: File): Promise<NoteUploadResult | { error: string }> {
  const form = new FormData();
  form.append("mrn", mrn);
  form.append("file", file);
  const resp = await fetch("/notes/upload", { method: "POST", headers: await authHeaders(), body: form });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({ detail: `HTTP ${resp.status}` }));
    return { error: detail.detail || `HTTP ${resp.status}` };
  }
  return resp.json();
}

export async function getMe(): Promise<Me | null> {
  const resp = await fetch("/me", { headers: await authHeaders() });
  return resp.ok ? ((await resp.json()) as Me) : null;
}

export type PatientInfo = { mrn: string; name: string; age?: number | null; sex?: string | null };

/** Resolve the UI's selected patient by MRN — a small card to confirm and show
 *  the active patient. null when the MRN names no patient (or on any error). */
export async function getPatient(mrn: string): Promise<PatientInfo | null> {
  const resp = await fetch(`/patients/${encodeURIComponent(mrn)}`, { headers: await authHeaders() });
  return resp.ok ? ((await resp.json()) as PatientInfo) : null;
}

export async function listThreads(): Promise<ThreadSummary[]> {
  const resp = await fetch("/threads", { headers: await authHeaders() });
  return resp.ok ? ((await resp.json()) as ThreadSummary[]) : [];
}

export async function listConversations(): Promise<ConversationSummary[]> {
  const resp = await fetch("/conversations", { headers: await authHeaders() });
  return resp.ok ? ((await resp.json()) as ConversationSummary[]) : [];
}

export async function getConversation(id: string): Promise<Transcript | null> {
  const resp = await fetch(`/conversations/${encodeURIComponent(id)}`, { headers: await authHeaders() });
  return resp.ok ? ((await resp.json()) as Transcript) : null;
}

// ── the live encounter (streaming STT over the media service's WebSocket) ──────

export type StreamSegment = {
  speaker: string;
  text: string;
  t0: number;
  t1: number;
  final: boolean;
  confidence: number;
  needs_review: boolean;
};

export type EncounterResult = {
  text: string;
  speakers: string[];
  duration_s: number;
  needs_review: boolean;
  enrolled: number[] | null; // the re-enrolled provider print, for us to persist
};

type EncounterHandlers = {
  onSegment: (s: StreamSegment) => void;
  onDone: (d: EncounterResult) => void;
  onError: (detail: string) => void;
};

// A handle on a running encounter: call stop() when the clinician ends the
// visit — it closes the mic and asks the server for the final transcript.
export type Encounter = { stop: () => void };

// The media service runs on its own host (design §5). VITE_MEDIA_URL points at
// it; empty means same-origin (dev: vite proxies /stt/stream → :8200).
function mediaWsUrl(path: string): string {
  const base = (import.meta.env.VITE_MEDIA_URL as string) || window.location.origin;
  const url = new URL(path, base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

/** Record a live, diarized encounter: capture mic audio in clips, push them to
 *  the media service, and surface the diarized segments as they return. The
 *  bearer token rides in the `bearer` subprotocol — a WebSocket cannot set an
 *  Authorization header, and the URL must carry no token. */
export async function recordEncounter(handlers: EncounterHandlers): Promise<Encounter | null> {
  const token = await accessToken();
  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    handlers.onError(`microphone unavailable: ${String(err)}`);
    return null;
  }

  const ws = new WebSocket(mediaWsUrl("/stt/stream"), token ? ["bearer", token] : undefined);
  ws.binaryType = "arraybuffer";
  const recorder = new MediaRecorder(stream);

  const teardown = () => {
    stream.getTracks().forEach((t) => t.stop());
    if (recorder.state !== "inactive") recorder.stop();
  };

  ws.onopen = () => {
    ws.send(JSON.stringify({ type: "start", sample_rate: 16000 }));
    recorder.ondataavailable = (e) => {
      if (e.data.size > 0 && ws.readyState === WebSocket.OPEN) e.data.arrayBuffer().then((b) => ws.send(b));
    };
    recorder.start(2000); // a clip every 2s — the server streams segments back
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data as string);
    if (msg.type === "segment") handlers.onSegment(msg as StreamSegment);
    else if (msg.type === "done") {
      handlers.onDone(msg as EncounterResult);
      ws.close();
    } else if (msg.type === "error") handlers.onError(msg.detail);
  };
  ws.onerror = () => handlers.onError("the encounter stream failed");
  ws.onclose = teardown;

  return {
    stop: () => {
      teardown();
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "stop" }));
    },
  };
}

// The media service base URL (same as the streaming encounter's). Empty means
// same-origin (dev: vite proxies /tts → :8200).
function mediaUrl(path: string): string {
  const base = (import.meta.env.VITE_MEDIA_URL as string) || "";
  return base ? new URL(path, base).toString() : path;
}

/** Voice a line of text through the media service (Kokoro). The text is the
 *  agent's grounded `spoken` rendering — the TTS layer only voices it, it
 *  invents nothing. Returns false (and plays nothing) when TTS is unavailable. */
export async function speak(text: string): Promise<boolean> {
  try {
    const resp = await fetch(mediaUrl("/tts"), {
      method: "POST",
      headers: await authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ text }),
    });
    if (!resp.ok) return false;
    const blob = await resp.blob();
    const audio = new Audio(URL.createObjectURL(blob));
    audio.onended = () => URL.revokeObjectURL(audio.src);
    await audio.play();
    return true;
  } catch {
    return false; // no media service, or autoplay blocked — the text line still shows
  }
}

/** Transcribe one recorded audio clip to text via the media service (async
 *  /stt) — the push-to-talk path for dictating into a field. Returns "" on any
 *  failure, so a failed dictation simply inserts nothing and typing still works. */
export async function transcribeClip(blob: Blob): Promise<string> {
  try {
    const form = new FormData();
    form.append("file", blob, "clip.webm"); // the browser sets the multipart boundary
    const resp = await fetch(mediaUrl("/stt"), { method: "POST", headers: await authHeaders(), body: form });
    if (!resp.ok) return "";
    const result = (await resp.json()) as { text?: string };
    return (result.text ?? "").trim();
  } catch {
    return "";
  }
}

/** Persist a finished encounter as the provider's current dictation — the media
 *  service holds nothing, so the UI saves the transcript the moment it ends. */
export async function saveEncounter(
  result: EncounterResult,
  mrn: string,
  segments: StreamSegment[],
): Promise<{ dictation_id: number } | { error: string }> {
  const resp = await fetch("/dictations", {
    method: "POST",
    headers: await authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      text: result.text,
      mrn: mrn || null,
      source: "encounter",
      segments,
      duration_s: result.duration_s,
      needs_review: result.needs_review,
    }),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({ detail: `HTTP ${resp.status}` }));
    return { error: detail.detail || `HTTP ${resp.status}` };
  }
  return resp.json();
}

/** Persist a standalone provider note as its own dictation (source="note") for
 *  provenance. Save this BEFORE the combined encounter so the encounter stays
 *  the newest dictation — the one `use_last_dictation` pulls to chart. */
export async function saveNote(
  text: string,
  mrn: string,
): Promise<{ dictation_id: number } | { error: string }> {
  const resp = await fetch("/dictations", {
    method: "POST",
    headers: await authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ text, mrn: mrn || null, source: "note" }),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({ detail: `HTTP ${resp.status}` }));
    return { error: detail.detail || `HTTP ${resp.status}` };
  }
  return resp.json();
}
