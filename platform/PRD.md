# RAILSHOT PRD v0 (초안)

> 가제 RAILSHOT. Team Jasmin, SoftBank Hackathon 2026 예선 1 (테마 "One Action, Infinite Clouds.").
> 상태: 초안 (2026-09-30). 베스트 프랙티스 문서가 아니다. 결정 대기 항목은 §16에 모았다.
> 관련 문서: [README.md](README.md)(구성·권한), [TOPOLOGY.md](TOPOLOGY.md)(요청·배포 경로), [mcp/TOOLS.md](mcp/TOOLS.md)(MCP 계약), [scenarios/db-migration.md](scenarios/db-migration.md), [contract/](contract/), [agents/](agents/), [runner/](runner/).

## 1. 문제

킥오프 원본 발표(운영진, 2026-09-28)의 요구:
- 로컬에서 만든 웹 앱을 AI로 클라우드나 온프레미스에 원터치로 배포한다.
- 코드를 자동으로 변경하면서 인프라 환경까지 알맞게 구축한다.
- 대충 만든 앱도 운영 수준 인프라로 올린다(예: SQLite → 적절히 설정된 RDS). RAILSHOT은 이를 클러스터 안의 컨테이너 Postgres(CloudNativePG)로 구현한다(§7, [scenarios/db-migration.md](scenarios/db-migration.md)).
- CI/CD 연동, 데이터베이스 마이그레이션, 모니터링·시각화, 배포 작업 로그, 롤백·Blue-Green·Canary, AI 비용 대비 효과, 여러 명의 동시 배포를 고려한다.

공개된 에이전트 IaC 사례(SoftBank AGENTIC STAR의 Terraform 자율 반복, 2026-03)에서도 두 지점은 여전히 사람 몫이다. 앞단의 설계 정리(구성도·설정값)와 뒷단의 품질 검증이다. RAILSHOT은 이 두 지점을 **기본값과 결정론 게이트**로 옮긴다.

## 2. 목표와 비목표

| ID | 목표 |
|---|---|
| G1 | 사용자는 폴더만 넘기고 **도메인 링크 하나**를 받는다. 클라우드 계정·자격·HCL·매니페스트를 주지 않는다 |
| G2 | CI 안의 LLM이 레포를 배포 스택에 맞게 고치고, 판정은 결정론 게이트가 한다(최대 3회, 포기·보고 출구) |
| G3 | 배포 뒤 변경은 자연어로 받고, 미리보기 → 확인 → 적용한다. 링크는 유지된다 |
| G4 | DB 마이그레이션(SQLite → Postgres 포함)을 배포 흐름 안에서 안전하게 한다 |
| G5 | 앱·파이프라인·에이전트를 모두 관측하고, 사용자에게는 핵심 지표를, 운영자에게는 추적을 준다 |
| G6 | 같은 계약으로 온프렘(Terraform의 거울상)을 붙일 수 있게 한다 |

비목표 (MVP): 사용자 클라우드 계정 배포(BYOC), 커스텀 도메인, GPU 워크로드, 멀티 리전, 사람 코드 리뷰를 전제한 흐름, RAILSHOT이 외부 에이전트를 호출하는 것(A2A 클라이언트).

## 3. 사용자와 대표 시나리오

| 사용자 | 경로 | 예 |
|---|---|---|
| 코딩 에이전트를 쓰는 개발자 | MCP | Claude Code·Codex에서 "이 폴더 배포해줘" |
| 팀 | MCP, 동시 요청 | 두 사람이 같은 앱에 변경 요청 → 두 번째는 plan이 낡았다며 거부 |
| 고객사 에이전트 플랫폼 | MCP 등록(1순위), A2A(2순위) | AGENTIC STAR 관리 화면에 RAILSHOT MCP URL 등록 |
| 운영자 | 운영자 MCP(읽기 전용 연합), Grafana | 실패한 배포의 원인 확인 |

대표 시나리오:
1. **첫 배포**: 업로드 → intake → adapter → 게이트(실패 시 fixer, 최대 3회) → deploy → 링크, 적용된 기본값과 AI가 고친 diff
2. **자연어 변경**: "DB 붙이고 메모리 늘려줘" → 위험 등급 cost → 월 비용 차이 확인 → 적용 → 같은 링크
3. **실패**: 스모크 실패 → LKG 자동 롤백 → diagnoser 설명 + 그대로 보낼 수 있는 변경 요청문
4. **DB**: SQLite 앱 → Postgres(M1), 컬럼 추가·삭제, 잘못된 마이그레이션(시나리오 M0–M6)

## 4. 기능 요구사항

우선순위: P0 = 10/3 데모 필수, P1 = 시간 되면, P2 = 확장.

**수집 (intake)**

