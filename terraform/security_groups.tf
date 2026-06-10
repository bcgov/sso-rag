# ── ALB Security Group ────────────────────────────────────────────────────────
# Accepts HTTP/HTTPS from anywhere (API Gateway VPC Link uses HTTP internally).

resource "aws_security_group" "alb" {
  name        = "${local.name_prefix}-alb-sg"
  description = "Allow HTTP inbound to the internal ALB from the VPC"
  vpc_id      = data.aws_vpc.selected.id

  ingress {
    description = "HTTP from VPC (covers VPC Link ENI traffic)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.selected.cidr_block]
  }

  # Self-referencing rule: allows VPC Link ENIs (which share this SG) to reach the ALB
  ingress {
    description = "HTTP from VPC Link ENIs (same SG)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    self        = true
  }

  egress {
    description = "Allow all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-alb-sg" }
}

# ── API Container Security Group ──────────────────────────────────────────────

resource "aws_security_group" "api" {
  name        = "${local.name_prefix}-api-sg"
  description = "Allow inbound from ALB to API containers on port 8000"
  vpc_id      = data.aws_vpc.selected.id

  ingress {
    description     = "From ALB to API"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "Allow all outbound (Bedrock, ECR, CloudWatch)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-api-sg" }
}

# ── UI Container Security Group ───────────────────────────────────────────────

resource "aws_security_group" "ui" {
  name        = "${local.name_prefix}-ui-sg"
  description = "Allow inbound from ALB to UI containers on port 8080"
  vpc_id      = data.aws_vpc.selected.id

  ingress {
    description     = "From ALB to UI"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "Allow all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-ui-sg" }
}
