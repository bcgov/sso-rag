# ── VPC Link (v2) ─────────────────────────────────────────────────────────────
# REST API private integrations reach the internal ALB directly through a
# VPC Link v2 (integration_target = ALB ARN); no NLB is needed.

resource "aws_apigatewayv2_vpc_link" "main" {
  name               = "${local.name_prefix}-vpc-link"
  security_group_ids = [aws_security_group.alb.id]
  subnet_ids         = [data.aws_subnet.subnet_a.id, data.aws_subnet.subnet_b.id]
}

# ── REST API ──────────────────────────────────────────────────────────────────
# A REST API (not an HTTP API) because only REST APIs can stream integration
# responses; HTTP APIs buffer the whole /chat SSE answer before sending it.
#
# The invoke URL includes the stage name (https://<id>.execute-api.<region>.amazonaws.com/<stage>/),
# so the UI is built with a relative base and calls the API with relative paths.
#
# There is no API Gateway CORS config: OPTIONS requests are proxied like any
# other method and answered by FastAPI's CORSMiddleware.

resource "aws_api_gateway_rest_api" "main" {
  name        = "${local.name_prefix}-api-gw"
  description = "REST API Gateway for ${var.project_name} (${var.environment})"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_resource" "proxy" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "{proxy+}"
}

locals {
  # "/" serves the UI's index.html; "/{proxy+}" covers /chat, /health, assets, …
  apigw_routes = {
    root = {
      resource_id = aws_api_gateway_rest_api.main.root_resource_id
      path        = ""
    }
    proxy = {
      resource_id = aws_api_gateway_resource.proxy.id
      path        = "{proxy}"
    }
  }
}

resource "aws_api_gateway_method" "any" {
  for_each = local.apigw_routes

  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = each.value.resource_id
  http_method   = "ANY"
  authorization = "NONE"

  request_parameters = each.key == "proxy" ? { "method.request.path.proxy" = true } : {}
}

resource "aws_api_gateway_integration" "alb" {
  for_each = local.apigw_routes

  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = each.value.resource_id
  http_method             = aws_api_gateway_method.any[each.key].http_method
  type                    = "HTTP_PROXY"
  integration_http_method = "ANY"

  connection_type    = "VPC_LINK"
  connection_id      = aws_apigatewayv2_vpc_link.main.id
  integration_target = aws_lb.main.arn
  # With VPC Link v2 the URI only sets the Host header and path; the request
  # goes to integration_target.
  uri = "http://${aws_lb.main.dns_name}/${each.value.path}"

  request_parameters = each.key == "proxy" ? { "integration.request.path.proxy" = "method.request.path.proxy" } : {}

  # Stream /chat server-sent events as they are produced. Also lets binary UI
  # assets through without configuring binary media types.
  response_transfer_mode = "STREAM"
  # Upper bound for one streamed response (max 900000 in STREAM mode); the
  # ALB idle timeout (120s) still applies between chunks.
  timeout_milliseconds = 300000
}

resource "aws_api_gateway_deployment" "main" {
  rest_api_id = aws_api_gateway_rest_api.main.id

  # Redeploy whenever routing changes.
  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.proxy,
      aws_api_gateway_method.any,
      aws_api_gateway_integration.alb,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "main" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  deployment_id = aws_api_gateway_deployment.main.id
  stage_name    = var.environment

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw.arn
    format = jsonencode({
      requestId        = "$context.requestId"
      sourceIp         = "$context.identity.sourceIp"
      requestTime      = "$context.requestTime"
      httpMethod       = "$context.httpMethod"
      resourcePath     = "$context.resourcePath"
      path             = "$context.path"
      status           = "$context.status"
      protocol         = "$context.protocol"
      responseLength   = "$context.responseLength"
      integrationError = "$context.integration.error"
    })
  }

  depends_on = [aws_api_gateway_account.main]
}