| ID | 요구 | P |
|---|---|---|
| FR-IN-1 | `upload_create`가 presigned URL(10분), 제외 목록, 최대 크기(100 MB, 2만 파일)를 준다. 파일 내용은 MCP로 받지 않는다 | P0 |
| FR-IN-2 | 경로 탈출·심볼릭 링크·크기 검사, 비밀 스캔(gitleaks)에서 걸리면 거부 | P0 |
| FR-IN-3 | 작업 사본에서 다른 에이전트용 지침·설정 파일(CLAUDE.md, AGENTS.md, `.claude/`, `.cursor*`, `.github/copilot-*`)을 제거 | P0 |
| FR-IN-4 | 인벤토리(`ir.json`): 언어·매니페스트·락파일·프레임워크·포트 후보·기존 Dockerfile·Railpack 결과 | P0 |
| FR-IN-5 | 플랫폼 조직에 앱별 private 레포 생성, import 커밋 | P0 |

**적응·수정 루프**

| ID | 요구 | P |
|---|---|---|
| FR-AD-1 | adapter가 Dockerfile류와 `jasmin.yaml`만 제안한다. 규칙 표나 Railpack으로 되면 LLM 0회 | P0 |
| FR-AD-2 | 게이트 L0(경로·패치 정책) → L1(정적: 스키마·conftest·kubeconform·hadolint) → L2(빌드) → L3(기동·플랫폼 프로브·동작) → L4(Trivy·크기·비밀·비용) | P0 |
| FR-AD-3 | 분류기 F1–F9. F7(앱 결함)·F8(일시 장애)은 LLM을 부르지 않는다 | P0 |
| FR-AD-4 | fixer 최대 3회, 같은 실패 서명 2회면 중단, `give_up` 채널 | P0 |
| FR-AD-5 | 에이전트는 읽기만 한다. 파일은 JSON 출력으로 받고 실행기가 경로 검사 후 쓴다 | P0 |
| FR-AD-6 | 좁은 범위의 앱 코드 수정(DB 연결·설정 계층) 허용 여부(D1) | P1 |

**배포**

| ID | 요구 | P |
|---|---|---|
| FR-DP-1 | `plan_hash` 재검증 → GHCR digest → Terraform apply(OIDC apply 역할) → GitOps 커밋 → Argo CD 동기화 | P0 |
| FR-DP-2 | 앱마다 추측 불가 서브도메인, 와일드카드 DNS·인증서 | P0 (PoC는 sslip.io HTTP) |
| FR-DP-3 | 공개 URL 스모크 통과 시 링크 반환, 실패 시 LKG 롤백 | P0 |
| FR-DP-4 | 배포 전략: 롤링 기본, Canary·Blue-Green은 Argo Rollouts(§8) | P1 |
| FR-DP-5 | 환경 순서(온프렘 → AWS) | P2 |

**DB (컨테이너 Postgres)**: FR-DM-1 앱별 CNPG `Cluster` + 역할 셋(소유자는 마이그레이션 전용, 런타임은 DML만, 읽기 전용)과 ESO 생성 비밀번호(P1), FR-DM-2 `migrate.command` → Argo Sync hook Job, sync-wave 1(DB wave -1, 앱 wave 2)(P1), FR-DM-3 게이트의 임시 `postgres:17` 시험(P1), FR-DM-4 일일 백업(S3)과 파괴적 변경 전 즉석 백업(P1), FR-DM-5 SQLite → Postgres 이전 Job(P2). 상세는 [scenarios/db-migration.md](scenarios/db-migration.md) M0–M7.

**변경**

| ID | 요구 | P |
|---|---|---|
| FR-CH-1 | `change` → `change_get`(diff·영향·비용·risk·`plan_hash`) → `apply(change_id, plan_hash)` → `discard` | P0 |
| FR-CH-2 | 위험 등급 low·restart·cost·destructive는 `terraform plan -json`과 매니페스트 diff로 결정론 분류 | P0 |
| FR-CH-3 | destructive는 서버 elicitation(사용자 UI)만 인정, 스냅샷 먼저. 미지원 클라이언트면 거부 | P1 |
| FR-CH-4 | plan 이후 spec이 바뀌면 `apply` 거부(`base_rev`) | P0 |
| FR-CH-5 | spec에서 빠진 리소스는 분리 후 7일 보존 | P1 |
| FR-CH-6 | 비밀 값은 URL 모드 elicitation(웹 폼)으로만 | P1 |

**관측성**: FR-OB-1 세 신호 수집(P0), FR-OB-2 `status`의 health 블록(P0), FR-OB-3 에이전트 호출 추적(P1), FR-OB-4 파이프라인 추적과 DORA·time-to-URL(P1), FR-OB-5 운영자 대시보드(P1). 상세 §9.

**인터페이스**: FR-IF-1 MCP 도구 11개(§11, P0), FR-IF-2 신·구 MCP 스펙 동시 지원(P0), FR-IF-3 `protocols.lock.yaml`과 적합성 CI(P1), FR-IF-4 A2A 파사드(P2, §10), FR-IF-5 운영자용 읽기 전용 MCP 연합(P2).

## 5. 비기능 요구사항

