terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # After running `terraform apply` to create the bucket and DynamoDB table,
  # uncomment this backend block and run `terraform init -migrate-state`.
  # backend "s3" {
  #   bucket         = "sso-rag-tfstate-<account_id>"
  #   key            = "sso-rag/terraform.tfstate"
  #   region         = "ca-central-1"
  #   encrypt        = true
  #   dynamodb_table = "sso-rag-tfstate-lock"
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name_prefix = var.project_name
  azs         = slice(data.aws_availability_zones.available.names, 0, 2)
}
