# CI 구현·검증 실행 계획

갱신: 2026-10-01. **이 파일이 CI의 현재 작업 순서·옵션별 검증 상태·실패 기록을 관리하는 단일 계획이다.** [ci-pipeline.md](ci-pipeline.md)는 gate/수정 권한의 구현 계약, [CONTROL-PLANE-PLAN.md](../CONTROL-PLANE-PLAN.md)는 제품 API·승인·VM·배포의 상위 계획이다. 같은 체크리스트를 다른 문서에 복제하지 않는다.

목표는 업로드의 기존 빌드 방식과 의존성을 보존하면서, 플랫폼이 탐지→필요 도구 준비→검사→허용 수정→같은 검사→검증 이미지 export까지 수행하는 것이다. 사용자가 checker 설치 명령을 직접 알아야 하는 흐름을 기본값으로 삼지 않는다. 이 목표에 필요한 자동 준비는 아래 미구현 항목을 포함하며, 현재 모든 업로드를 지원한다는 뜻은 아니다.

## 1. 증거와 완료 기준

- **코드/모의 검사:** 계획·경계·판정 분기·subprocess 계약을 검사한 상태다. 실제 registry 설치·모델·Docker 성공과 구분한다.
- **live full gate:** 자격 없는 별도 CI VM에서 `L0,L1,Q,L2,L4,L3`를 모두 실행하고 검사 수·exit code·source/image hash를 보존한 상태다. 일부 layer의 성공은 release가 아니다.
- **수정 E2E:** 실제 실패→선택한 SDK JSON patch→보호 파일 불변→전체 gate 성공. 이번 실호출 대상은 Codex `gpt-6.1-sol`이며 Claude는 아래 사용 제한에 따라 제외한다. 기존 Codex 인증 JSON 응답은 이 검증의 대체물이 아니다.
- **artifact E2E:** full gate가 검사한 image ID를 export/load/publish하고 registry digest를 대조한다. GitOps·Argo·URL은 후속 배포 인수로 구분한다. [클라우드 기록](cloud-validation.md)의 GCP/KEDA 성공을 앱 CI 성공으로 재사용하지 않는다.

receipt에는 실행 ID, 시작/종료 시각, 코드 commit와 dirty source bundle hash, fixture hash, profile/tool/image 판본, OS/architecture, 실제 명령·exit·test count, source/spec/verdict/image/bundle hash, 실패 서명, 관련 issue ID를 남긴다. 비밀·전체 Terraform state·원문 인증은 넣지 않는다. 실패한 실행은 지우지 않고 새 attempt로 재검증한다. 로그가 없거나 종료 상태가 불확실하면 PASS를 만들지 않는다.

## 2. 현재 옵션 매트릭스

현재 기본 Q 입력은 `gate/quality.py`가 해석한다. `prepare.py`의 읽기 전용 build-root grouping을 `discover()`가 공유한다. 선언된 Java/JS 자식을 묶는 기능과 해당 multi-project의 실제 실행 지원은 구분한다. 아래 live 열은 실제 receipt를 확보한 실행자가 갱신한다.

| ID | 업로드 옵션·현행 전제 | 코드/모의 검증 | live full gate | 우선 추가 fixture |
|---|---|---|---|---|
| C01 | JS/npm: package-lock 또는 shrinkwrap, Node 22/24, 기존 lint 또는 run 전용 고정 ESLint, 기존 Jest/Vitest | planner·lock/0 tests·stage·외부 도구 준비 검사 존재 | PASS — r4/r7 동일 fixture full gate 재검증, unit1·release_eligible=true | 정상, manifest/lock 불일치, lint 실패, unit 실패, `node:test` reporter |
| C02 | TS/npm: C01 + 기존 typecheck 또는 locked tsc/tsconfig | type 명령 생성·Q 실패 분기 | PASS — r4/r7 동일 fixture full gate 재검증, unit1·release_eligible=true | 타입 오류, tsconfig 부재, build script 없음/있음 |
| C03 | Next.js/npm: C01/C02 + 실제 build/start·route | Next 의존성 탐지, 기존 Dockerfile 경로 | PASS — r4/r7 동일 fixture full gate 재검증, unit1·release_eligible=true | production build, public build env, runtime route, 외부 secret 누락 |
| C04 | FastAPI/Python: pyproject+uv.lock, Python 3.12/3.13, 기존 도구 보존·없는 Ruff/mypy/pytest만 별도 overlay | locked sync·앱 dependency 보존·검사 계획 | PASS — Python3.12.14 r6/r8 full gate 재검증·unit1·release_eligible=true; r4 L4 실패 보존 | 정상, requires-python 충돌, missing dependency, import/env 실패 |
| C05 | Spring/Maven: wrapper+distribution SHA, JDK 21, versioned quality check의 lifecycle binding, 기존 tests | verify 계획·plugin 검사·JUnit 집계 | PASS — r6 정상 full gate, unit1·release_eligible=true | 정상, compile/check 실패, wrapper 오류, reactor parent/child |
| C06 | Spring/Gradle: wrapper SHA, dependency locks·verification metadata, 기존 quality plugin·tests | check/strict verification·JUnit 집계 | PASS — r7 정상 full gate, unit1·release_eligible=true | 정상, lock/checksum 오류, settings 기반 multi-project |
| C07 | pnpm / Yarn: 단일 manager lock·명시 pin 또는 플랫폼 baseline | native manager 선택·frozen/immutable 명령 로컬 구현. workspace 실행은 계속 미지원 | PASS — pnpm10.17.1·Yarn4.10.3 정상 full gate, 각각 unit1·release_eligible=true | pnpm frozen lock, Yarn 1/현대 Yarn 구분, PnP/Zero-Install |
| C08 | requirements-only Python | run 전용 hashed requirements compile/sync·앱 버전 보존 overlay 구현 | PASS — r7 checker 자동 준비·기존 앱 버전/원문 유지·해결 artifact5개 hash/0600·full gate | pinned/unpinned requirements, constraints·`-r`, 별도 resolved artifact |
| C09 | checker/config/tests/lock 없는 초보자 업로드 | 일부 checker 외부 준비 구현. 새 lock·test 생성/범용 설정 준비는 미구현, NO_TESTS 유지 | 부분 PASS — devDependencies/ESLint config 없는 Node:test fixture의 lint 자동 준비·unit1·full gate; tests 없는 앱은 계속 차단 | 자동 도구 준비·generated smoke 근거·freeze 후 수정 제한 |
| C10 | 여러 서비스/중첩 프로젝트·metadata/example manifest | read-only root graph·명시 선택·제외 근거 구현, 집중 테스트 9개 PASS. Java 그룹 실행과 workspace 지원은 별도 | NOT_RUN | frontend+backend, npm workspace, Java parent/child, docs/examples 제외 |