| 영역 | 요구 |
|---|---|
| 보안 | LLM 쓰기 0(제안만), Rule of Two(에이전트는 비신뢰 입력만), 클라우드는 OIDC 역할만·키 없음, token passthrough 금지, 테넌트 경계는 레포·네임스페이스·토큰 범위로 이중화, 승인은 서버 서명만 인정 |
| 신뢰성 | API 서버 메모리 상태 0, 복제 2개 affinity 없이 동작, 모든 부작용 도구 멱등 |
| 성능 | time-to-URL(호출 → 링크 200) 측정값 공개. 수정 루프 전체 30분 이내, 시도당 10분 |
| 비용 | 실행당 LLM 예산 상한, 계정 예산 알람, 비용 등급 게이트 |
| 개인정보 | 에이전트 텔레메트리에서 `user.email`·프롬프트 본문 제거, 로그·트레이스 보존 7일, 로그 발췌는 마스킹 |
| 이식성 | Provider Interface 계약(ensure/get/destroy/plan), Terraform은 AWS 구현 하나, 온프렘은 같은 계약 |

## 6. 아키텍처 요약

구성 요소와 권한은 [README.md](README.md), 경로는 [TOPOLOGY.md](TOPOLOGY.md). 층별 소유자는 하나다.

| 층 | 소유 | 하는 일 |
|---|---|---|
| 클라우드 리소스 | **Terraform** | VPC·SG·EC2·EBS·IAM(OIDC 역할)·Route 53·ACM·(ALB)·S3(DB 백업)·Secrets Manager 항목·예산. user-data(cloud-init) 렌더링까지. `helm`·`kubernetes` provider는 쓰지 않는다 |
| 노드 | **Ansible** (`ansible-pull`, SSH 없음) | cloud-init `ansible` 모듈로 첫 부팅 때 실행, 이후 systemd timer로 드리프트 교정. 하드닝, k3s 설치(버전·sha256 고정), k3s `manifests/`에 Cilium·Argo CD·루트 Application 배치. 기존 `install.sh`는 태스크 하나로 감싸고 폴백으로 남긴다 |
| 클러스터 안 | **Argo CD** | Cilium과 Argo CD 자신을 뺀 전부(cert-manager, 시크릿, CNPG, 관측성, 정책, 테넌트 앱) |
| 앱 산출물 | 에이전트 제안 + 렌더러 | Dockerfile류와 `jasmin.yaml` → 매니페스트·tfvars(기본값 적용) |

## 7. 기술 스택 (고정 버전)

규칙: 인프라·관측 구성 요소는 릴리스 7일 미만이면 직전 버전, 차트 버전 + 이미지 digest로 고정. 에이전트 CLI는 배포사의 `stable` 태그. 바이너리는 sha256 검증. 버전은 2026-09-30에 GitHub·레지스트리 API로 확인했다.

| 층 | 구성 요소 | 고정 |
|---|---|---|
| IaC | Terraform / AWS provider | 1.16.4 (`~> 1.16.0`) / 6.66.0 (`~> 6.66.0`, lock 파일 커밋) |
| 노드 구성 | ansible-core | 2.21.4 (pip, Python 3.12+) |
| 클러스터 | k3s | **v1.36.4+k3s1** (stable 채널). 1.37은 Cilium·Argo CD·cert-manager·CNPG·ESO 공통 지원 범위 밖 |
| CNI | Cilium | 1.20.2 (K8s 1.33–1.36) |
| 진입 | Traefik | k3s 번들 v3.7.8 (따로 올리지 않음) |
| CD | Argo CD / Argo Rollouts | 3.5.3 (차트 10.9.4) / §8 |
| 인증서·시크릿 | cert-manager / ESO / Sealed Secrets | 1.21.2 / 2.11.0 (월 1회 갱신 필요) / 0.40.0 |
| DB | CloudNativePG (AWS·온프렘 공통, RDS 쓰지 않음) / PostgreSQL / 백업 플러그인 | 1.30.1 / 17 (`ghcr.io/cloudnative-pg/postgresql:17`, digest) / Barman Cloud v0.15.0 |
| 게이트 | Trivy / conftest / kubeconform / gitleaks / hadolint / buildx / Railpack | 0.74.0 / 0.70.1 / 0.8.0 / 8.30.1 / 2.15.1 / 0.37.1 / 0.40.1 |
| 관측 | OTel Collector (`otelcol-k8s`) / Prometheus / Loki / Tempo / Grafana | 0.161.0 / 3.15.0 / 3.7.8 / 3.0.3 / 13.2.3 (차트는 grafana-community) |
| 에이전트 | claude-agent-sdk (Python, CLI 번들) / Claude Code / Codex CLI | `claude-agent-sdk[otel]==0.2.158` (CLI 2.1.280) / 2.1.280 (stable) / 0.159.1 |
| 프로토콜 | MCP TS SDK / A2A JS SDK / agentgateway | `@modelcontextprotocol/server` 2.2.0 / 1.3.0 (P2) / 1.5.0 (P2) |

## 8. CD 구성 (Argo CD 3.5.3)

Argo CD로 고정한다. 3.5.3은 최근 1년 보안 권고 6건이 모두 고쳐진 버전이다. webhook은 열지 않는다(폴링).

