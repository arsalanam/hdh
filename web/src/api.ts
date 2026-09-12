// The browser's view of the hdh agent API (design agentic-ui-module.md §4).
// Same-origin: in production FastAPI serves this SPA and the API together; in
// dev, vite proxies the API routes to the backend.

export type Stage = { stage: string; label: string; detail?: string };
export type Answer = {
  answer: string;
  status: string; // validated | unvalidated | rejected
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
): Promise<void> {
  let resp: Response;
  try {
    resp = await fetch("/ask/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, thread_id: threadId }),
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

export async function listThreads(): Promise<ThreadSummary[]> {
  const resp = await fetch("/threads");
  return resp.ok ? ((await resp.json()) as ThreadSummary[]) : [];
}

export async function listConversations(): Promise<ConversationSummary[]> {
  const resp = await fetch("/conversations");
  return resp.ok ? ((await resp.json()) as ConversationSummary[]) : [];
}

export async function getConversation(id: string): Promise<Transcript | null> {
  const resp = await fetch(`/conversations/${encodeURIComponent(id)}`);
  return resp.ok ? ((await resp.json()) as Transcript) : null;
}
