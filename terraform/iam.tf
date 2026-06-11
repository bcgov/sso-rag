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

data "aws_iam_policy_document" "api_bedrock" {
  statement {
    sid    = "BedrockRetrieveAndGenerate"
    effect = "Allow"
    actions = [
      "bedrock:RetrieveAndGenerate",
      "bedrock:RetrieveAndGenerateStream",
      "bedrock:Retrieve",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:InvokeModel",
      "bedrock:Rerank"
    ]
    resources = ["arn:aws:bedrock:${var.aws_region}:*:knowledge-base/${var.knowledge_base_id}"]
  }

  statement {
    sid    = "BedrockKnowledgeBase"
    effect = "Allow"
    actions = [
      "bedrock:GetKnowledgeBase",
      "bedrock:ListDataSources",
      "bedrock:ListKnowledgeBaseDocuments",
      "bedrock:DeleteKnowledgeBaseDocuments",
    ]
    resources = [
      "arn:aws:bedrock:${var.aws_region}:*:knowledge-base/${var.knowledge_base_id}",
    ]
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
