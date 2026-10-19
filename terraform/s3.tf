# ── S3 Bucket for Terraform State ─────────────────────────────────────────────

resource "aws_s3_bucket" "old_tfstate" {
  bucket = "${local.name_prefix}-tfstate"

  # Prevent accidental deletion of the state bucket
  lifecycle {
    prevent_destroy = true
  }

  tags = { Name = "${local.name_prefix}-tfstate" }
}

resource "aws_s3_bucket_public_access_block" "old_tfstate" {
  bucket = aws_s3_bucket.old_tfstate.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "old_tfstate" {
  bucket = aws_s3_bucket.old_tfstate.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket" "tfstate" {
  bucket = "${var.environment}-${local.name_prefix}-tfstate"

  # Prevent accidental deletion of the state bucket
  lifecycle {
    prevent_destroy = true
  }

  tags = { Name = "${var.environment}-${local.name_prefix}-tfstate" }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}


resource "aws_s3_bucket" "agent_storage" {
  bucket = "${var.environment}-${local.name_prefix}-agent-context-store"

  # Prevent accidental deletion of the agent storage bucket
  lifecycle {
    prevent_destroy = true
  }

  tags = { Name = "${var.environment}-${local.name_prefix}-agent-context-store" }
}

resource "aws_s3_bucket_public_access_block" "agent_storage" {
  bucket = aws_s3_bucket.agent_storage.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "agent_storage" {
  bucket = aws_s3_bucket.agent_storage.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}
