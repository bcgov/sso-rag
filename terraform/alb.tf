# ── Internal ALB ──────────────────────────────────────────────────────────────
# Private ALB; only reachable from within the VPC (via API Gateway VPC Link).

resource "aws_lb" "main" {
  name               = "${local.name_prefix}-alb"
  internal           = true
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = [data.aws_subnet.subnet_a.id, data.aws_subnet.subnet_b.id]

  enable_deletion_protection = false

  tags = { Name = "${local.name_prefix}-alb" }
}

# ── Target Groups ─────────────────────────────────────────────────────────────

resource "aws_lb_target_group" "api" {
  name                          = "${local.name_prefix}-api-tg"
  port                          = 8000
  protocol                      = "HTTP"
  vpc_id                        = data.aws_vpc.selected.id
  target_type                   = "ip"
  deregistration_delay          = 30

  health_check {
    enabled             = true
    path                = "/health"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = { Name = "${local.name_prefix}-api-tg" }
}

resource "aws_lb_target_group" "ui" {
  name        = "${local.name_prefix}-ui-tg"
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = data.aws_vpc.selected.id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = "/"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    timeout             = 10
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = { Name = "${local.name_prefix}-ui-tg" }
}

# ── Listener ──────────────────────────────────────────────────────────────────

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  # Default action: forward to UI
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.ui.arn
  }
}

# ── Listener Rules: route /query* and /health* to the API target group ────────

resource "aws_lb_listener_rule" "api_query" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }

  condition {
    path_pattern {
      values = ["/query", "/query/*"]
    }
  }
}

resource "aws_lb_listener_rule" "api_health" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 20

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }

  condition {
    path_pattern {
      values = ["/health", "/health/*"]
    }
  }
}
