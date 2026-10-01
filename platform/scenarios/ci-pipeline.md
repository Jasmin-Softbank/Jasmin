# 결정적 CI와 제한된 SDK 개입

현재 구현은 **intake → 기존 상태의 결정적 gate → 필요한 경우에만 SDK adapter/fixer → 같은 gate 재검사**다. LLM은 pass/fail을 판정하지 않는다. 기존 `.jasmin/jasmin.yaml`이 있으면 먼저 그대로 검사하고, 스펙이 없을 때만 adapter가 packaging 초안을 제안한다. 호출 경로는 `platform/runner/run_agent.py`의 Codex / Claude Code Agent SDK다. CLI legacy 실행이나 LLM의 자체 성공 보고를 release 근거로 사용하지 않는다.

## 순서와 결과 계약

| 순서 | 소유 검사 | 현재 판정 근거 |
|---|---|---|
| L0 | patch 정책 | 원본 Git HEAD 대비 변경, writable/protected 경로, 삭제·binary·symlink·금지 패턴 |
| L1 | 배포 spec / Dockerfile 정적 검사 | JSON Schema, allowlist, non-root/exec-form, render 가능 여부. KEDA는 trusted `--enable-keda`와 profile을 명시한 경우만 허용 |
| Q | 코드 품질 | manifest/lock/toolchain 계획, 격리 설치 후 기존 lint/type/unit 검사, 실제 테스트 수와 skip 여부 |
| L2 | 이미지 빌드 | 선택된 Dockerfile/context로 단일 이미지 빌드, Docker image ID 기록 |
| L4 | 이미지 검사 | Trivy image `vuln,secret`, CRITICAL 및 크기 검사. 현재 `--ignore-unfixed`이므로 미수정 CVE 전부를 차단하는 정책은 아님 |
| L3 | 실행 검사 | 검사한 image ID로 기동, ephemeral Postgres/migration, readiness/route. 외부 서비스 secret이 필요하면 placeholder로 성공을 가장하지 않고 BLOCKED |

기본 순서는 `L0,L1,Q,L2,L4,L3`이다. 빈 값·알 수 없는 layer·중복·순서 위반은 `BLOCKED`다. 일부 layer만 실행하면 성공해도 `checks_ok=true`, `status=INCOMPLETE`, `ok=false`, `release_eligible=false`다. 테스트 0개, 미설치 checker, 없는 lock, 미지원 toolchain, Docker/설치 네트워크 부재, timeout/예외는 통과가 아니다. gate 전후 source digest가 다르거나 build 후 image tag가 다른 image ID를 가리켜도 release를 막는다. verdict는 `source_sha256`, `images`, `image_ids`를 후속 artifact 검증에 전달한다.

## 탐지 → 고정 실행 계획 → 격리 설치

`quality.py`는 `.git`, dependency/cache/build 디렉터리를 제외하고 지원 manifest의 디렉터리를 탐지한다. 업로드한 GitHub/GitLab workflow 자체를 실행하지 않는다. manifest에서 발견한 scripts와 wrapper도 비신뢰 코드이며 컨테이너에서만 실행한다. `RUN/quality-plan.json`에 runtime image, 설치·검사 명령, lock 정책, 지원 제한을 남긴다.

