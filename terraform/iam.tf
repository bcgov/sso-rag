data "aws_iam_policy_document" "ecs_tasks_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ── ECS Task Execution Role ───────────────────────────────────────────────────
# Used by the ECS agent to pull images and send logs to CloudWatch.

resource "aws_iam_role" "ecs_task_execution" {
  name               = "${local.name_prefix}-ecs-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_managed" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ── API Task Role ─────────────────────────────────────────────────────────────
# Granted to the API container at runtime; needs Bedrock access.

resource "aws_iam_role" "api_task" {
  name               = "${local.name_prefix}-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "api_bedrock" {
  # search_knowledge_base tool and /documents/search
  statement {
    sid       = "KnowledgeBaseRetrieve"
    effect    = "Allow"
    actions   = ["bedrock:Retrieve"]
    resources = ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:knowledge-base/${var.knowledge_base_id}"]
  }

  # Agent model (Converse / ConverseStream) and reranker model. Cross-region
  # inference profiles route to foundation models in other regions, so model
  # resources are not pinned to one region.
  statement {
    sid    = "InvokeModels"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:GetInferenceProfile",
    ]
    resources = ["*"]
  }

  # Rerank does not support resource-level permissions
  statement {
    sid       = "Rerank"
    effect    = "Allow"
    actions   = ["bedrock:Rerank"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "api_bedrock" {
  name   = "bedrock-access"
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api_bedrock.json
}

# ── UI Task Role ──────────────────────────────────────────────────────────────
# Minimal role; UI container only serves static files and proxies to the API.

resource "aws_iam_role" "ui_task" {
  name               = "${local.name_prefix}-ui-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

# agent storage s3 bucket permissions for the api task role
resource "aws_iam_policy" "api_agent_storage" {
  name        = "${local.name_prefix}-api-agent-storage"
  description = "Allow API task role to access the agent storage S3 bucket"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket"
        ]
        Resource = [
          "arn:aws:s3:::${var.environment}-${local.name_prefix}-agent-context-store",
          "arn:aws:s3:::${var.environment}-${local.name_prefix}-agent-context-store/*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "api_agent_storage" {
  role       = aws_iam_role.api_task.name
  policy_arn = aws_iam_policy.api_agent_storage.arn
}

# ── Chat turns and ratings (terraform/dynamodb.tf) ────────────────────────────

data "aws_iam_policy_document" "api_interactions" {
  # PutItem: turn records. GetItem: the owner check before a rating.
  # UpdateItem / DeleteItem: setting and clearing a rating. Query: all
  # ratings of one turn. The gsi1 index is maintained by DynamoDB.
  statement {
    sid    = "TurnsAndRatings"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:GetItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
    ]
    resources = [aws_dynamodb_table.interactions.arn]
  }
}

resource "aws_iam_role_policy" "api_interactions" {
  name   = "interactions-access"
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api_interactions.json
}

# Read-only access for people reviewing ratings with
# `python -m app.jobs.feedback_report` (and ad hoc scans). Not attached to the
# API task: attach it to the role or permission set of whoever reviews.
data "aws_iam_policy_document" "feedback_reader" {
  statement {
    sid       = "ListRatedTurns"
    effect    = "Allow"
    actions   = ["dynamodb:Query"]
    resources = ["${aws_dynamodb_table.interactions.arn}/index/gsi1"]
  }

  statement {
    sid    = "ReadTurns"
    effect = "Allow"
    actions = [
      "dynamodb:BatchGetItem",
      "dynamodb:Scan",
    ]
    resources = [aws_dynamodb_table.interactions.arn]
  }
}

resource "aws_iam_policy" "feedback_reader" {
  name        = "${local.name_prefix}-feedback-reader"
  description = "Read rated chat turns from the interactions table"
  policy      = data.aws_iam_policy_document.feedback_reader.json
}
