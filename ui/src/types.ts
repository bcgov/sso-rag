// ---------------------------------------------------------------------------
// OpenAI-compatible streaming chunk types
// ---------------------------------------------------------------------------

export type ProgressStatus = "searching" | "writing";

export type GroundingStatus =
  | "SUPPORTED"
  | "PARTIALLY_SUPPORTED"
  | "NOT_SUPPORTED";

export interface DeltaContent {
  role?: "assistant" | "user";
  content?: string;
  citations?: Citation[];
  /** Progress while the answer is prepared and checked (before content). */
  status?: ProgressStatus;
  /** How well the documentation supports the answer (after content). */
  grounding_status?: GroundingStatus;
}

export interface StreamChoice {
  index: number;
  delta: DeltaContent;
  finish_reason: "stop" | null;
}

export interface ChatCompletionChunk {
  id: string;
  object: "chat.completion.chunk";
  created: number;
  model: string;
  choices: StreamChoice[];
}

// ---------------------------------------------------------------------------
// /chat request / response
// ---------------------------------------------------------------------------

export interface ChatRequest {
  prompt: string;
}

/** In-band error event sent if the stream fails after it has started. */
export interface StreamError {
  error: string;
}

// ---------------------------------------------------------------------------
// Citation shape returned by the API
// ---------------------------------------------------------------------------

export interface CitationLocation {
  uri?: string;
  pageNumber?: number;
}

export interface Citation {
  /** Page / section title of the source */
  title?: string;
  content?: string;
  location?: CitationLocation;
  score?: number;
}

// ---------------------------------------------------------------------------
// UI message model
// ---------------------------------------------------------------------------

export type MessageRole = "user" | "assistant";

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  citations?: Citation[];
  /** True while the assistant is still streaming */
  streaming?: boolean;
  /** Latest progress update, shown until the answer text arrives */
  status?: ProgressStatus;
  groundingStatus?: GroundingStatus;
}
