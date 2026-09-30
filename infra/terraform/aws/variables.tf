variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "name" {
  type    = string
  default = "railshot"
}

variable "instance_type" {
  type    = string
  default = "t3.large" # 2 vCPU / 8 GiB: k3s + Cilium + Argo CD + CNPG + tenant apps
}

variable "root_volume_gb" {
  type    = number
  default = 40
}

variable "node_repo" {
  description = "Public repo that holds infra/ansible (pulled by cloud-init, no SSH)"
  type        = string
  default     = "https://github.com/Jasmin-Softbank/Jasmin.git"
}

variable "node_ref" {
  description = "Branch, tag or commit of node_repo to run"
  type        = string
  default     = "feature/poc-cloud-jihwan"
}

variable "gitops_repo" {
  description = "HTTPS URL of the Git repo Argo CD pulls (the node has no SSH egress)"
  type        = string
}

variable "github_repo" {
  description = "owner/repo allowed to assume the read-only CI role via OIDC"
  type        = string
  default     = "Jasmin-Softbank/Jasmin"
}

variable "budget_usd" {
  type    = number
  default = 30
}

variable "budget_email" {
  description = "Budget alert recipient; empty disables the budget"
  type        = string
  default     = ""
}

variable "https_enabled" {
  description = "Open 443 once the app domain and its certificate exist; until then only 80 is reachable"
  type        = bool
  default     = false
}