| 층 | 도구 | 판정 |
|---|---|---|
| 플랫폼 구성 요소 | **App-of-Apps** 루트 하나, sync wave로 순서 | 채택. 수가 적고 순서가 중요하다 |
| 테넌트 앱 생성 | **ApplicationSet**(git generator, 앱 디렉터리 단위) | 채택 + 보강: `applicationsSync: create-update`, `preserveResourcesOnDeletion: true`. 기본값이면 앱 디렉터리가 사라질 때 DB까지 연쇄 삭제된다 |
| 릴리스 전략 | **Argo Rollouts**(Gateway API 플러그인, Prometheus AnalysisTemplate) | 선택 채택. 기본은 RollingUpdate, `jasmin.yaml`에 `strategy: canary | bluegreen`을 적은 앱만 Rollout. 단일 노드에서 Blue-Green은 파드가 두 배라 쿼터 안에서만 |
| 환경 순서(온프렘 → AWS) | ApplicationSet RollingSync | **기각**. Beta이고, 자동 동기화를 강제로 끄며, 한 Argo CD가 모든 클러스터를 볼 때만 동작한다. 파이프라인 승격 커밋으로 둔다 |

앱 디렉터리(렌더러 출력, `jasmin-gitops/apps/<target>/<tenant>/<app>/`):

```
00-db.yaml        CNPG Cluster·Database·DatabaseRole    wave -1, Prune=confirm, Delete=false
10-migrate.yaml   마이그레이션 Job                      hook Sync, wave 1, hook-delete-policy HookSucceeded
20-app.yaml       Deployment (strategy 지정 시 Rollout)  wave 2
21-services.yaml  Service (+ canary/preview Service)
22-route.yaml     HTTPRoute (정확한 host)
23-analysis.yaml  AnalysisTemplate (Rollout일 때만)
90-smoke.yaml     PostSync Job: 공개 URL 200 스모크
```

- 동기화: `automated: {prune: true, selfHeal: true}`, `ServerSideApply`, `PruneLast`, `FailOnSharedResource`. Rollouts가 바꾸는 HTTPRoute 가중치는 `ignoreDifferences`로 둔다.
- 파괴적 변경 보호: DB·PVC·DB 자격 Secret에 `Prune=confirm`, `Delete=false`. 서버 확인이 끝나면 deployer가 `argocd.argoproj.io/deletion-approved` 주석을 커밋한다.
- 같은 커밋에서 실패한 동기화는 Argo가 자동으로 다시 시도하지 않는다. deployer가 실패를 감지해 LKG를 커밋한다(`argocd app rollback`은 자동 동기화 앱에서 쓸 수 없다).
- Notifications로 동기화·헬스 이벤트를 evidence와 관측으로 보낸다.
- 테넌트 격리: AppProject `t-<tenant>`(sourceRepos·destinations·kind 화이트리스트). Rollouts 컨트롤러가 클러스터 전체 HTTPRoute 수정 권한을 받으므로, 남의 라우트 가중치를 바꾸지 못하게 conftest·VAP 규칙을 둔다. 트래픽 분할은 Traefik 전용 CRD가 아니라 Gateway API 플러그인으로 한다.
- 성숙 조직(Google SRE 카나리, Netflix Kayenta, Uber, Meta, 토스뱅크 사례)의 방향도 같다: 카나리 판정은 지표와 규칙이 하고, AI는 배포 전 위험 평가와 사후 설명에만 쓴다.

## 9. 관측성

**MVP 구성** (k3s 단일 노드, 추정 메모리 0.7–1.4 GiB, 0일차 실측으로 교체):

```
앱 stdout ─filelog─▶ otelcol-k8s ─otlphttp─▶ Loki (단일 바이너리, 7일)
앱 OTLP(선택) ─────▶ otelcol-k8s ─otlphttp─▶ Tempo (단일 바이너리)
                                  └────────▶ Prometheus (OTLP 수신, 7일)
Traefik·Argo CD·Hubble·수집기 ◀─scrape── Prometheus
공개 URL ◀─httpcheck── otelcol-k8s (합성 가용성)
Grafana 13: 데이터소스 3개, 대시보드 "app"(tenant, app 변수)
```

- 수집기에서 네임스페이스 라벨로 `tenant`·`app`을 붙이고, `user.email`과 프롬프트 계열 속성을 지운다.
- 온프렘은 수집기만 두고 AWS 쪽으로 OTLP/HTTPS 푸시한다.
- **사용자에게**: `status`에 health 블록(가용성, 분당 요청, 5xx 비율, p50·p95 지연, 재시작, CPU·메모리, 마지막 배포와 time-to-URL, verdict). 앱을 고치지 않고 Traefik 메트릭과 수집기 수신기만으로 채운다. Grafana는 운영자 인증 뒤에 둔다.
- **파이프라인**: GitHub Actions 트레이스는 `otel-cicd-action`(MVP). CI/CD 규약은 Release Candidate 단계다. DORA 5지표는 Deployments API와 evidence에서 계산하고, 리드 타임은 time-to-URL로 다시 정의한다.
- **에이전트**: OTel GenAI 규약은 아직 Development 단계다(버전 고정 불가, 토큰 메트릭 이름 `gen_ai.client.inference.usage.*`).
  - Claude Code 트레이스(beta)는 호출 쪽 `TRACEPARENT`를 받아 이어진다. 메트릭은 delta라 cumulative로 바꾸고, `user.email`은 지운다.
  - 에이전트 잡 안에서는 로컬 수집기가 파일로만 남기고, 다음 잡이 클러스터로 넘긴다. LLM 잡에 수집 자격을 두지 않는다.
  - Codex는 `[analytics] enabled=false`로 두고 로그·트레이스만 받는다. 기본 메트릭 전송처가 외부다.
