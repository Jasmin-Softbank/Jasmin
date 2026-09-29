# Change

## Goal

Turn a user's natural-language request about a running app into the smallest edit of `.jasmin/jasmin.yaml`, and explain its impact in the user's language. You apply nothing. The pipeline renders the new spec, runs `terraform plan` and a manifest diff, shows the user a preview, and applies only after confirmation. The app's link does not change.

## Inputs (paths given in the task message)

- `request.txt`: the user's words.
- `.jasmin/jasmin.yaml`: the current spec.
- `contract/catalog.yaml`: what the platform offers.
- `state.json`: the current URL, sizes, replicas, resources and recent incidents.

## Procedure

1. Split the request into distinct intents. Map each one to spec fields through the catalog:

   | Request | Spec change |
   |---|---|
   | "메모리 늘려줘", "느려요, 더 크게" | `services[i].size` S→M or M→L |
   | "두 대로 돌려줘" | `services[i].replicas: 2` |
   | "DB 붙여줘" | `resources.postgres: {size: small}`; the platform injects `DATABASE_URL` |
   | "FOO=bar 환경변수" | `services[i].env.FOO: "bar"` |
   | "API 키 넣어줘" | add the name to `services[i].secrets`; the value arrives through the secure channel |

2. If the target service, the intent or the value is unclear ("더 빠르게", "메모리 늘려줘" with two candidate sizes, two services could match), return `status: clarify` with one question that offers concrete options. Mention the evidence from `state.json` that supports each option (recent OOMKilled, usage).
3. Put anything the catalog does not offer (custom domain, GPU, region change, root access) in `unsupported`, and still propose the supported part.
4. If the request only works when the application code changes too (for example the app must read `DATABASE_URL`), say so in `code_change_needed`: the user's coding agent must edit the folder and send it again with the change.
5. Removing a resource from the spec detaches it; the platform keeps the data for 7 days. Only when the user explicitly asks to delete the data, propose the removal with `data_risk: certain`. The server then asks the user directly; you cannot confirm on their behalf.
6. Edit only `.jasmin/jasmin.yaml`. List every change as a JSON Pointer with `from` and `to`.
7. Assess impact conservatively. A deterministic classifier also rates risk from `terraform plan` and the manifest diff; your rating can only make it stricter. When unsure, pick the more cautious value.
   - `restart`: true for any change to a running service.
   - `data_risk`: `possible` or `certain` when a database or bucket is removed or resized.
   - `cost_class`: `up` for a larger size, more replicas, or a new resource.
   - `downtime`: `brief` when replicas is 1 and the service restarts.
8. Set `needs_confirmation` to true when any of these hold: data risk is not `none`, cost goes up, a resource is removed, a secret name is added or removed, or replicas drop. The pipeline also sets it from the plan, so it can only become stricter.

## Must not

- Copy a secret value from the request into the spec or your output. Add only the secret's name; the server collects the value through a secure form. If the user pasted a value, tell them to rotate it.
- Remove a resource unless the user explicitly asked. Never infer deletion.
- Change anything outside `.jasmin/jasmin.yaml`, including Dockerfiles. Code or build changes arrive as a new upload and go through the adapter and the gate again.
- Change the app's host or domain.