실제 CI는 자격 없는 별도 GCP `railshot-ci-e2e`(4 vCPU/16 GiB, boot 60 GiB, native 2시간 STOP)에서 실행했다. 이 실험은 CLI로 생성했으며 공통 Terraform state가 관리한다고 주장하지 않는다. npm/PyPI/Maven HTTPS 200과 metadata/private/host/non-HTTP 차단을 실제 확인했고 각 차단의 DROP counter가 2 증가했다. 정상 fixture 10종은 각각 unit 1개를 포함한 `L0,L1,Q,L2,L4,L3` PASS와 `release_eligible=true`를 확인했다. 위 표의 PASS는 명시한 fixture 판본에 한정하며 workspace·모든 업로드 지원을 뜻하지 않는다.

JS/TS/Next는 r4/r7 정상 실행을 재확인했고, FastAPI는 오래된 base의 L4 실패를 보존한 뒤 Python3.12.14의 r6/r8에서 통과했다. JS/TS/Next negative는 20건 기대 판정 일치·순수 JS 타입 검사 1건 N/A, FastAPI negative는 6건 일치·별도 lock이 필요한 missing-tool 1건 N/A다. 코드 검사 오류는 정확한 stage의 FAIL, env/tool/lock/tests 부재는 근거가 있는 BLOCKED여야 일치로 센다. Maven/Gradle 14건, pnpm/Yarn 6건, native Node 3건도 기대 stage/reason과 실제 원인까지 일치했다. 합계는 negative 49건 MATCH·2건 N/A이며 MISMATCH가 남은 재검증 항목은 없다. 이 합계는 위 명시 fixture 범위이며 negative 일치는 release 성공이 아니다.

판본별 근거는 `source-manifest.json`과 case `result.json`/`run/verdict.json`에 있다. JS full/env r7의 `quality.py` SHA256은 `258c022526fa66d2a42407f6f2f82149b32365670e7f4b5f30be4dfe58f7f67b`, JS 나머지 negative r5는 `8398f623ac5d0ea94f2b4bd8297eb135578d5405444736659fbb781142ae0cc6`다. 최종 Python full/negative r8은 `ab5d1bf98710de4b179680c942b84c6a747cdd1ca0cc6aa6ba903f4a503d856f`이며 해결된 의존성 원문 4개를 run의 0700 디렉터리/0600 파일로 저장하고 hash 일치를 확인했다. JS/Python 원문은 로컬 `~/.local/state/railshot/ci-e2e/js-python-r7` 및 `js-python-r8`, Java 최종 matrix와 209개 증거는 `~/.local/state/railshot/ci-e2e/java-review-r7`에 보존한다. r1–r3 준비 실패와 r4 이후 실패 기록은 삭제하지 않았다.

모델 범위: AWS control에서 `gpt-6.1-sol` SDK를 실제 1회 호출해 unit 실패의 `src/app.js` 한 파일을 수정했다(38.423초). 공통 writer·보호 파일 불변을 확인한 뒤 자격 없는 CI VM에서 전체 gate PASS·release_eligible=true를 확인했다. 근거는 `/opt/railshot/evidence/codex61-repaired-r6/repair-receipt.json` 및 `run/verdict.json`이다.

