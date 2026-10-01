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

data "aws_subnet" "selected" { id = sort(data.aws_subnets.default.ids)[0] }
data "aws_ami" "ubuntu" {
  owners = ["099720109477"] # Canonical
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

locals {
  # Registry pulls use namespace-owned imagePullSecrets, never node-wide SSM auth.
  node_parameter_arns = var.gitops_token_param == null ? [] : [
  "arn:aws:ssm:${var.region}:${var.account_id}:parameter${var.gitops_token_param}"]
  # The managed SSM agent policy grants GetParameter(s) on '*'. Explicitly narrow
  # it even when no private GitOps parameter is configured.
  node_parameter_policy = {
    Version = "2012-10-17"
    Statement = concat([
      { Effect = "Deny", Action = ["ssm:GetParameters", "ssm:GetParametersByPath", "ssm:GetParameterHistory"], Resource = "*" }
      ], var.gitops_token_param == null ? [
      { Effect = "Deny", Action = ["ssm:GetParameter"], Resource = "*" }
      ] : [], var.gitops_token_param == null ? [] : [
      { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = local.node_parameter_arns }
      ], var.gitops_token_param == null ? [] : [
      { Effect = "Deny", Action = ["ssm:GetParameter"], NotResource = local.node_parameter_arns }
    ])
  }
}

resource "aws_security_group" "node" {
  name        = "${var.name}-node"
  description = "Web in only, no SSH (SSM). Out: 80/443 only; control plane pulls (platform/ZERO-TRUST.md)"
  vpc_id      = data.aws_vpc.default.id

  dynamic "ingress" {
    for_each = var.https_enabled ? [80, 443] : [80]
    content {
      description = "web ${ingress.value}"
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }
  dynamic "egress" {
    for_each = [80, 443] # apt mirrors, HTTPS registries, GitOps, SSM. AWS DNS and IMDS bypass SGs.
    content {
      description = "out ${egress.value}"
      from_port   = egress.value
      to_port     = egress.value
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }
}

resource "aws_iam_role" "node" {
  name = "${var.name}-node"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "node_params" {
  name   = "read-railshot-params"
  role   = aws_iam_role.node.id
  policy = jsonencode(local.node_parameter_policy)
}

resource "aws_iam_instance_profile" "node" {
  name = "${var.name}-node"
  role = aws_iam_role.node.name
}

resource "aws_instance" "node" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnet.selected.id
  vpc_security_group_ids = [aws_security_group.node.id]
  iam_instance_profile   = aws_iam_instance_profile.node.name

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1 # pods cannot reach instance credentials
  }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    encrypted             = true
    delete_on_termination = false # Old root data must survive an explicitly approved migration/replacement.
  }
  user_data = local.cloud_init
  credit_specification { cpu_credits = "standard" }
  user_data_replace_on_change = false # guest changes require a separate approved configuration job
  tags                        = { Name = "${var.name}-node" }
}

resource "aws_eip" "node" {
  instance = aws_instance.node.id
  domain   = "vpc"
}

# Read-only role for CI (terraform plan); applies stay with the platform admin for now.
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "ci_plan" {
  name = "${var.name}-ci-plan"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        StringLike   = { "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:*" }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ci_plan" {
  role       = aws_iam_role.ci_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_budgets_budget" "monthly" {
  count        = var.budget_email == "" ? 0 : 1
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}

resource "aws_ebs_volume" "data" {
  availability_zone = data.aws_subnet.selected.availability_zone
  type              = "gp3"
  size              = var.data_disk_gib
  encrypted         = true
  tags              = { Name = "${var.name}-data", Target = var.target_id, Retention = "retain-until-approved" }
  lifecycle { prevent_destroy = true }
}
resource "aws_volume_attachment" "data" {
  device_name                    = "/dev/sdf"
  volume_id                      = aws_ebs_volume.data.id
  instance_id                    = aws_instance.node.id
  force_detach                   = false
  stop_instance_before_detaching = false # Caller must complete the separate drain/stop maintenance operation.
}
locals {
  cloud_init = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    node_config = yamlencode(merge({
      name        = var.name, node_name = coalesce(var.node_name, var.name), cloud_provider = "aws", region = var.region,
      gitops_repo = var.gitops_repo, gitops_path = var.gitops_path, gitops_revision = var.gitops_revision
    }, var.gitops_token_param == null ? {} : { gitops_token_param = var.gitops_token_param }))
    bootstrap_manifest = jsonencode({ method = "pinned-public-git", revision = var.node_ref, image_ref = var.ami_id })
    bootstrap_script = templatefile("${path.module}/bootstrap.sh.tftpl", {
      device                     = "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${replace(aws_ebs_volume.data.id, "-", "")}",
      initialize_empty_data_disk = var.initialize_empty_data_disk ? "true" : "false",
      node_repo                  = var.node_repo, node_ref = var.node_ref
    })
  })
}
