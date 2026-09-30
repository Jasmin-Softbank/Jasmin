output "public_ip" { value = aws_eip.node.public_ip }
output "app_domain" { value = "${replace(aws_eip.node.public_ip, ".", "-")}.sslip.io" }
output "instance_id" { value = aws_instance.node.id }
output "ssm_session" { value = "aws ssm start-session --region ${var.region} --target ${aws_instance.node.id}" }
output "ci_plan_role_arn" { value = aws_iam_role.ci_plan.arn }
