# ── Chat turns and ratings ────────────────────────────────────────────────────
# One item per /chat turn (pk = TURN#<turn id>, sk = META) and one per rater's
# thumbs up/down (sk = FB#<rater hash>), kept for analysis; see
# app/services/interaction_store.py. gsi1 lists rated turns by rating and
# month (BC dates). Items expire ANALYTICS_RETENTION_DAYS after their turn.

resource "aws_dynamodb_table" "interactions" {
  name         = "${local.name_prefix}-interactions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  attribute {
    name = "gsi1pk"
    type = "S"
  }

  attribute {
    name = "gsi1sk"
    type = "S"
  }

  # Only rating items have these keys; KEYS_ONLY because the table's pk (the
  # turn id) is all the listing needs before it reads the turns.
  global_secondary_index {
    name            = "gsi1"
    projection_type = "KEYS_ONLY"

    key_schema {
      attribute_name = "gsi1pk"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "gsi1sk"
      key_type       = "RANGE"
    }
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  # Recovery from bad writes. Deleted items (TTL included) stay restorable
  # for this many days.
  point_in_time_recovery {
    enabled                 = true
    recovery_period_in_days = 35
  }

  # KMS encryption with the AWS managed key (aws/dynamodb): callers need no
  # KMS permissions of their own.
  server_side_encryption {
    enabled = true
  }

  deletion_protection_enabled = true

  lifecycle {
    prevent_destroy = true
  }

  tags = { Name = "${local.name_prefix}-interactions" }
}