| 코드베이스 | 현재 준비·실행 | 명확한 제한 |
|---|---|---|
| JavaScript / TypeScript / Next.js + npm | 정확한 npm `packageManager`가 있으면 해당 버전을 컨테이너 `/tmp`에 설치. Node 22/24의 exact pin 또는 고정 baseline 선택, `engines.node`/Volta/버전 파일 교집합 검사, `npm ci --engine-strict`, 기존 lint, TS typecheck 또는 locked tsc, Jest/Vitest JSON 또는 아래 node:test JUnit 검사 | npm lock 필수. 단일 프로젝트의 pnpm/Yarn은 아래 manager profile을 사용하며 workspaces·아래 범위 밖의 test reporter는 UNSUPPORTED. Node·Next 버전의 완전한 정적 호환성 증명은 아님 |
| pnpm / Yarn | packageManager pin 또는 플랫폼 baseline, 단일 lock 선택. Corepack으로 정확한 manager를 준비하고 pnpm frozen lock / Yarn classic frozen lock / 현대 Yarn immutable install 실행. 이후 공통 lint/type/unit 보고서 처리 | 충돌하는 여러 lock·workspace는 BLOCKED. native fixture로 pnpm 10.17.1·Yarn 4.10.3을 검증했고 Yarn classic은 실검증 전이다. |
| Python / FastAPI | 고정 Python image와 uv 0.12.18. pyproject+uv.lock은 `uv sync --locked --all-groups`; requirements.txt는 실행 전용 hash lock 생성 후 `uv pip sync --require-hashes`. 앱 환경에 없는 checker만 ruff 0.16.9/mypy 2.3.1/pytest 9.1.1로 준비하고 기존 버전·설정 보존, lint/type/unit와 JUnit 수 검사 | `uv run --with-requirements`에 앱 freeze를 함께 넣고 원래 버전 일치 확인. 원본 manifest/lock/config 불변, Python 3.12/3.13 exact pin만 지원. requires-python 충돌·도구 의존성 충돌·기존 테스트 부재는 BLOCKED. 실행 전 resolution/환경 provenance를 기록하고 고정 파일 목록·1 MiB 한도·해시를 검증하여 `RUN/python-N/`에 원문을 보존(directory 0700, files 0600). verdict에는 경로와 해시를 남김 |
| Java / Spring + Maven | repository wrapper와 distribution SHA-256 사용, JDK 21 image, versioned quality-plugin check의 lifecycle binding 확인, `verify` 실행, Surefire JUnit 결과 확인 | Maven에는 npm/uv와 같은 transitive lock freshness가 없음. wrapper/JDK/runtime/toolchain은 다른 계약이며 다른 JDK·복잡한 parent/profile 구성은 후속 fixture 검증 필요 |
| Java / Spring + Gradle | repository wrapper+distribution checksum, dependency lock+verification metadata 필수, strict dependency verification 및 lock init script, `check`와 기존 Checkstyle/PMD/SpotBugs task, JUnit 결과 확인 | 버전 catalog/복합 build·다른 JDK·사용자 정의 test task는 일반 지원으로 주장하지 않음 |

고정 이미지에 runtime/기본 도구가 있고 프로젝트 도구가 lock에 선언되어 있으면 해당 정확한 의존성을 **실행 환경에 설치**한다. Python은 없는 검사 도구를 위 실행 전용 baseline으로 보충하며 설정이 없으면 도구 기본값을 사용한다. JS는 lint script/config가 없으면 실행 전용 ESLint와 최소 설정을 `/tmp/quality-tools`에 준비한다. TypeScript 설정이 있으면 누락된 tsc도 고정 버전으로 준비하고 원본 package/lock/config는 보존한다. 다른 profile의 지원 제한과 별개로 테스트가 없으면 계속 `NO_TESTS`다. 업로드한 manifest/lock/config 수정이나 최소 테스트 제안은 별도 diff와 승인 범위가 필요하며 현재 packaging/source fixer 모두 이 파일들을 수정하지 못한다. Jest 설치만으로 tests passed, `passWithNoTests`, 테스트 삭제·skip 추가, 생성한 테스트를 기존 검증처럼 표기하는 동작은 제공하지 않는다.

현재 Q는 lint/type/unit 및 Java compile/quality plugin 범위다. **source SAST 전용 엔진, coverage 임계값/변화량, 전체 dependency policy/SBOM/provenance, API 계약·브라우저 E2E는 아직 필수 gate로 구현되지 않았다.** Trivy image 검사가 이 항목들을 모두 대체한다고 표시하지 않는다. 추가 검사는 도구·설정·baseline과 fixture 결과를 함께 승인해야 한다.