- **LangSmith 계열 에이전트 관측 도구**
  - MVP에는 넣지 않는다.
    - `evidence.json`을 권위로 둔다. 시도별로 모델, 턴, 비용, 캐시 토큰, 시간, F-코드, 지침 해시를 기록한다.
    - 결정론 회귀 eval을 둔다: 샘플 레포 10–20개를 게이트 verdict와 URL 200으로 채점하고, 프롬프트·계약 파일이 바뀌는 PR마다 돌린다.
    - 프롬프트 버전은 Git과 해시로 관리한다.
  - P1: Langfuse Cloud 무료 등급(월 50k units)에 신뢰 잡이 구조 트레이스만 OTLP로 보낸다. 코어가 MIT라 나중에 셀프호스트로 옮길 수 있다.
  - 오프라인·온프렘: Phoenix(단일 컨테이너 + SQLite)
  - 기각:
    - Langfuse 셀프호스트: 최소 약 25 GiB라 노드에 안 들어감
    - LangSmith·Braintrust·Weave 셀프호스트: Enterprise 전용
    - Helicone: 유지보수 모드
    - OpenLIT: 기본으로 본문 수집

## 10. 에이전트 구성과 통신

| 에이전트 | 공급자 | 입력 → 출력 |
|---|---|---|
| adapter, fixer | Agent API 실행기(`runner/`): Claude Agent SDK 또는 Codex exec | 작업 사본(읽기) → 보고서 + 파일(JSON) |
| change | 같음 | 자연어 + 현재 spec → 새 spec + 영향 |
| diagnoser | 같음 | 결정론으로 모은 증거(TOPOLOGY §1 사다리) → 진단 |

통신 원칙:

| 경계 | 수단 |
|---|---|
| 한 run 안(adapter → 게이트 → fixer → deployer) | 파일(`jasmin.yaml`, `evidence.json`), git 커밋, 잡 출력. 한 단계 안의 병렬 분석은 SDK 서브에이전트 |
| run 사이(`change` → `apply`, 실패 → diagnoser) | 저장된 산출물(plan, 서명된 `change_id`, evidence)을 새 세션에 주입. 세션을 이어 붙이지 않는다 |
| 외부 코딩 에이전트 → RAILSHOT | MCP |
| 고객사 에이전트 플랫폼(이기종) → RAILSHOT | MCP 등록이 1순위, **A2A 파사드**가 2순위 |
| RAILSHOT → 외부 에이전트 | 하지 않는다 (Rule of Two) |

A2A 파사드 (P2):
- A2A 1.0은 Linux Foundation의 AAIF에 속한다. AAIF 호스팅 프로젝트는 MCP, A2A, agentgateway, AGENTS.md 등이다.
- jasmin-api 한 프로세스에 `/mcp`와 `/a2a/v1` 두 어댑터를 두고, 같은 서비스 계층을 부른다. 파사드는 LLM이 아니라 결정론 코드다.
- 프로토콜은 1.0을 기본으로 하고, 0.3 호환을 켠다. AGENTIC STAR의 A2A가 0.3이기 때문이다.
- 스트리밍·푸시 알림은 끈다. 푸시는 SSRF 표면이라 폴링만 쓴다.
- 인증은 사용자 대리에 device code, 플랫폼 간에 client credentials를 쓴다.
- Agent Card 스킬: `deploy`, `change`, `app_status`, `explain`
- task ↔ 내부 매핑:
  - `Task.id` = 서명 토큰 {deploy|change, `run_id`|`change_id`, sub, 만료}. 서버 저장이 없다.
  - `INPUT_REQUIRED`: 업로드 URL과 변경 미리보기
  - `COMPLETED`: URL
  - `FAILED`: `explain`
  - `CancelTask`: `discard` 또는 run 취소
- destructive 승인은 A2A 메시지로 받지 않는다. 사람이 웹에서 승인한 서명 티켓만 인정한다.
- 레퍼런스: kagent(CNCF Sandbox)는 클러스터 안 에이전트끼리도 A2A를 쓴다. 우리는 내부 에이전트가 투명하고 서버 상태가 0이라 내부에는 쓰지 않는다.

## 11. MCP 도구

