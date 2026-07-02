// ---------------------------------------------------------------------------
// OpenAI-compatible streaming chunk types
// ---------------------------------------------------------------------------

export interface DeltaContent {
  role?: "assistant" | "user";
  content?: string;
  citations?: Citation[];
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
// Citation shape returned by the API
// ---------------------------------------------------------------------------

export interface CitationLocation {
  uri?: string;
  pageNumber?: number;
}

export interface Citation {
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
}