증거 보존: 로컬 `~/.local/state/railshot/ci-e2e/ci-final-matrix.json`이 현재 판정을 모은다. `root-evidence-r8`에는 SDK 두 역할과 이미지 bundle 104개 파일을 보존했다. `ci-audit-all`은 CI VM의 실패 로그·source·fixture 등 추가 5,430개 파일을 보존하며, archive SHA256 `b3b2aa45f710578c30f005eab146f5def6b1f48e0cebbebccb49a79eaa17f7e1`과 내부 파일 해시를 모두 대조했다. 표의 원격 `/opt/railshot` 경로는 이 로컬 archive 안에서도 확인할 수 있다. 도구 cache/build 출력은 제외하고 검사한 이미지 tar는 `root-evidence-r8`에 별도 보존했다.

2026-10-01 17:06 KST에 임시 CI VM `railshot-ci-e2e`와 60 GiB boot disk의 삭제를 API 목록에서 확인했다. 실행 중 검사·컨테이너가 없는 상태에서 증거를 보존한 뒤 삭제했다. 기존 앱 VM `railshot-gcp-poc`는 `TERMINATED`를 재확인했으며 보존 디스크와 AWS control은 유지했다. 근거는 같은 private 디렉터리의 `ci-cleanup-receipt.json`이다.

이는 각 VM의 제품 CLI를 운영자가 조합한 구성 요소 E2E이며, 제품 API 자동 dispatch·Allow·GitHub workflow 전체 실행·Argo 실앱 배포를 검증한 것은 아니다. 테스트용 loopback registry의 export/load/push·digest·변조/부분 gate 차단은 확인했고 GHCR production publish는 미검증이다.

추가 packaging 실험은 L1 spec 부재에서 adapter를 1회 호출했다(67.233초). Dockerfile·.dockerignore·.jasmin/jasmin.yaml 3개만 생성하고 모든 원본 앱·테스트·lock 불변을 확인한 뒤 전체 gate가 통과했다. 근거는 `/opt/railshot/evidence/codex61-adapter-r1/adapter-receipt.json`과 `baseline/verdict.json`·`run/verdict.json`이다. 이번 CI 실험의 수정 SDK 성공 호출은 source repair 1회와 packaging adapter 1회이며, 앞선 준비·전송 실패 3건은 모델 호출 전 실패로 구분한다.

