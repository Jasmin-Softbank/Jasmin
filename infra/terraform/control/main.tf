# Independent state: administrator-only Codex PoC, not the app node or tenant CI.
terraform {
  required_version = ">= 1.5.7"
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.66.0" }
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = "railshot", Component = "control-poc", ManagedBy = "terraform" } }
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

data "aws_vpc" "default" { default = true }
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}
variable "ami_id" {
  type        = string
  description = "Reviewed exact Canonical Ubuntu 24.04 amd64 image; required for future plans. Existing VM is not changed by this source edit."
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.ami_id))
    error_message = "Provide an exact regional AMI ID, never a moving image alias."
  }
}
data "aws_ami" "ubuntu" {
  owners = ["099720109477"]
  filter {
    name   = "image-id"
    values = [var.ami_id]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
}
data "aws_caller_identity" "current" {}

locals {
  name           = "railshot-control-poc"
  auth_parameter = "/railshot/codex/operator-primary/auth"
}

resource "aws_security_group" "control" {
  name        = local.name
  description = "Administrator PoC: no ingress; SSM and HTTPS outbound"
  vpc_id      = data.aws_vpc.default.id
  ingress     = []
  dynamic "egress" {
    for_each = [80, 443]
    content {
      from_port   = egress.value
      to_port     = egress.value
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }
}

resource "aws_iam_role" "control" {
  name = local.name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.control.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "codex_auth" {
  role = aws_iam_role.control.id
  name = "read-single-operator-auth"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.auth_parameter}"
      }, {
      # SSMManagedInstanceCore itself grants parameter reads on '*'. Narrow those too.
      Effect      = "Deny"
      Action      = ["ssm:GetParameter", "ssm:GetParameters"]
      NotResource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.auth_parameter}"
    }]
  })
}
resource "aws_iam_instance_profile" "control" {
  name = local.name
  role = aws_iam_role.control.name
}

resource "aws_instance" "control" {
  ami                                  = data.aws_ami.ubuntu.id
  instance_type                        = "t3.medium"
  subnet_id                            = sort(data.aws_subnets.default.ids)[0]
  associate_public_ip_address          = true
  vpc_security_group_ids               = [aws_security_group.control.id]
  iam_instance_profile                 = aws_iam_instance_profile.control.name
  instance_initiated_shutdown_behavior = "stop"
  credit_specification { cpu_credits = "standard" }
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 30
    encrypted             = true
    delete_on_termination = false
  }
  # Only public bootstrap code and a parameter NAME enter user-data/state.
  # Credential creation/import is a separate administrator operation.
  user_data_base64 = base64gzip(templatefile("${path.module}/cloud-init.yaml.tftpl", {
    playbook_b64 = filebase64("${path.module}/../../ansible/control.yml")
    accounts_b64 = filebase64("${path.module}/../../../platform/runner/accounts.yaml")
    region       = var.region
  }))
  user_data_replace_on_change = false
  tags                        = { Name = local.name }
  depends_on                  = [aws_iam_role_policy_attachment.ssm, aws_iam_role_policy.codex_auth]
}

output "instance_id" { value = aws_instance.control.id }
output "auth_parameter_name" { value = local.auth_parameter }
output "connect" { value = "aws ssm start-session --region ${var.region} --target ${aws_instance.control.id}" }