Node 내장 `node:test`는 test script가 정확히 `node --test`와 저장소 안의 검증된 `.js/.mjs/.cjs` 파일·glob 인자로만 구성된 경우 지원한다. native JUnit 옵션을 파일 인자 앞에 넣으며 별도 npm pretest/posttest hook·추가 Node flag·shell 조합은 자동 변환하지 않는다. JUnit의 실제 testcase를 세고 failure/error/skip/todo가 있으면 통과시키지 않는다. Node가 빈 파일도 성공한 파일 testcase로 기록하므로 현재 profile은 `.js/.mjs/.cjs`로 끝나는 testcase 이름을 집계에서 제외한다. top-level assertion-only 파일과 그러한 이름의 명시 test case는 이 제한 때문에 실행된 테스트로 인정되지 않는다. Node의 [공식 reporter·glob 문서](https://nodejs.org/api/test.html#test-reporters)와 [reporter destination 옵션](https://nodejs.org/api/cli.html#--test-reporter-destination)에 근거하며, reporter 형식 변경은 통과 추정 대신 다시 검증한다.

## 실행 경계와 환경변수

Q 컨테이너는 UID 65532, read-only root, cap-drop ALL, no-new-privileges, 2 CPU/2 GiB/256 PID 제한을 사용한다. 원본은 read-only bind mount, 설치/출력은 제한된 tmpfs 사본에 기록한다. 호스트 shell에서 사용자 scripts를 실행하지 않고 Docker socket·홈·SSH·cloud/registry/LLM 자격을 넘기지 않는다. 각 프로젝트 timeout은 900초, 출력은 8 MiB 파일 제한이며 종료 시 컨테이너 제거를 시도한다. Docker daemon 장애로 제거에 실패하면 executor를 폐기해야 하며 성공으로 간주하지 않는다.

의존성 설치는 외부 registry 연결이 필요하다. **기본값은 설치 네트워크 미설정으로 BLOCKED**다. Q/L2/L3는 root 소유 network helper의 native 검증 receipt와 실행 프로파일 SHA-256 일치를 요구한다. 관리자는 `infra/ansible/test-ci-network.sh`로 해당 호스트의 실제 차단 경로를 검사해야 한다. `--quality-network` 이름만으로 승인하지 않으며 `host`, 기본 `bridge`, `none`도 허용하지 않는다.

설치기·gate·native 검증기는 [공통 CI 실행 프로파일](../contract/ci-executor.yaml)의 BuildKit 이미지·네트워크·자원 제한을 읽는다. L2는 이 네트워크에 연결한 전용 BuildKit을 쓰고, L3는 실행별 internal bridge에서 임시 DB와 앱만 연결한다. Q/L2의 public TCP 80/443·DNS 허용은 registry 도메인 allowlist가 아니다. L3 외부 egress 요청은 현재 `RUNTIME_EGRESS_UNSUPPORTED`로 차단한다. 새 경로의 실제 Linux Docker/클러스터 인수 시험은 아직 수행하지 않았으며 과거 fixture PASS로 대신하지 않는다. L4 Trivy scanner의 기본 네트워크 제한은 별도 잔여 항목이다.

Docker는 적대적 코드에 대한 VM 격리 대체재가 아니다. 다중 사용자 서비스에서는 credential-free 실행과 trusted SDK/release 실행을 별도 worker로 분리해야 한다. 현재 관리자 전용 PoC workflow의 SDK/CI 동거를 다중 사용자 격리 완료로 보지 않는다. trusted 이미지 publish/GitOps write·CD 관측은 보호된 전용 runner 경계로 분리한다.

사용자 `.env*`는 intake가 거부한다. 값은 향후 별도 secret ingestion/사용 시점별 주입 경로에서 처리해야 한다. env가 없어도 일부 static compatibility/lock 판단은 가능하지만 빌드·실행·외부 연동 성공은 보장할 수 없다. Next `NEXT_PUBLIC_*`처럼 build 시점 값이 필요한 경우도 별도 입력 계약이 필요하다. 현재 실제 외부 서비스 secret 검증은 미구현이며 `MISSING_SECRET`을 반환한다. local Postgres는 `app_owner` migration 권한과 DML/sequence만 허용한 `app_rw` runtime 권한을 나눈다. 기본 schema 소유권·owner의 default grants·migration 뒤 existing grants를 설정한다. credential은 임시 fixture용이며 운영 CNPG/DB role 검증의 대체물이 아니다.

## SDK 개입과 중단

Codex 모델은 사용자 지정 `gpt-6.1-sol`, SDK는 `openai-codex==0.159.3`이다. 기본 read-only는 루트 전체 읽기를 허용하므로 `railshot_read` permission profile을 사용한다. 명령의 읽기는 작업 사본·계약·schema·run과 최소 OS/정확한 읽기 도구 실행 파일로 제한하고 인증 home, 그 밖의 경로, 쓰기와 명령 네트워크는 차단한다. legacy `sandbox=read-only` 인수는 profile을 덮어쓸 수 있어 전달하지 않는다. 이 경계는 command 실행에 적용되며 MCP·브라우저·첨부 파일 접근까지 검증한 주장이 아니다. 관리자 인증 home에는 인증 외 설정·MCP 연결을 넣지 않는다. 실제 native/SDK 도구 검증은 [CI-19](ci-validation.md)에 기록한다.

baseline이 이미 통과하면 SDK 호출은 0회다. `--repair-scope packaging`이 기본값이며 SDK 제안은 기존 `paths.yaml`의 Dockerfile, `.dockerignore`, `.jasmin/**`에만 적용한다. 관리자가 **trusted CLI의 `--repair-scope source`를 명시**하면 실제 Q lint/type/unit 명령이 실행되어 FAIL이고 `source_repair_eligible=true`인 경우에만 fixer의 해당 호출에 source scope를 전달한다. adapter나 packaging/build 실패의 fixer는 packaging scope를 유지한다. 프로젝트 `.jasmin` 값이나 LLM 응답으로 scope를 올릴 수 없다. 이는 현재 운영자가 source patch 승인 범위를 고르는 경로이며, 상용 웹콘솔 Allow·권한 승인 서비스가 구현됐다는 뜻은 아니다.

source scope의 허용 대상은 Python/JS/TS/JSX/TSX/Java 소스다. 기존 테스트, package/lock, CI, migration/data schema/model, generated/vendor, lint/type/test 설정, 정책·계약 파일은 추가 deny 규칙으로 보호한다. 변경된 줄의 `noqa`, `eslint-disable`, `@ts-ignore`/`@ts-nocheck`, `type: ignore`, `SuppressWarnings` 등 검사 우회도 금지한다. 이미 있던 우회 문자열을 읽었다는 이유로 실패시키지는 않는다. gate L0와 runner writer가 같은 `writable_rules(..., scope=...)` 계약을 사용한다.

Q의 준비/설치 단계 오류, 도구·lock·테스트 부재, missing secret, 인프라 실패/timeout, 미지원 기능은 source scope에서도 중단한다. 검사 실행 단계와 마스킹한 진단을 failure evidence에 남기고 결과는 다시 전체 gate로 검증한다. 수정 호출은 최대 3회이고 같은 실패 signature를 두 번째 관찰하면 중단한다. `max-attempts=0`은 순수 결정적 진단이다. SDK subprocess도 1,800초로 제한한다. 구독 SDK가 비용 값을 제공하지 않으면 비용은 `null / unknown`이며 무료로 계산하지 않는다.

Q의 단계 판정은 플랫폼 부모 셸의 EXIT trap이 기록한 종료 코드만 사용한다(prepare 201, lint 202, type 203, unit 204, report 205). 업로드 코드의 stdout에 있는 `RAILSHOT_STAGE` 문자열은 판정 근거가 아니다. 자식 명령이 같은 종료 코드를 반환해도 부모가 실제 실행 단계로 다시 매핑한다. signal·명령 실행 불가·예상하지 못한 종료 상태는 BLOCKED다. 품질 실패의 `failure_signature`는 프로젝트 경로·실제 검사 단계·마스킹하고 정규화한 진단의 digest를 포함하므로 서로 다른 lint 오류를 같은 실패로 합치지 않는다.

프로젝트 결과에는 경로·검사 단계·정규화한 실패 진단의 안정적인 fingerprint를 남긴다. ANSI 색상·공백·진단 위치의 line/column·실행 시간 차이는 정규화하지만 파일명과 오류 내용은 유지한다. 현재는 프로젝트 단위 결과이며 GitLab Code Quality의 개별 rule·파일·line·severity report와 호환한다고 주장하지 않는다. tool-native 보고서 수집을 추가할 때 해당 필드와 안정적인 fingerprint를 유지한다.

## 검증 기록과 다음 인수 시험

`test_quality_boundary.py`는 로컬 `sh` 부모/자식 프로세스로 stdout 단계 위조, 예약 종료 코드 위조, signal·없는 명령·unset 변수·준비 실패, 신뢰된 export 유지, 예상하지 못한 host 종료 코드 차단을 확인한다. 서로 다른 진단·프로젝트·검사의 fingerprint 구분과 동일한 정규화 진단의 안정성도 검사한다. Docker 호출은 모의 처리하며 실제 컨테이너·품질 도구 통합 검증은 아니다.

오프라인 `test_pipeline.py`는 layer 순서/빈 입력/partial release 차단, structured timeout, Q→build 차단, lock/checker/test 부재, Docker 권한·mount 정책, 0/skip 테스트, 반복 signature, baseline 0 SDK 호출, build 경로 traversal 거부, 임시 DB role SQL 계약, image ID 전달, source 변경 차단, 구독 비용 unknown을 검증했다. 추가로 기본 Q 실패 중단, 명시 source scope의 실제 checker 실패 허용, 준비/환경 실패 차단, source 경로에서도 test/config/data 보호를 검증했다. Python 3.13 + PyYAML/jsonschema 환경에서 gate/loop 기존 self-test도 통과했다. 이 테스트는 실제 이미지 pull, dependency install, 품질 명령, SDK 호출, Docker 격리/egress, build→scan→runtime 통합 성공의 증거가 아니다.

실제 profile별 정상·실패 실행, SDK 수정, artifact 검증과 미지원 옵션은 [단일 검증 기록](ci-validation.md)에 기록한다. 위 로컬 테스트 수를 live 실행 수로 합산하지 않는다. private 설치원·외부 secret·도메인·실제 release workflow는 별도 운영 입력과 인수 시험이 필요하다.

## 근거와 구현 선택

- [npm ci](https://docs.npmjs.com/cli/v11/commands/npm-ci/): lock 불일치 시 실패하고 lock을 갱신하지 않는 공식 동작을 설치 명령에 사용했다.
- [uv locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/): `--locked` freshness와 `--frozen`의 차이에 따라 `--locked`를 선택했다.
- [uv run dependencies](https://docs.astral.sh/uv/concepts/projects/run/)와 [uv compile/sync](https://docs.astral.sh/uv/pip/compile/): 도구 overlay가 프로젝트 요구 버전을 덮어쓸 수 있으므로 앱 freeze를 함께 제약하고, requirements-only 입력은 실행 전용 hash lock으로 설치한다.
- [Maven lifecycle](https://maven.apache.org/guides/introduction/introduction-to-the-lifecycle.html): `verify` 이전 단계와 바인딩된 plugin 실행을 재사용한다. 모든 POM의 품질 plugin이 자동 실행된다는 의미는 아니다.
- [Gradle dependency locking](https://docs.gradle.org/current/userguide/dependency_locking.html) 및 [dependency verification](https://docs.gradle.org/current/userguide/dependency_verification.html): dependency version lock과 checksum/signature 검증을 별도로 요구했다.
- [GitHub self-hosted runner security](https://docs.github.com/en/actions/reference/security/secure-use#hardening-for-self-hosted-runners): runner 등록 해제만으로 환경이 깨끗해지는 것은 아니므로 credential-free 실행 및 job 이후 VM 재사용 경계를 분리한다.
- [GitLab Code Quality report](https://docs.gitlab.com/ci/testing/code_quality/#code-quality-report-format): fingerprint와 rule/location/severity 구조를 향후 tool report 확장의 기준으로 삼되 현재 구현 범위를 구분했다.


## 상태와 재개

`RUN/state.sqlite3`를 local CI 실행의 정본으로 둔다. 단계 시작 의도, 완료 checkpoint 참조·hash, run/attempt ID와 append-only events를 SQLite transaction으로 함께 저장한다. 실행 전체에 Unix file lock을 잡아 중복 writer를 막는다. `evidence.json`은 최종 checkpoint에서 재생성 가능한 출력이며, SDK sidecar·gate verdict는 개별 실행 증거다. 별도 message bus, provider별 작업 DB, 임의 shell-hook 실행기는 추가하지 않는다.

```bash
python platform/loop/loop.py UPLOAD RUN --provider codex --repair-scope source --quality-network railshot-quality
# 같은 입력·설정·harness에서만 완료 checkpoint를 이어 사용한다.
python platform/loop/loop.py UPLOAD RUN --provider codex --repair-scope source --quality-network railshot-quality --resume
```

- upload/run은 겹칠 수 없고 새 run은 빈 디렉터리여야 한다. 기존 run의 초기화·workspace/lessons 삭제는 거부한다.
- source/request/config/harness와 작업 사본·checkpoint·native artifact hash가 달라지면 기존 통과 판정을 재사용하지 않는다. 새 입력은 새 run이다.
- 완료된 intake/agent/gate/lesson은 재호출하지 않는다. 예산·attempt와 SDK ID도 기존 값을 사용한다. 완료된 evidence 파일만 지워져도 모델 호출 없이 복구한다.
- 실행 중 프로세스가 죽거나 SDK 결과가 불확정이면 `UNKNOWN / after_reconcile`이다. 완료 기록 없는 단계의 재개는 `STATE_INFLIGHT_UNCERTAIN`으로 중단한다. 같은 모델 호출·migration·dispatch를 안전하다는 추정으로 다시 실행하지 않는다. 운영자가 실제 작업과 receipt를 대조해야 하며 무조건 retry하는 복구 스위치는 없다.
- Codex/Claude 공통 lifecycle은 실제 SDK ID와 started/finished/unknown을 보존한다. Codex thread/turn, Claude session을 같은 `run_id`·`attempt_id`에 연결한다. raw tool 인수·출력·토큰을 event에 넣지 않는다. private `*-session.json`과 `*-events.jsonl`은 진단 증거이며 작업 상태 DB의 대체물이 아니다.
- **native 대화 resume는 현재 미지원**이다. Codex의 ephemeral thread, Claude의 정책 재결합을 검증하지 않은 채 session ID만으로 재호출하지 않는다. `--resume-session-id`는 거부한다. SDK session 완료는 CI 통과나 CD 완료가 아니다.
- 단일 머신·단일 writer의 범위다. GitHub dispatch·registry push·GitOps commit·제품 Allow/SSE는 이 DB와 아직 하나의 영속 작업으로 연결되지 않았다. `RAILSHOT_RUN_ROOT`는 checkout 밖의 보호된 영속 경로로 두고 SQLite/checkpoint 원문은 공개 artifact로 업로드하지 않는다.

현재 local event: `run.started/resumed/resume_blocked/completed`, `step.started/checkpointed/interrupted`. `checkpointed`는 저장 완료이며 검사 성공이 아니다. 오류/결과/원인 처리의 정본은 [관측성 규약](../contract/observability.md)이다. `step`은 `intake:0`, `gate:N`, `agent:N`, `lesson:N`처럼 안정된 식별자를 가진다. 제품 API의 `approval.required/resolved`와 최종 `deployment.verified`는 [CONTROL 계약](../CONTROL-PLANE-PLAN.md#6-allow-카드와-이벤트-훅)에 남아 있으며 아직 UI와 연결되지 않았다. approval·권한 실패를 provider hook의 기본값으로 통과시키지 않는 원칙을 유지한다.

검증은 `python -m unittest discover -s platform/loop -p 'test_*.py'`다. 실제 자식 프로세스 강제 종료, 완료 단계 재사용, 중복 writer, 입력·artifact 변조, uncertain SDK 결과를 모델 호출 없이 검사한다. 이는 클라우드 worker 장애 복구 E2E의 통과가 아니다.
