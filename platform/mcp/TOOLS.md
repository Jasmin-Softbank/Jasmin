# Jasmin MCP 도구 계약 v0 (jasmin-api)

> 상태: 제안 (2026-09-30). 노션 QA-7의 도구 목록을 대체하는 안. 근거는 팀 조사 노트(비공개: Agent Gateway 조사, MCP 2026-07-28 신·구 스펙 지원, 로컬 폴더→URL, 입력·변경 흐름). 변경 흐름은 Railway(저장된 plan, 파괴적 변경 별도 확인, plan 이후 환경이 바뀌면 거부), Massdriver(롤백 = 새 제안), Render(spec에서 빠진 리소스는 분리)를 따랐다.

## 원칙

1. **파일은 MCP로 받지 않는다.**
   - MCP 2026-07-28에는 파일 전송 기능이 없다(Roots는 deprecated, SEP-2631은 미채택). 원격 MCP 서버는 사용자 디스크를 읽을 수 없다.
   - 업로드는 presigned URL로 받고, MCP에는 `upload_id`만 넘긴다. 앞으로 나올 표준 방향(`files/authorizeUpload`)과도 같은 모양이다.
2. **서버 메모리에 상태를 두지 않는다.** 신 스펙(stateless)과 구 스펙(stateful) 클라이언트를 같은 핸들러로 받는다. 복제 2개가 세션 고정 없이 동작한다.
3. **부작용이 있는 도구는 둘뿐이다: `deploy`(첫 배포)와 `apply`.** 나머지는 읽기나 미리보기다. `change`와 `rollback`은 제안만 만들고, 실행은 항상 `apply`가 한다. 기존의 `plan` 도구는 `change`에 흡수했다.
4. **오래 걸리는 작업은 `run_id`를 바로 돌려주고, 클라이언트가 `status`로 폴링한다.**
5. **사용자 확인은 MCP 안에서 받는다.** 2026 스펙은 MRTR `inputRequired`, 2025 스펙은 elicitation을 쓴다. SDK shim이 둘을 자동으로 바꿔 준다.

## 도구

| 도구 | 종류 | 입력 | 출력 |
|---|---|---|---|
| `upload_create` | 준비 | `app?`, 예상 크기 | `upload_id`, presigned PUT URL(10분), 제외 목록, 최대 크기 |
| `deploy` | 부작용 | `upload_id`, `request?`(자연어) | `run_id`, `app` |
| `status` | 읽기 | `run_id` 또는 `app` | 단계, 시도 수, AI가 바꾼 파일 요약, 적용된 기본값, `url`, 다음 행동 |
| `change` | 미리보기 | `app`, `request`(자연어), `upload_id?`(코드도 바뀔 때) | `change_id`, `base_rev`(현재 spec 커밋), 만료 30분 |
| `change_get` | 읽기 | `change_id` | 사람용 요약, spec diff, 영향(재시작·다운타임), K8s·Terraform 변경 수(+/~/−/replace), 월 비용 차이, `risk`, `requires[]`, `plan_hash` |
| `apply` | 부작용 | `change_id`, `plan_hash`, `confirm_destructive?` | `run_id`. `base_rev`가 현재 커밋과 다르면 거부 |
| `discard` | 준비 | `change_id` | 제안 폐기 |
| `explain` | 읽기 | `run_id` | diagnosis(원인, 근거, 사용자가 할 일, 제안 요청문) |
| `logs` | 읽기 | `run_id`, 단계 | 마스킹된 발췌 |
| `rollback` | 미리보기 | `app`, `to_revision?` | 새 `change_id`(되돌리는 제안). 적용은 `apply` |
| `apps_list` | 읽기 | 없음 | 내 앱, 링크, 상태 |

도구 annotation(`readOnlyHint`, `destructiveHint`)을 모든 도구에 명시한다.
- `destructiveHint`는 기본값이 true라서, 빠뜨리면 클라이언트가 모든 호출에 확인을 묻는다.
- annotation은 힌트일 뿐이다. 실제 강제는 서버의 `plan_hash`·`base_rev` 검사와 elicitation이 한다.

QA-7 초안과 달라진 점:
- `plan(path, targets)`는 원격 서버에서 경로를 읽을 수 없어서 `upload_create` + `deploy(upload_id)`로 바꿨다.
- 자연어 변경을 위해 `change`와 `apply`를 추가했다.
- `targets_list`는 운영자 전용으로 내렸다.

## 사용자 흐름

