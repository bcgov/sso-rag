output "api_gateway_url" {
  description = "Public invoke URL of the API Gateway HTTP API"
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "alb_dns_name" {
  description = "Internal DNS name of the Application Load Balancer"
  value       = aws_lb.main.dns_name
}

output "ecr_api_repository_url" {
  description = "ECR repository URL for the API image"
  value       = aws_ecr_repository.api.repository_url
}

output "ecr_ui_repository_url" {
  description = "ECR repository URL for the UI image"
  value       = aws_ecr_repository.ui.repository_url
}

output "ecs_cluster_name" {
  description = "Name of the ECS cluster"
  value       = aws_ecs_cluster.main.name
}

output "vpc_id" {
  description = "VPC ID"
  value       = data.aws_vpc.selected.id
}

output "tfstate_bucket" {
  description = "S3 bucket name for Terraform state"
  value       = aws_s3_bucket.tfstate.bucket
}