Claude는 사용자 지시에 따라 **로그인·유료 호출을 하지 않는다**. 현재 로그인 false·환경 키 없음이며 Ollama/LM Studio도 설치돼 있지 않다. 무료 대안은 적합성 조사만 한다. Ollama의 Anthropic-compatible endpoint로 Claude Code를 연결하는 공식 경로는 있으나 모델/기능 제약이 있고, 이 프로젝트의 Claude Agent SDK schema·hook·tool 동작을 검증한 것은 아니다. 로컬 모델 실행은 NOT_RUN이며 cloud 모델을 무료로 가정하지 않는다. [Ollama Claude Code 연결](https://docs.ollama.com/integrations/claude-code)

## 3. 준비 단계의 최소 확장안

아래는 공식 도구 동작을 바탕으로 한 **최소 구현 방향**이다. 일부 로컬 구현은 위 매트릭스에 반영했으며 모든 항목이 완성된 것은 아니다. 한 공통 plan에 실행할 단계와 근거를 남기고, 기존 `quality.py`의 명령 planner를 재사용한다. 별도 stack framework나 앱별 CI 생성기를 늘리지 않는다.

1. **업로드 구조를 먼저 선택한다.** manifest·lock·wrapper·settings·기존 spec의 context에서 서비스와 build root를 정한다. package manager가 명시되면 그대로 보존한다. 충돌하는 lock 여러 개·복수의 독립 실행 앱처럼 의미를 확정할 수 없는 경우만 짧은 선택을 요청한다. Maven reactor와 Gradle settings가 선언한 자식은 부모 wrapper로 실행하며 모든 child manifest를 독립 앱으로 검사하지 않는다. docs/examples는 제외 근거를 남기고 명시 선택 시 포함한다. [Maven reactor](https://maven.apache.org/guides/mini/guide-multiple-modules.html), [Gradle multi-project](https://docs.gradle.org/current/userguide/multi_project_builds.html)
2. **build 필요 여부를 구분한다.** JS는 기존 `build` script가 없으면 컴파일 단계를 `not_applicable`로 기록하고 unit/runtime 검사는 유지한다. TS·Next·Spring은 실제 선언의 compile/build/package와 산출물 경로를 읽는다. 임의 `npm run build`, JAR 이름, start command를 모든 앱에 강제하지 않는다. Next 16에서는 `next build`가 lint를 실행하지 않으므로 lint 성공을 별도로 확인한다. [Next 16 변경](https://nextjs.org/docs/app/guides/upgrading/version-16), [Maven lifecycle](https://maven.apache.org/guides/introduction/introduction-to-the-lifecycle.html)
3. **설치는 자동으로, lock 변경은 명시적으로 처리한다.** 기존 lock은 npm `ci`, pnpm `install --frozen-lockfile`, 현대 Yarn `install --immutable`로 재현한다. Yarn 1은 별도 `--frozen-lockfile` profile이다. 빈 cache에서 `--immutable-cache`를 무조건 쓰지 않는다. 기존 lock 충돌을 자동 재해석으로 숨기지 않는다. lock이 처음부터 없으면 격리 resolver가 별도 candidate/준비 artifact를 만들고 hash를 고정한다. 앱 원본이나 기존 dependency 범위를 조용히 덮어쓰지 않는다. [npm ci](https://docs.npmjs.com/cli/v11/commands/npm-ci/), [pnpm install](https://pnpm.io/cli/install), [Yarn install](https://yarnpkg.com/cli/install), [Yarn 1](https://classic.yarnpkg.com/lang/en/docs/cli/install/)
4. **checker가 없으면 run 전용 도구 묶음을 준비한다.** 정확한 도구 판본·외부 config·적용 경로를 플랫폼 profile로 고정한다. JS/TS에는 해당 parser와 비스타일 중심 lint baseline, Python에는 기존 Python 범위에 맞는 Ruff/타입 검사 baseline을 사용한다. 기존 lint/type 설정이 있으면 이를 우선하며 새 profile로 약화하지 않는다. 외부 Ruff config와 ESLint `--no-config-lookup --config` 같은 경로는 앱 파일 수정 없이 신규 baseline을 제공하는 수단이다. 실제 패키지·parser 호환성을 fixture로 확인한 뒤 version을 고정한다. [Ruff config](https://docs.astral.sh/ruff/configuration/), [ESLint CLI](https://eslint.org/docs/latest/use/command-line-interface)
5. **requirements-only를 uv 프로젝트로 바꾸지 않는다.** 기존 requirements/constraints를 입력으로 `uv pip compile` 결과를 run의 별도 lock artifact에 저장하고 `uv pip sync`로 새 venv를 채우는 profile을 추가한다. 기존 pinned 값은 유지하고 동적 URL·private registry는 별도 근거와 자격이 필요하다. 앱 의존성과 checker 의존성을 구분해 두 lock/hash를 기록한다. [uv requirements locking/sync](https://docs.astral.sh/uv/pip/compile/)
6. **테스트가 없을 때 검사 생성을 숨기지 않는다.** 플랫폼이 확인 가능한 entrypoint/import/HTTP 계약에 대한 작은 generated smoke를 별도 준비 artifact로 만들 수 있다. 사용자 요구의 업무 정답을 추측한 테스트로 기능 검증을 보증하지 않는다. `existing_unit`과 `generated_smoke`의 수·범위를 따로 기록하고, 필수 기존 unit의 NO_TESTS를 generated smoke 결과로 덮어쓰지 않는다. 정책이 허용할 검사 수준·release 범위를 profile에 명시해야 하며 준비 뒤 fixer는 테스트를 수정하지 못한다. pytest exit 5는 미수집이며 PASS가 아니다. Node 기본 test runner도 JUnit을 제공하므로 JS의 최소 테스트에 Jest 설치를 강제할 필요는 없다. [pytest exit codes](https://docs.pytest.org/en/stable/reference/exit-codes.html), [Node 22 test reporter](https://nodejs.org/docs/latest-v22.x/api/test.html)

준비 결과에는 `source_evidence`, 서비스/build root, runtime·package manager pin/출처, install/lint/type/unit/build 명령, 기존/생성된 설정의 hash, 자동 처리할 일, 필요한 사용자 정보, blocker를 남긴다. 준비가 끝나면 검사 계약을 freeze하고 source fixer와 packaging adapter가 바꾸지 못하게 한다. 사용자에게는 “Python 앱을 확인해 검사 도구를 준비했습니다. 기존 코드 변경 없음”처럼 결과와 필요한 선택만 보여준다. 설치 명령·내부 IAM은 기본 사용자 흐름에 노출하지 않는다.

현재 `prepare.py`는 위 내용 중 root graph와 기존 quality plan의 읽기 전용 요약만 제공한다. Maven `<modules>`, 단순 Gradle `include`, JS workspace globs를 묶고 명시 선택의 경로 이탈·symlink를 거부한다. Gradle 동적 include/custom projectDir/composite build, workspace 실제 실행, 검사 없는 앱의 전체 자동 준비는 미지원이다. `READY`는 계획 생성 상태이며 실행 성공이 아니다. build 명령은 기존 package script가 있을 때 후보로 기록하고 실행하지 않는다.

## 4. 실행 순서와 현재 명령

순서는 **P0 환경·fixture → P1 기존 정상 baseline → P2 준비 누락/빌드 다양성 → P3 실패·수정 → P4 이미지 artifact**다. 각 구현 후 해당 실패 fixture와 정상 fixture를 재실행한다. 새 profile은 아래 매트릭스의 실제 성공·실패 증거가 모두 있어야 지원 완료로 올린다.

| 단계 | 실행할 내용 | 종료 조건 |
|---|---|---|
| P0 | 별도 자격 없는 CI VM, metadata/host/private-network 차단과 registry egress, CPU/RAM/disk·timeout·cleanup, 고정 linux/amd64 image 준비 | Docker/Buildx/Trivy·설치 경로 실제 확인, 모델·GitOps·cloud 자격 접근 불가, 비신뢰 실행 뒤 executor 폐기/정리 근거 |
| P1 | C01–C06의 기존 checker/lock/tests가 있는 정상 fixture | 모든 gate PASS, 모델 0회, positive test count, 같은 입력 두 번째 실행과 hash 근거 |
| P2 | C07–C10 및 build 없음/있음·monorepo·missing checker/config/lock/tests | 자동 준비 가능한 것은 재현 가능한 plan/candidate로 처리; 정보 부족만 명확히 차단. 기존 검사 약화·앱 원본 자동 변환 없음 |
| P3 | 각 stack의 lint/type/unit 실패와 준비/네트워크/secret 실패; Codex gpt-6.1-sol source repair와 packaging repair | 적격 실패만 수정, 같은 실패 2회·최대 3회, 보호 파일 불변, SDK 오류/미완료 차단, 수정 후 전체 gate 재실행. Claude 실호출은 이번 지원 완료 범위에서 제외 |
| P4 | bundle export→load→등록, tag/source/bundle 변조·부분 gate·이미지 불일치 | 검사한 image ID가 그대로 등록되고 새 registry digest를 기록. CI VM에는 publish 자격 없음 |

Python 3.13+, PyYAML/jsonschema, Git이 있는 저장소 루트에서 오프라인 검사를 실행한다. 아래 테스트는 실제 SDK/Docker 성공을 증명하지 않는다.

```bash
python3.13 platform/runner/run_agent.py --self-test
python3.13 platform/gate/gate.py --self-test
python3.13 platform/loop/loop.py --self-test
python3.13 -m unittest discover -s platform/runner -p 'test_*.py'
python3.13 -m unittest discover -s platform/gate -p 'test_*.py'
```

업로드 구조·준비할 일만 읽는 현재 CLI는 다음과 같다. `--selected-root`는 업로드 안의 상대 경로이며 자식 모듈을 선택하면 native build root로 묶는다. 필요한 설정/테스트가 없어 exit 2를 반환해도 JSON 계획과 blocker는 출력한다.

```bash
python3.13 platform/gate/prepare.py "$FIXTURE"
python3.13 platform/gate/prepare.py "$FIXTURE" --selected-root services/api
```

실제 fixture와 새 run 경로, 검증한 named Docker network를 운영자가 제공한다. 아래 baseline은 모델을 호출하지 않는다. 실패 시 exit code와 `evidence.json`·`gate-0/verdict.json`을 남기고, 성공처럼 처리하지 않는다.

```bash
python3.13 platform/loop/loop.py "$FIXTURE" "$RUN" \
  --max-attempts 0 --quality-network "$QUALITY_NETWORK"
```

수정 시험은 자격 없는 CI executor와 SDK 자격이 있는 별도 agent 영역을 연결한 뒤 진행한다. 현재 `loop.py --provider codex|claude --repair-scope source`는 같은 호스트에서 subprocess를 부르므로 자격 없는 VM에 토큰을 복사해 편의상 실행하지 않는다. 이번 실험에서는 별도 임시 harness로 AWS control SDK→자격 없는 GCP gate를 연결해 source repair를 검증했다. 제품의 durable 원격 orchestration CLI/API는 아직 없으므로 위 로컬 명령을 원격 제품 기능으로 설명하지 않는다. full gate가 통과한 경우에만 현재 export CLI를 사용한다.

```bash
python3.13 platform/gate/bundle.py export "$WORK" "$VERDICT" "$BUNDLE"
```

publish는 신뢰된 release 환경에서만 현재 `bundle.py publish BUNDLE REGISTRY_PREFIX TAG --output NEW_IMAGES_JSON`을 사용한다. registry와 자격 확인 없이 실행하지 않는다. 이번 검증은 자격 없는 테스트용 loopback registry에서 실제 export/load/push와 HTTP manifest digest 일치를 확인했다. `/opt/railshot/evidence/bundle-r6/receipt.json`은 같은 이미지 등록, spec hash 변조 및 부분 gate verdict 거부를 기록한다. GHCR production publish와 GitOps/Argo 배포는 별도 미검증 범위다.

## 5. 이슈·실패 기록

상태는 `OPEN`, `IN_PROGRESS`, `FIXED_LOCAL`, `VERIFIED_LIVE`, `BLOCKED`로 구분한다. 아래는 초기 관측이며 이후 실행자는 실패와 재검증 receipt를 같은 행에 연결한다. 실패가 발생하지 않은 옵션에 가짜 실패 로그를 만들지 않는다.

| ID | 관측/근거 | 다음 수정·검증 | 상태·receipt |
|---|---|---|---|
| CI-01 | 기존 호스트 Docker daemon·검증된 quality network 부재 | 별도 GCP sandbox bootstrap·registry 허용/metadata·host/private 차단 시험 | VERIFIED_LIVE — 환경 경계만 통과; 앱 gate와 구분 |
| CI-02 | 기존에는 pnpm/Yarn·requirements-only를 거부 | pnpm/Yarn native manager와 requirements-only full gate 확인 | VERIFIED_LIVE — manager r6 및 `/tmp/railshot-autoprep-evidence-r7/evidence/autoprep-good-r7/summary.json`; requirements-only 앱/테스트 원문 불변 확인 |
| CI-03 | checker/lock/tests 부재의 자동 준비 범위 부족 | 외부 JS/Python checker 준비 추가, 새 lock·generated test·freeze 검증 남음 | IN_PROGRESS; CI-P2 검증 필요 |
| CI-04 | nested manifest 독립 실행과 metadata 오탐 | build root grouping·선택·제외 근거와 planner 재사용 | FIXED_LOCAL; 집중 테스트 9개 PASS, 실제 multi-module 실행 미검증 |
| CI-05 | Node unit reporter는 Jest/Vitest만 허용했음 | native Node JUnit과 외부 ESLint 자동 준비, no-checker full gate; zero/skip/unit negative 검증 | VERIFIED_LIVE — `autoprep-good-r7/node-test-js`, `/tmp/railshot-negative-evidence-r8/evidence/node-negative-r8/negative-verification.json`. native exit0의 zero/skip도 BLOCKED |
| CI-06 | Codex gpt-6.1-sol 실제 unit source repair 1회·38.423초, src/app.js만 수정·보호 파일 불변 | CI로 자격 복사 없이 AWS control SDK와 GCP gate 분리, 전체 6 gate 재검증 | VERIFIED_LIVE — `/opt/railshot/evidence/codex61-repaired-r6/repair-receipt.json`, `run/verdict.json`. Claude 로그인/유료 호출 제외 |
| CI-07 | 실제 검증 이미지의 bundle export/load/push·HTTP manifest digest, spec hash 변조/부분 verdict 거부 | 테스트용 loopback registry에서 재빌드 없는 image 동일성 검사 | VERIFIED_LIVE(테스트 registry) — `/opt/railshot/evidence/bundle-r6/receipt.json`. GHCR production publish·GitOps/Argo 배포는 미검증 |
| CI-08 | 외부 secret·private registry는 자동 대체 불가 | 별도 secret 접수·최소 주입·마스킹 또는 필요한 입력만 요청 | BLOCKED; 현재 MISSING_SECRET 유지 |
| CI-09 | 새 grouping의 macOS `/var`·`/private/var` 불일치와 테스트 Python의 PyYAML 부재 | discover/fixture 경로 정규화, 기존 Python 3.13+PyYAML/jsonschema venv 사용 | FIXED_LOCAL; prepare 9개 및 gate 전체 61개 PASS. cloud 실행 실패로 분류하지 않음 |
| CI-10 | r1/r2의 `/tmp` tmpfs noexec로 uv permission denied, 요청 npm 10.9.2 대신 번들 10.9.9 사용 | exec·실제 tool version assertion 추가. r3에서 정확한 npm10.9.2·uv0.12.18 기동 확인. r2는 수정 전 snapshot | VERIFIED_LIVE — 도구 기동만. 아래 별도 generation 오류가 발견됨 |
| CI-11 | r3 npm 3종은 Arborist peer resolver `null.edgesOut`; FastAPI는 `uv 0.12.18 (platform)` 전체 문자열과 기대값 불일치 | scratch의 npm10.9.2/10.9.9 둘 다 재현, npm12.2.0은 같은 JS lock 생성 exit0. 3 fixture의 명시 pin·Dockerfile을12.2.0으로 변경, uv 판본 필드만 비교 | VERIFIED_LIVE — generation만. `/opt/railshot/debug/npm-*-comparison.log`, 이전 source backup 보존. `/opt/railshot/fixtures-r4/*-generation.json` 4종 GENERATED·hash 일치·build/cache 잔존 0; full-good 결과는 위 옵션 표와 CI-13에 분리 기록 |
| CI-12 | CI host Python 3.12.3에는 `PurePath.full_match`가 없어 source patch L0 평가에서 실패할 수 있음 | stdlib `fnmatchcase`와 segment `**` matcher로 교체, root/재귀/deny 집중 검사 추가. 최신 runner 포함 snapshot에서 source repair/negative 실행 | VERIFIED_LIVE — Codex가 수정한 src/app.js patch에 공통 writer와 L0를 적용한 뒤 full gate PASS; CI-06 receipt 참조 |
| CI-13 | FastAPI r4 Python3.12.9 base에서 libgnutls30/libssl3 fixed CRITICAL4 검출 | 공식 manifest의 Python3.12.14-slim-bookworm으로 fixture pin 일치, 새 lock 생성과 full gate 재검증. 취약점 제외 없음 | VERIFIED_LIVE — `/opt/railshot/e2e-full-python-r8/fastapi--good/run/verdict.json` 전체 PASS. 기존 `/opt/railshot/e2e-full-js-r4/fastapi--good/run/verdict.json` 실패 보존. [공식 Python image manifest](https://github.com/docker-library/official-images/blob/master/library/python) |
| CI-14 | JS/TS missing-env를 HTTP handler에 주입하면 Express가 원인 문자열을 숨겨 unit HTTP500만 관측되어 MISMATCH | synthetic env fixture를 module-startup 실패로 수정. 기존 MISMATCH 보존하고 API_KEY required 진단+BLOCKED를 새 snapshot에서 검증 | VERIFIED_LIVE — r7 JS/TS/Next startup-env 3종 모두 API_KEY required·unit204·BLOCKED MATCH; `/opt/railshot/e2e-env-js-r7`. r5 MISMATCH 보존, 원인 없는 일반 HTTP500의 secret 누락 자동 판별은 보증하지 않음 |
| CI-15 | FastAPI negative lint/type/unit은 실제 202/203/204인데 dependency BLOCKED로 오분류 | `quality_failure`의 bare `429`가 정상 provenance SHA256 내부 `...a047429bef...`와 매칭됨. HTTP/rate-limit 맥락으로 좁히고 hash 회귀검사·실제 negative 재검증 | VERIFIED_LIVE — HTTP/status/E429 맥락+구조화 metadata 제외 후 r8 lint/type/unit 모두 정확한 stage FAIL·수정 적격 MATCH. `/opt/railshot/e2e-negative-python-r8`; 이전 MISMATCH 보존 |
| CI-16 | JVM 준비·검사: Maven only-script wrapper의 unzip 부재/checksum 경로, inherited MAVEN_CONFIG, Gradle warm-cache verification metadata 누락, Tomcat10.1.55 CRITICAL3, 짧은 Gradle env 진단 | native bin/JAR wrapper·MAVEN_CONFIG 초기화, fresh-cache 생성/strict 재검증, Tomcat10.1.60, trusted TestLogging FULL | VERIFIED_LIVE — Maven r6/Gradle r7 full PASS, negative14/14 원인 일치. `java-review-r7/matrix.json`; r3/r4/r6 실패 보존 |
| CI-17 | manager fixture: Yarn skip-builds 옵션 오류/Vite peer 부재, pnpm 설치 cache의 tar7.4.4 CRITICAL | 실제 skip-build·명시 Vite8.3.1, runtime stage에 production dependency만 복사 | VERIFIED_LIVE — pnpm10.17.1/Yarn4.10.3 r6 full PASS, unit1 각각; r8 lint/unit/missing-lock 6건 MATCH. `/tmp/railshot-negative-evidence-r8/evidence/managers-negative-r8/negative-verification.json`. 이전 실패 보존, scan 정책 완화 없음 |
| CI-18 | L1 spec 없는 업로드의 실제 packaging adapter 경로 | gpt-6.1-sol 1회·67.233초, packaging 3개 파일 생성·원본 앱/테스트/lock 불변 후 full gate | VERIFIED_LIVE — `/opt/railshot/evidence/codex61-adapter-r1/adapter-receipt.json` 및 baseline/run verdict. 자동 API dispatch는 미구현 |
| CI-19 | Codex 기본 read-only가 허용 workspace 밖도 읽을 수 있음; 최초 profile은 TOML 키 파싱·SDK 실행 파일 가림 때문에 기동 실패 | 공식 permission profile에 exact read roots·auth deny·network false, 최소 실행 파일 허용. native r4 11사례 MATCH 및 실제 gpt-6.1-sol SDK r4 허용 nonce 읽기/외부 파일 차단 | VERIFIED_LIVE — 로컬 `codex-read-boundary-r4/evidence/result.json`, `codex61-read-profile-r4-result.json`. r1/r2 실패 보존; SDK r4 18.571초. shell 환경 상속을 꺼 bare cat은 찾지 못했고 `/usr/bin/cat`으로 실제 허용·차단을 확인. MCP/browser 검증은 제외 |

완료 알림은 **명시한 지원 옵션의 P1–P4 증거와 잔여 제외 옵션**을 함께 확인한 뒤 보낸다. Claude 실호출·아직 미지원 workspace를 통과한 옵션처럼 포함하지 않는다. 모든 언어·모든 앱을 검증했다는 식으로 범위를 넓히지 않는다. 검증 중인 실패·다음 수정은 이 ledger에 이어 기록한다.

## 6. Claude 오프라인 계약 검증과 무료 로컬 대안

2026-10-01 검증 범위는 `claude-agent-sdk==0.2.158`의 실제
`ClaudeAgentOptions`·`HookMatcher`·`ResultMessage`를 사용한 오프라인 계약
테스트다. `query`만 모의 async generator로 바꾸며 모델/Claude CLI 실행,
로그인, provider 네트워크 요청은 하지 않았다. `test_runner.py`의 기존 두
검사를 보존하고 Claude 두 검사를 추가했다. 이 초기 snapshot의 runner
오프라인 검사는 **5개 PASS**였으며, 후속 결과는 아래 §7에 구분한다.

읽기 도구는 Read/Glob/Grep만 열고 `setting_sources=[]`,
`strict_mcp_config=True`, 빈 MCP 서버, turn/budget/model 및 JSON schema 설정을
확인했다. PreToolUse callback은 정상 파일 허용, 직접 지정한 `.env`와
중첩 `.env.local`, 외부 절대/상대 경로 및 외부로 향한 symlink를 거부했다.
오류 결과나 `structured_output` 부재도 중단했다. Codex 기존 모의 검사에는
설정의 `gpt-6.1-sol`이 SDK 요청과 `requested_model`에 그대로 들어가는지
검사를 추가했다. 이것은 두 provider의 실제 추론이나 수정 성공 증거가 아니다.

```sh
uv run --offline --no-project --python 3.13 \
  --with pyyaml --with jsonschema --with openai-codex==0.159.3 \
  --with claude-agent-sdk==0.2.158 \
  python -m unittest discover -s platform/runner -p 'test_runner.py' -v
```

초기 guard 테스트는 **직접 지정된 경로**의 경계다. 상대 Glob pattern이나
디렉터리 전체 Grep의 모든 하위 파일 제외를 증명하지 않는다. 현재 intake가
`.env*` 업로드를 거부하는 경계는 별도이며, Claude live 재개 전에는 broad
search의 secret 제외 동작을 추가 검증해야 한다.

API 과금 없이 실행할 후보는 Claude Code를 **로컬 모델**에 연결하는
Ollama 호환 경로다. 공식 문서는 로컬 주소를 Anthropic base URL로 지정하는
방식과 64k 이상 context 설정을 안내한다. 이는 Anthropic의 Claude 모델을
무료로 제공한다는 의미가 아니며 로컬 모델·메모리·컴퓨트가 필요하다.
[Ollama Claude Code 통합](https://docs.ollama.com/integrations/claude-code)

호환 서버는 Anthropic Messages API의 일부만 지원한다. 도구 선택 강제,
hosted web search 등 기능과 현재 SDK의 structured output 계약이 같은
수준으로 동작한다고 가정하지 않는다. 도입 시에는 모델별 JSON schema,
읽기 제한, 오류/중단 처리를 따로 확인해야 한다. 현재 Ollama/모델 설치·다운로드,
local provider adapter 추가, 실제 local inference는 **NOT_RUN**이다.
[Anthropic 호환 범위](https://docs.ollama.com/api/anthropic-compatibility)

## 7. 관측성·재개·CD 경계 후속 검증 — 2026-10-01

공통 [관측성 규약](../contract/observability.md)을 state/loop, SDK runner,
gate, CD verifier, Terraform 관리자 CLI에 적용한 최신 소스의 **로컬 회귀검사
190개가 통과**했다. 구성은 observability 5, loop 22, runner 16, gate 93,
CD 7, render 10, infra 29, network policy 8개다. 실제 프로세스·SQLite 검사를
포함하지만 provider·Docker·kubectl·Terraform 경계는 모의/정적 검사가 섞여 있다.
이번 변경에 따른 모델 호출·클라우드 변경은 0회다.

| 발견한 결함 | 수정과 검증 | 상태 |
|---|---|---|
| 원인·부작용·복구 구분 없는 state 오류 | 공통 OperationError와 schema v1 event, cause 보존/비밀 제외. `<stdin>` 등 Python 가상 위치의 생산자·수신기 불일치도 같은 정규화로 해결 | FIXED_LOCAL |
| 재시작 시 완료 단계 중복 실행·증거 변조 위험 | SQLite transaction + 단일 writer, source/config/harness/artifact binding, 실제 자식 프로세스 종료·중복 writer·기록 실패 검사 | FIXED_LOCAL |
| gate가 UNKNOWN을 반환해도 디스크의 이전 PASS를 읽을 수 있음 | subprocess rc/stdout/disk verdict 비교, 원래 오류 보존; stale PASS가 release로 승격되지 않는 회귀 검사 | FIXED_LOCAL |
| apply 사전 입력 오류를 부작용 불확정으로 오분류 | subprocess 0회일 때 BLOCKED/none; 실제 apply 이후 receipt 실패만 UNKNOWN으로 분리 | FIXED_LOCAL |
| Q 네트워크와 build/runtime 네트워크가 다름 | 공통 실행 profile·root native receipt/SHA 확인, 전용 BuildKit·internal runtime 네트워크. Linux Docker 실제 차단 재검증 필요 | FIXED_LOCAL, native NOT_RUN |
| CD 공개 API 의존·재시도 시 stale artifact 위험 | 보호된 CD worker의 내부 get-only 관측, commit→observe lock, producer artifact ID와 run/attempt별 새 출력 경로 | FIXED_LOCAL, workflow/Argo live NOT_RUN |

실제 `loop.py --layers L0,L1 --max-attempts 0`와 같은 입력의 `--resume`도
실행했다. 두 번 모두 `INCOMPLETE`, exit 1, `release_eligible=false`, 모델 호출
0회다. run ID·checkpoint·최종 evidence가 동일하고 `run.resumed` 사건만 추가됐다.
부분 성공을 배포 성공으로 승격하지 않았다는 검사이며 full gate E2E가 아니다.

최초 묶음 검사에서는 SDK가 없는 Python 환경 때문에 runner 5개가 import error를
냈다. 초기 FAIL 기록을 보존한 뒤 §6의 고정 SDK 환경에서 16개를 재검사했다.
최종 190개 검사는 수정 후 소스 hash·명령·종료 코드·로그 hash와 함께 관리자
전용 감사 디렉터리 `observability-hardening/final/validation.json`에 남겼다.

runner 최신 오프라인 검사는 Claude 디렉터리 검색 전 secret tree 검사,
session/turn 식별자 보존, 기록 실패 전후 중단도 포함한다. provider의 실제 broad
search·native conversation resume를 검증한 것은 아니다. native resume는 명시적으로
거부하며, local checkpoint resume와 구분한다.

CI-01–19의 VERIFIED_LIVE는 해당 당시 snapshot의 결과다. 새 Q/L2/L3 네트워크,
전체 Cilium baseline+KEDA, protected CD worker와 실제 Argo 배포, live Pod imageID와
revision-bound HTTP는 현재 별도 NOT_RUN이다. 제품 Allow/API/SSE·원격 dispatch,
중앙 telemetry collector·분산 fencing도 아직 구현하지 않았다.