**첫 배포**
1. `upload_create`
2. 사용자 에이전트가 서버가 준 제외 목록으로 tar를 만들고 sha256을 계산한 뒤 PUT한다.
3. `deploy(upload_id)` 호출 → `status`를 폴링한다 → 링크를 받는다.
   - 기본값(크기 S, 추가 리소스 없음)만 쓰면 확인 없이 진행한다.
   - `request`로 DB 같은 리소스를 요청했다면 `inputRequired`로 비용 등급을 확인받는다.

**변경**
1. `change(app, "DB 붙이고 메모리 늘려줘")` → `change_get`으로 미리보기를 받는다.
   - 값이 모호하면("메모리 늘려줘") 서버가 `inputRequired`로 되묻는다. 선택지(1Gi / 2Gi)와 근거(최근 OOMKilled, 사용량)를 함께 보여 준다.
2. 사용자가 확인한다.
3. `apply(change_id, plan_hash)` → `status` → 같은 링크가 유지된다.
4. 반영 확인(URL 200, readiness)에 실패하면 이전 spec 커밋으로 자동 복원한다. DB·볼륨 데이터는 되돌리지 않는다는 것을 요약에 명시한다.

**변경 위험 등급과 확인** (결정론 분류기가 `terraform plan -json`과 매니페스트 diff로 정한다. LLM 판단은 더 엄격한 쪽으로만 반영)

| risk | 예 | 확인 |
|---|---|---|
| low | 비밀 아닌 env, 쿼터 안의 메모리·레플리카 증가 | 요청자가 요약을 보고 1회 확인 |
| restart | 레플리카 1개인 서비스의 재시작 | 재시작·예상 다운타임을 표시하고 확인 |
| cost | 노드 추가, DB 추가·상향 | 월 비용 차이를 표시하고 확인. 플랫폼 상한을 넘으면 거부 |
| destructive | DB·버킷 제거·축소, replace를 일으키는 필드, 서비스 삭제 | 서버가 elicitation으로 사용자에게 직접 확인(앱 이름 입력)을 받는다. 스냅샷을 먼저 뜨고, Argo `Prune=false`, RDS `deletion_protection`을 둔다 |

- 파괴적 변경을 플래그 하나로만 막으면 LLM이 그 플래그를 스스로 붙일 수 있다. 그래서 LLM이 대신 답할 수 없는 elicitation UI로만 받는다. 클라이언트가 elicitation을 지원하지 않으면 파괴적 변경은 거부한다.
- spec에서 리소스를 빼면 기본은 **분리 후 보존**(7일)이다. 실제 삭제는 별도의 파괴적 변경으로만 한다. Terraform `prevent_destroy`는 블록을 지우면 무력해지므로, 분류기가 `delete` 액션 자체를 잡는다.
- **비밀 값은 도구 인자로 받지 않는다.** URL 모드 elicitation으로 Jasmin 웹 폼을 열어 값을 받고, 저장소에만 넣는다. spec에는 이름만 남는다.

**실패**
- deployer가 LKG로 자동 롤백한다.
- `status`에 실패와 롤백이 표시되고, `explain`이 원인과 할 일을 돌려준다.
- 사용자는 `suggested_change_request`를 그대로 `change`에 보낼 수 있다.
- 사용자가 직접 요청하는 `rollback`은 되돌리는 변경 제안을 만든다. 적용 전에 미리보기와 확인을 거친다.

## 상태를 두는 곳

| 상태 | 위치 | 비고 |
|---|---|---|
| 업로드 | S3 객체(키 = sha256), 10분 뒤 미완료분 정리 | 서버 메모리 0 |
| 앱 소스·AI 수정·spec | 플랫폼 조직의 private 앱 레포(main + `jasmin/fix`) | 변경 이력이 곧 증거 |
| 실행 진행 | Actions run, Deployments API, `evidence.json` | `run_id` = run 참조 |
| 변경 미리보기 | 아티팩트(spec·저장된 plan) + `change_id` 서명 토큰(app, `base_rev`, `plan_hash`, requester, 만료 30분) | `plan_hash` = hash(base_rev, 렌더 결과, plan). apply는 저장된 plan만 적용. 1회용 소비 기록은 Deployments API에 |
| 확인 대기 | 2026은 무결성 보호된 `requestState` 왕복, 2025는 elicitation | 서버 저장 없음 |
| 사용자 인증 | 자체 포함 Jasmin JWT(8시간, aud=jasmin) | DB 없음 |