| 도구 | readOnly | destructive | idempotent | 설명 |
|---|---|---|---|---|
| `upload_create` | false | false | false | 업로드 슬롯 |
| `deploy` | false | false | true | 첫 배포만. 이미 있는 앱이면 `change`로 안내 |
| `status` | true | – | – | 단계, 시도, 링크, health |
| `change` | false | false | false | 변경 제안(LLM 예산 사용) |
| `change_get` | true | – | – | 미리보기 |
| `apply` | false | true | true | 유일한 실행 도구 |
| `discard` | false | false | true | 제안 폐기 |
| `rollback` | false | false | false | 되돌리는 제안 |
| `explain` | true | – | – | 진단 |
| `logs` | true | – | – | 마스킹된 발췌 |
| `apps_list` | true | – | – | 내 앱, 역할, 남은 한도 |

- 모든 도구의 `openWorldHint`는 false다.
- annotation은 힌트일 뿐이다. 강제는 서버의 역할 검사, `plan_hash`·`base_rev` 검사, elicitation이 한다.
- 운영자용 (P2, agentgateway 1.5.0 뒤, strict JWT, allowlist CEL):
  - kubernetes-mcp-server: read-only, core 도구 세트, Secret 거부, 읽기 전용 ServiceAccount
  - Argo CD MCP ≥ 0.9.0: read-only, 읽기 전용 계정 토큰
- diagnoser LLM에는 어떤 MCP도 주지 않는다.
- 성숙한 OSS에서 가져온 원칙:
  - 역할별 도구 목록은 서버에서 거른다(kubernetes-mcp-server).
  - read-only는 자격증명으로 강제한다. 모드 플래그만 믿지 않는다.
  - 승인은 서버 서명 티켓만 인정한다. HolmesGPT가 승인 위조 critical 권고 뒤 이렇게 고쳤다.

## 12. 동향 추적과 스펙 반영

- **`protocols.lock.yaml`**
  - MCP: 기본 2026-07-28, 구 스펙 2025-11-25·2025-06-18도 받음
  - SDK 버전, A2A 1.0·0.3, 게이트웨이·연합 MCP 버전
  - 적합성 테스트 패키지: `@modelcontextprotocol/conformance@0.2.0-alpha.11`. 안정판 0.1.16은 2026-07-28을 지원하지 않는다.
  - `tools/list` 스냅샷
- **CI**
  - MCP 적합성 테스트(기대 실패 목록 포함)
  - 도구 스냅샷 비교. 이름·스키마·힌트·설명 해시를 본다. 계약 변경과 도구 설명 변조를 함께 막는다.
  - A2A TCK: 파사드를 열 때만
- **추적**
  - 주간 `gh api` 스크립트: 새 릴리스와 보안 권고 수 변화
  - 추적 대상: MCP 사양·SEP(파일 전송 SEP-2631 등), SDK, A2A, agentgateway, 연합 MCP 서버
  - SDK는 자동으로 최신판으로 올리지 않는다.
- **정기 검토:** 월 1회 30분. MCP 릴리스(3월·9월) 직후에 추가로 한다.

## 13. 빠진 사안과 위험

"누구나 폴더를 올리면 공개 URL" 플랫폼이 겪는 사안이다(공개 사고·남용 사례 기준).

| 사안 | 우선 | MVP 최소 조치 | 확장 |
|---|---|---|---|
| 남용(피싱·크립토마이닝·스팸) | P0 | 초대제 로그인, 앱 TTL, 운영자 킬스위치. 무료 배포 URL에는 첫날부터 피싱이 온다(Lovable·v0 사례) | 콘텐츠 스캔, 평판 점수 |
| 도메인 평판·Safe Browsing | P0 | 앱 도메인과 플랫폼(API) 도메인을 **다른 등록 도메인**으로 산다. 차단은 등록 도메인 단위라 앱 하나가 전체를 막을 수 있다 | PSL 등재 |
| abuse 신고·이용 정책 | P0 | abuse@ 창구, 24시간 담당, 1쪽 AUP. AWS abuse 신고에 24시간 안에 답하지 않으면 계정 정지 위험 | 자동 처리 |
| 이그레스 제어 | P0 | 기존 정책에 SMTP 포트 차단 추가 | 도메인 allowlist |
| 로그·트레이스 마스킹과 보존 | P0 | 내용(본문) 수집 끔, 보존 7일 | 테넌트별 보존 |
| **LLM 자격의 약관 등급** | P0 | 사용자 코드를 처리하는 CI는 **API 키(가능하면 Anthropic WIF)**. 구독 토큰(`claude setup-token`)은 소비자 약관이 적용되므로 팀원 로컬 개발에만 쓴다 | ZDR 협의, 리전 고정 |
| 자원 쿼터·유휴 앱 | P1 | TTL 만료 시 replicas 0 + 안내 | 스케일 투 제로 |
| 비용 상한 | P1 | Budgets 알림 + Budget action(하드 캡 아님), CI 전용 LLM 키 | 앱별 비용 |
| LLM 비용·캐싱 | P1 | 작업 경로·프롬프트 접두사 고정(캐시가 작업 디렉터리 단위), 캐시 토큰 기록 | |
| 에이전트 회귀 평가 | P1 | 샘플 레포 10–20개 결정론 채점 | LLM-as-judge(설명 품질만) |
| 동시 배포 충돌 | P1 | gitops 쓰기 직렬화 + rebase 재시도, `base_rev` | |
| 백업·복구 | P1 | 복구 리허설 1회(M7), 재구성 시간 측정 | |
| 공급망 | P1 | 허용 베이스 이미지 digest 목록, 레지스트리 미러 | 서명 검증 |
| 해커톤 데모 환경 | P1 | 443만 사용, 핫스팟·녹화 백업 | |
| 구성 요소 CVE | P2 | 릴리스·GHSA 주간 확인(§12 스크립트) | |

