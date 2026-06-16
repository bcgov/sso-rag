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
  description = "Deployment environment (dev, staging, prod)"
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
  description = "ARN of the Bedrock foundation model used for RetrieveAndGenerate"
  type        = string
  default     = "arn:aws:bedrock:ca-central-1::foundation-model/meta.llama3-8b-instruct-v1:0"
}

variable "reranker_model_arn" {
  description = "ARN of the Bedrock foundation model used for Reranking"
  type        = string
  default     = "arn:aws:bedrock:ca-central-1::foundation-model/amazon.rerank-v1:0"
}

variable "number_of_reranked_results" {
  description = "Number of retrieved results to rerank in the API"
  type        = string
  default     = "10"
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
  default     = 256
}

variable "api_memory" {
  description = "Memory (MiB) for the API Fargate task"
  type        = number
  default     = 512
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
