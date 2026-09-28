locals {
  name_prefix = "telemetry-${var.environment}"
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, { Name = "${local.name_prefix}-vpc" })
}

# Lock down the VPC's auto-created default security group instead of leaving it with its
# implicit allow-all rules (checkov CKV2_AWS_12) - all traffic should go through a security
# group we explicitly define and reason about, i.e. aws_security_group.internal below.
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${local.name_prefix}-default-sg-locked" })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${local.name_prefix}-igw" })
}

resource "aws_subnet" "public" {
  count      = var.az_count
  vpc_id     = aws_vpc.this.id
  cidr_block = cidrsubnet(var.vpc_cidr, 8, count.index)

  # Deliberately false (checkov CKV_AWS_130): a resource that genuinely needs a public IP
  # (rare in this pipeline - most compute is private-subnet) should request one explicitly at
  # launch rather than getting one by subnet-wide default.
  map_public_ip_on_launch = false

  tags = merge(var.tags, { Name = "${local.name_prefix}-public-${count.index}" })
}

resource "aws_subnet" "private" {
  count      = var.az_count
  vpc_id     = aws_vpc.this.id
  cidr_block = cidrsubnet(var.vpc_cidr, 8, var.az_count + count.index)

  tags = merge(var.tags, { Name = "${local.name_prefix}-private-${count.index}" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = merge(var.tags, { Name = "${local.name_prefix}-public-rt" })
}

resource "aws_route_table_association" "public" {
  count          = var.az_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# checkov:skip=CKV2_AWS_5: intentionally provisioned ahead of any compute. P0-02's scope is
# the networking/IAM baseline only - P0-04 (orchestrator) and P1-01 (ingest service) attach
# their compute to this group via the internal_security_group_id output. An SG with nothing
# attached yet is expected at this stage, not an oversight.
resource "aws_security_group" "internal" {
  name        = "${local.name_prefix}-internal"
  description = "Allow traffic between pipeline services within this environment's VPC"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "Internal VPC traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  # checkov:skip=CKV_AWS_382: open egress is a deliberate placeholder for this baseline pass -
  # pipeline services need general internet egress (package registries, provider APIs) and
  # there's no NAT/VPC-endpoint design yet to scope it further. Revisit once P1-01/P0-04
  # define what egress compute actually needs; track narrowing this in docs/decisions/ before
  # a real (non-LocalStack) apply.
  egress {
    description = "All outbound (see checkov:skip note above)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${local.name_prefix}-internal-sg" })
}

# KMS key for the flow-log group (checkov CKV_AWS_158). An explicit key policy (checkov
# CKV2_AWS_64) rather than relying on the provider's implicit default - grants the account
# root full access, which IS the same effective policy as the implicit default, just written
# down instead of assumed. Review against your org's KMS key policy conventions before a real
# (non-LocalStack) apply.
data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "flow_logs_kms" {
  statement {
    sid       = "EnableRootAccountAccess"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

# checkov:skip=CKV2_AWS_64: a policy IS attached (above) - checkov's graph scanner can't
# statically resolve it through the aws_iam_policy_document data source's .json output, so it
# reports this as if no policy were set. Confirmed by hand: the statement above is present and
# non-empty.
resource "aws_kms_key" "flow_logs" {
  description         = "${local.name_prefix} VPC flow log encryption"
  enable_key_rotation = true
  policy              = data.aws_iam_policy_document.flow_logs_kms.json

  tags = merge(var.tags, { Name = "${local.name_prefix}-flow-logs-kms" })
}

resource "aws_kms_alias" "flow_logs" {
  name          = "alias/${local.name_prefix}-flow-logs"
  target_key_id = aws_kms_key.flow_logs.key_id
}

# VPC flow logs (checkov CKV2_AWS_11) - to CloudWatch Logs rather than S3, so this module
# doesn't need to depend on P0-03's object store to satisfy the check. Retention >= 1 year
# (checkov CKV_AWS_338) via the flow_log_retention_days default below.
resource "aws_cloudwatch_log_group" "flow_logs" {
  name              = "/telemetry/${var.environment}/vpc-flow-logs"
  retention_in_days = var.flow_log_retention_days
  kms_key_id        = aws_kms_key.flow_logs.arn

  tags = merge(var.tags, { Name = "${local.name_prefix}-flow-logs" })
}

data "aws_iam_policy_document" "flow_logs_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "flow_logs_publish" {
  statement {
    sid = "PublishFlowLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.flow_logs.arn}:*"]
  }
}

resource "aws_iam_role" "flow_logs" {
  name               = "${local.name_prefix}-vpc-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_logs_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "flow_logs" {
  name   = "${local.name_prefix}-vpc-flow-logs-publish"
  role   = aws_iam_role.flow_logs.id
  policy = data.aws_iam_policy_document.flow_logs_publish.json
}

resource "aws_flow_log" "this" {
  vpc_id               = aws_vpc.this.id
  traffic_type         = "ALL"
  log_destination_type = "cloud-watch-logs"
  log_destination      = aws_cloudwatch_log_group.flow_logs.arn
  iam_role_arn         = aws_iam_role.flow_logs.arn

  tags = merge(var.tags, { Name = "${local.name_prefix}-flow-log" })
}