## 신·구 스펙 동시 지원

- 서버: 공식 SDK의 dual-mode 핸들러(TS `createMcpHandler(factory, { legacy: 'stateless' })`)
- 2026 요청에는 `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name` 헤더가 필수다. 헤더와 본문이 다르면 400, JSON-RPC `-32020`을 돌려준다. 이 헤더 덕분에 Traefik이나 게이트웨이가 도구 단위로 라우팅·제한을 할 수 있다.
- 2025 클라이언트는 `Mcp-Session-Id`를 받지만 서버에는 저장하지 않는다. 세션에 의미 있는 상태가 없으니 어느 복제든 처리한다.
- PoC 확인 항목: 로드밸런서 뒤 복제 2개가 affinity 없이 신·구 클라이언트를 모두 처리하는지, Claude Code가 헤더 Bearer로 원격 MCP에 붙는지

## 인증·인가·감사

- `/login`(GitHub OAuth) → Jasmin JWT. 클라이언트 토큰을 GitHub나 AWS로 넘기지 않는다(token passthrough 금지).
- 역할은 둘이다. `user`는 자기 앱만 다루고, `operator`는 전체 읽기와 `rollback`을 할 수 있다. 도구 인가는 MVP에서 서버 안에서 한다.
- 테넌트 경계를 앱 코드의 권한 검사에만 맡기지 않는다. 앱별 private 레포, 네임스페이스, App 토큰 범위로 한 번 더 막는다. Coolify·Dokploy가 권한 검사만 두었다가 다른 테넌트에 접근되는 취약점을 반복했다.
- 감사 로그 필드: `sub`, 도구, `app`, `upload_id`·`run_id`·`change_id`, 확인 여부, 요청자(사람)와 실행 주체(App·워크플로)

## 한도와 멱등성

- 업로드: 100 MB, 파일 2만 개, 10분 만료
- deploy: 사용자당 동시 1건, 하루 20건. LLM 예산은 실행당 상한(값은 PoC에서 측정 후 결정)
- `deploy`는 (업로드 sha256, request 해시)가 같으면 기존 run을 돌려준다. `apply`는 `change_id`당 한 번만 실행된다.

## 도메인·업로드 보안

- 호스트는 `<app>-<random6>.<platform-domain>`으로, 추측할 수 없게 한다. 앱을 지운 뒤 같은 이름을 남이 가져가는 것도 막는다.
- jasmin-api는 사용자 앱과 다른 등록 도메인에 둔다. 사용자 앱이 쿠키로 백엔드 세션을 건드리지 못하게 하려는 것이다. 앱 도메인을 Public Suffix List에 올리는 것은 확장 과제다.
- 업로드 검사: 경로 탈출(Zip Slip), 심볼릭 링크, 크기·개수, 서버 측 gitleaks. 비밀이 발견되면 배포를 거부한다(Netlify 방식). Dokploy는 zip 업로드 경로로 CVSS 9.9 원격 코드 실행 취약점을 냈다.

## Agent Gateway를 붙인다면 (선택 확장, QA-17)

- **쓸 때:** jasmin-api와 운영자용 읽기 전용 k8s·Argo MCP를 한 URL로 묶고, JWT 검증·도구 allowlist·접근 로그를 설정으로 처리할 때
- **조건:**
  - v1.5.0 고정, `jwtAuth.mode: strict`
  - stateless가 기본이고, stateful은 구 클라이언트용으로만 켠다. 이때 `SESSION_KEY`가 필수다. 설정하지 않으면 세션이 평문 base64로 인코딩된다.
  - CEL은 allowlist로 쓴다. deny 규칙은 평가 오류가 나면 통과시키므로(fail-open) 보조로만 쓴다.
- **효과:** 역할마다 `tools/list`가 달라진다. 일반 토큰에는 `rollback`이 보이지 않고, 호출하면 Unknown tool로 거부된다(9/29 v1.5.0 실측).
- **한계:** 게이트웨이는 프롬프트 인젝션을 막지 못한다. 안전의 근거는 plan/apply 분리, LLM 쓰기 0, 결정론 게이트다.
- 세션 재사용으로 인가를 우회한 사고(v1.4.0에서 수정)는 stateless에서는 구조적으로 생길 수 없다.
- LLM 에이전트(adapter·fixer·change·diagnoser)에게는 어떤 MCP도 주지 않는다. diagnoser가 쓰는 증거는 결정론 단계가 모은다.
