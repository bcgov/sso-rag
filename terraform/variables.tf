variable "web_subnet_a" {
  type        = string
  description = "Value of the name tag for the web subnet in AZ a"
  default     = "Dev-Web-MainTgwAttach-A"
}

variable "web_subnet_b" {
  type        = string
  description = "Value of the name tag for the web subnet in AZ b"
  default     = "Dev-Web-MainTgwAttach-B"
}

variable "subnet_a" {
  type        = string
  description = "Value of the name tag for the app subnet in AZ a"
  default     = "Dev-App-A"
}

variable "subnet_b" {
  type        = string
  description = "Value of the name tag for the app subnet in AZ b"
  default     = "Dev-App-B"
}

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "ca-central-1"
}

variable "project_name" {
  description = "Short project identifier used in resource names"
  type        = string
  default     = "sso-rag"
}

variable "environment" {
  description = "Deployment environment (sandbox, prod)"
  type        = string
  default     = "sandbox"
}

# ── Networking ────────────────────────────────────────────────────────────────

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

# ── Bedrock ───────────────────────────────────────────────────────────────────

variable "knowledge_base_id" {
  description = "AWS Bedrock Knowledge Base ID used by the API"
  type        = string
}

variable "model_arn" {
  description = "ARN of the Bedrock model or inference profile the agent uses"
  type        = string
  default     = "arn:aws:bedrock:ca-central-1:820251787117:inference-profile/global.amazon.nova-2-lite-v1:0"
}

variable "model_max_tokens" {
  description = "Output-token limit per agent model call; a reply cut off at it gets one repair attempt asking for a shorter answer"
  type        = string
  default     = "4096"
}

variable "grounding_repair_enabled" {
  description = "Give a reply that fails the grounding checks one rewrite before falling back to the no-documentation reply"
  type        = string
  default     = "true"
}

variable "reranker_model_arn" {
  description = "ARN of the Bedrock foundation model used for Reranking"
  type        = string
  default     = "arn:aws:bedrock:ca-central-1::foundation-model/amazon.rerank-v1:0"
}

variable "number_of_reranked_results" {
  description = "Results kept after reranking (+2 per extra question part, max 12)"
  type        = string
  default     = "6"
}

variable "query_rewriter_model_id" {
  description = "Bedrock model id of the query rewriter (Converse API); must be enabled in Bedrock model access"
  type        = string
  default     = "meta.llama3-8b-instruct-v1:0"
}

variable "query_rewriter_timeout_seconds" {
  description = "Timeout for each query rewriter call; on timeout the search falls back to the raw prompt"
  type        = string
  default     = "3"
}

variable "query_rewriter_history_turns" {
  description = "Conversation turns given to the query rewriter to resolve follow-up questions"
  type        = string
  default     = "3"
}

variable "query_rewriter_step_back_enabled" {
  description = "Add a broader step-back query to each search"
  type        = string
  default     = "true"
}

variable "query_rewriter_decomposition_enabled" {
  description = "Split multi-part questions into 2-4 sub-queries"
  type        = string
  default     = "true"
}

variable "query_rewriter_retry_enabled" {
  description = "Reword queries that found nothing and search once more"
  type        = string
  default     = "true"
}

variable "number_of_results" {
  description = "Total candidates retrieved per search tool call, split across queries"
  type        = string
  default     = "25"
}

variable "min_rerank_score" {
  description = "Drop reranked results below this relevance score"
  type        = string
  default     = "0.05"
}

variable "search_type" {
  description = "Bedrock KB overrideSearchType; S3 Vectors supports SEMANTIC only"
  type        = string
  default     = "SEMANTIC"
}

# ── ECS ───────────────────────────────────────────────────────────────────────

variable "api_image" {
  description = "Full container image URI for the API service (e.g. 123456789012.dkr.ecr.ca-central-1.amazonaws.com/sso-rag-api:latest)"
  type        = string
  default     = ""
}

variable "ui_image" {
  description = "Full container image URI for the UI service (e.g. 123456789012.dkr.ecr.ca-central-1.amazonaws.com/sso-rag-ui:latest)"
  type        = string
  default     = ""
}

variable "api_cpu" {
  description = "vCPU units for the API Fargate task (1 vCPU = 1024)"
  type        = number
  default     = 512
}

variable "api_memory" {
  description = "Memory (MiB) for the API Fargate task (2 uvicorn workers)"
  type        = number
  default     = 1024
}

variable "ui_cpu" {
  description = "vCPU units for the UI Fargate task"
  type        = number
  default     = 256
}

variable "ui_memory" {
  description = "Memory (MiB) for the UI Fargate task"
  type        = number
  default     = 512
}

variable "api_desired_count" {
  description = "Number of API Fargate tasks to run"
  type        = number
  default     = 1
}

variable "ui_desired_count" {
  description = "Number of UI Fargate tasks to run"
  type        = number
  default     = 1
}