현재 막힌 것:
- 로컬 Claude 미로그인
- Codex 사용 한도(10/4 초기화)
- Docker 데몬 무응답
- HTTPS 시연용 도메인 미구매

## 14. 마일스톤

| 날짜 | 내용 |
|---|---|
| 9/30 | 레퍼런스, 설계 지침, 실행기, PRD |
| 10/1 21:00 | 팀 회의: 파트별 데모. CI/CD 파트는 Terraform + CI 파이프라인(수정 루프 포함) |
| 10/2 | 툴셋 동결(QA-6), 스택 버전 확정 |
| 10/3 10:00 | 제출 ①, Day1 |
| 10/4 13:00 | 제출 ②, Day2 발표 |

## 15. 성공 지표

| 지표 | 측정 |
|---|---|
| time-to-URL | 호출 → 링크 200, 앱별 측정값 공개 |
| 첫 통과율 | LLM 0회로 통과한 비율, 수정 루프로 통과한 비율(시도 수 분포) |
| give_up 정확도 | 포기한 건 중 실제로 앱 코드 수정이 필요했던 비율 |
| 게이트 드릴 | 6개 드릴 통과(테스트 삭제 패치, `HEALTHCHECK exit 0`, 로그 인젝션, 같은 오류 2회, 앱 결함, 자연어 변경) |
| 변경 | `apply` 성공률, 거부된 낡은 plan 수 |
| 롤백 | 실패 감지 → LKG 복원까지 시간 |
| 비용 | 배포당 LLM 비용·토큰, 인프라 일 비용 |

## 16. 결정 대기

| ID | 질문 | 권고 |
|---|---|---|
| D1 | 앱 코드 수정 범위 | DB 연결·설정 계층 같은 좁은 범위 허용, diff 표시, 게이트 통과 |
| D2 | 반복 상한 | 3 |
| D6 | 워크로드 명세 형식 | `jasmin.yaml` v0 (이름 확정 시 변경) |
| D7 | 소스 입력 | 업로드 + 앱별 private 레포 |
| D8 | 도메인 | HTTPS 시연 전 구매 |
| D9 | 변경 확인 주체 | 파괴적 변경·비용 상한 초과만 서버 확인 |
| P-D1 | `change`·`rollback`의 readOnlyHint | false |
| P-D2 | `cancel(run_id)` | P2(A2A와 함께) |
| P-D3 | A2A 파사드 해커톤 범위 | 제외. Agent Card와 매핑만 발표 |
| P-D4 | AGENTIC STAR 연동 시연 | MCP 서버 URL 등록 |
| S-D1 | Ansible 도입 | `install.sh`를 감싸는 플레이북으로 시작, install.sh는 폴백 |

참고: AGENTIC STAR의 A2A는 0.3이다. 마켓플레이스판 릴리스 노트의 최신은 v2.9.0(8/21)이다. 앞선 조사에서 쓴 "v2.10.0, MCP·A2A UI 리소스"는 AWS Marketplace 제품 페이지 표기라서 릴리스 노트와 맞지 않는다.

## 부록 A. score.yaml은 쓸 가치가 있나

**판단: 교환 형식으로는 쓰고, 원천 명세나 MVP 렌더러로는 쓰지 않는다.**

| 관점 | Score (score.dev/v1b1, score-k8s) | RAILSHOT에 미치는 영향 |
|---|---|---|
| 표준성 | CNCF Sandbox 사양, JSON Schema 있음 | 심사·발표에서 "표준 명세로 내보낸다"는 설명이 된다 |
| 표현 범위 | 컨테이너·변수·프로브·자원 요청·`resources`(type/class/params) | 빌드, 마이그레이션 hook, 크기 등급, 릴리스 전략, 레플리카가 없다 → 비표준 annotation으로만 담긴다 |
| 렌더러 | score-k8s: 기본 postgres는 StatefulSet(`postgres:17-alpine`), route는 HTTPRoute. **CNPG provisioner는 없다** | CNPG·Rollouts·sync wave·보안 기본값을 모두 우리 provisioner와 patch 템플릿으로 다시 써야 한다 |
| 성숙도 | 공개 채택 6곳(모두 Humanitec 경유), score-k8s 규모 작음, "개념은 좋은데 구현이 얇다"는 비판 | 해커톤 일정에서 렌더러 의존은 위험 |
| LLM 입력면 | 필드가 많고 일반적 | 에이전트가 쓰고 자연어 변경이 고치는 명세는 작을수록 안정적이다 → `jasmin.yaml`이 유리 |

