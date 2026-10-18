# ── VPC ───────────────────────────────────────────────────────────────────────

data "aws_vpc" "selected" {
  state = "available"
}

data "aws_subnet" "subnet_a" {
  filter {
    name   = "tag:Name"
    values = [var.subnet_a]
  }
}

data "aws_subnet" "subnet_b" {
  filter {
    name   = "tag:Name"
    values = [var.subnet_b]
  }
}

data "aws_subnet" "web_subnet_a" {
  filter {
    name   = "tag:Name"
    values = [var.web_subnet_a]
  }
}

data "aws_subnet" "web_subnet_b" {
  filter {
    name   = "tag:Name"
    values = [var.web_subnet_b]
  }
}
