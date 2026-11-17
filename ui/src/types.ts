// ---------------------------------------------------------------------------
// OpenAI-compatible streaming chunk types
// ---------------------------------------------------------------------------

export type ProgressStatus = "searching" | "writing";

export type GroundingStatus =
  | "SUPPORTED"
  | "PARTIALLY_SUPPORTED"
  | "NOT_SUPPORTED"
  /** General identity guidance (OAuth, OIDC, SAML, other vendors), not from the docs. */
  | "GENERAL";

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

/** GET /chat/config: limits the API also enforces (422 with a `detail`). */
export interface ChatConfig {
  max_prompt_tokens: number;
  /** The token limit as characters (~4 per token). */
  max_prompt_chars: number;
  /** Whether answers can be rated (POST /chat/feedback). */
  feedback_enabled: boolean;
  /** How long questions and answers are kept; null when they aren't. */
  analytics_retention_days: number | null;
}

export type Rating = "up" | "down";

/** POST /chat/feedback; a null rating clears it. */
export interface FeedbackRequest {
  turn_id: string;
  rating: Rating | null;
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
  /** The stream chunks' `id`: what a rating refers to */
  turnId?: string;
  rating?: Rating | null;
  /** True while a rating is being saved */
  ratingPending?: boolean;
}