적용 방법:
- `jasmin.yaml`이 원천이다(에이전트 입력면, 자연어 변경면).
- 렌더러가 `score.yaml`을 결정론으로 **함께 내보낸다**(서비스마다 한 파일).
  - `port` → `service.ports`
  - `health` → `readinessProbe.httpGet`
  - `env`·`secrets` → `variables`
  - `resources.postgres` → `resources.db: {type: postgres}`
  - `route` → `resources.route: {type: route}`
  - 나머지(build, migrate, size, strategy, replicas) → `metadata.annotations`의 `railshot.dev/*`
- score-k8s를 렌더러 백엔드로 쓰는 것은 QA-5의 1일 PoC로 판단한다. CNPG provisioner를 만드는 비용이 템플릿 직접 작성보다 작을 때만 쓴다.

## 부록 B. 2025 수상·우수작과 인프라 스택 비교

2025 예선 주제는 "Make Deployment Delightful"로 2026과 거의 같았다. 아래는 공개 레포 코드 기준이다(팀 단위 수상 표기는 레포·인터뷰 근거, 일부는 팀 주장).

| 팀 (결과) | 빌드 | CD·런타임 | 네트워크·도메인 | 보안 | 관측 | AI |
|---|---|---|---|---|---|---|
| Orange (예선 최우수 주장) | CodeBuild | CodeDeploy, ECS B/G (Terraform) | — | OIDC | 야간 자동 정지 Lambda | 없음 |
| Yoitang (예선 최우수 주장) | Jenkins + Kaniko | k3s | **앱별 서브도메인 + TLS** | **Trivy 게이트**, non-root, **사용자 계정 없이 배포** | Kubecost | 없음 |
| Blue (본선 최우수) | ECR | kOps k8s + WASM(SpinKube), Spot | CloudFront + WAF | Cilium | Loki·Prometheus | 없음 |
| Green (본선 2위) | CodeBuild + Buildpacks | k3s + Knative, **Argo CD**, Terraform Cloud | 정규식 Ingress | gVisor | — | 없음 |
| Yellow (본선) | — | k3s(멀티 AZ) + Knative, **Argo CD**, Terraform | 4-Tier 망분리 | SSM(SSH 없음) | — | 없음 |
| Banana (예선) | — | EKS + Kustomize + **Argo Rollouts** + HPA | — | — | — | 없음 |
| Deplight (예선) | LLM이 Dockerfile 생성(검증 없음) | ECS circuit breaker | — | — | — | OpenAI |
| HikariFlow (예선, 입상 없음) | — | 3사 Terraform **생성만**(배포 안 함) | — | — | 비용 추정 | Bedrock |
| **RAILSHOT** | Dockerfile·Railpack + **LLM 수정 루프 + 결정론 게이트** | k3s + **Argo CD**(App-of-Apps·ApplicationSet) + Rollouts(선택), Terraform·Ansible | 앱별 서브도메인 + 와일드카드 TLS, 별도 등록 도메인 | OIDC·키 없음, PSS restricted, 경로 allowlist, Trivy·conftest | OTel → Prometheus·Loki·Tempo, health 블록, evidence | Claude·Codex(핵심 경로, 쓰기 0) |

같은 선택(검증된 조합): k3s(Yoitang·Green·Yellow), Argo CD(Green·Yellow), Terraform(대부분), Cilium(Blue), Argo Rollouts(Banana), 앱별 서브도메인 TLS·Trivy·계정 없는 배포(Yoitang).

2025에 없던 것:
- 온프렘 + 클라우드를 같은 계약으로(2025 온프렘 0팀)
- LLM이 배포 핵심 경로에 있고 결정론 게이트가 판정(2025 LLM 팀은 부가 기능이거나 검증 없음)
- 자연어 변경과 plan 미리보기
- 컨테이너 Postgres의 역할 분리와 마이그레이션 게이트(2025 배점에 "데이터 지속성"이 있었다)
- MCP 인터페이스와 에이전트 evidence

보강할 것:
- **비용 절감 장치:** Orange는 야간 자동 정지, Yoitang은 Kubecost를 넣었다 → 유휴 앱 TTL, 예산 알람, 야간 정지 일정(EventBridge)
- **가용성:** Blue·Yellow는 멀티 AZ였다 → 단일 노드 한계를 명시하고 "시간이 더 있었다면" 답을 준비한다
- **격리:** Green은 gVisor, Yoitang은 non-root를 썼다 → PSS restricted + user namespace. gVisor RuntimeClass는 확장
- **남용 대응:** Blue는 WAF를 두었다 → §13 P0

심사위원이 칭찬한 것은 두 가지였다.
- "작은 세부 기능까지 끝까지 동작"
- "우려 → 대안 → PoC → 결정"

그래서 가장 큰 위험은 설계에 비해 구현이 늦은 것이다. README는 코드와 같아야 하고, 수치는 측정값만 쓴다.
