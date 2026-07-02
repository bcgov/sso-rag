# ── VPC Link ──────────────────────────────────────────────────────────────────
# API Gateway HTTP API uses a VPC Link to reach the private ALB.

resource "aws_apigatewayv2_vpc_link" "main" {
  name               = "${local.name_prefix}-vpc-link"
  security_group_ids = [aws_security_group.alb.id]
  subnet_ids         = [data.aws_subnet.subnet_a.id, data.aws_subnet.subnet_b.id]
}

# ── HTTP API ──────────────────────────────────────────────────────────────────

resource "aws_apigatewayv2_api" "main" {
  name          = "${local.name_prefix}-api-gw"
  protocol_type = "HTTP"
  description   = "HTTP API Gateway for ${var.project_name} (${var.environment})"

  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["Content-Type", "Authorization"]
    max_age       = 300
  }
}

# ── ALB Integration ───────────────────────────────────────────────────────────

resource "aws_apigatewayv2_integration" "alb" {
  api_id             = aws_apigatewayv2_api.main.id
  integration_type   = "HTTP_PROXY"
  integration_method = "ANY"
  integration_uri    = aws_lb_listener.http.arn

  connection_type    = "VPC_LINK"
  connection_id      = aws_apigatewayv2_vpc_link.main.id

  # Forward the original path and query string to the ALB unchanged.
  payload_format_version = "1.0"
}

# ── Catch-all Route ───────────────────────────────────────────────────────────
# All traffic enters via API Gateway and is forwarded to the ALB.
# The ALB then applies its own path-based routing rules.

resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

# ── Stage (auto-deploy) ───────────────────────────────────────────────────────

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw.arn
    format = jsonencode({
      requestId        = "$context.requestId"
      sourceIp         = "$context.identity.sourceIp"
      requestTime      = "$context.requestTime"
      httpMethod       = "$context.httpMethod"
      routeKey         = "$context.routeKey"
      status           = "$context.status"
      protocol         = "$context.protocol"
      responseLength   = "$context.responseLength"
      integrationError = "$context.integrationErrorMessage"
    })
  }
}

resource "aws_cloudwatch_log_group" "apigw" {
  name              = "/aws/apigateway/${local.name_prefix}"
  retention_in_days = 30
}
