# RAILSHOT (가제): 구성 · 에이전트 분리 · 지침 전달

> Team Jasmin이 만드는 플랫폼의 가제다. 파일·필드 이름(`.jasmin/jasmin.yaml`, `jasmin-api` 등)은 이름이 확정되면 한 번에 바꾼다.
> 상태: 제안 (2026-09-30). 근거는 팀 조사 노트(비공개): ① CI 수정 루프, ② 로컬 폴더→URL, ③ 네트워크·도메인·인터페이스, ④ 엔터프라이즈·스택, ⑤ 입력·변경 흐름. 팀이 정할 항목은 맨 아래에 모았다.

사용자는 폴더와, 필요하면 자연어 요청만 준다. 플랫폼은 세 가지를 한다.
- AI로 레포를 배포 스택에 맞게 고치고, 결정론 게이트로 판정한다.
- 플랫폼 계정의 역할로 클러스터·DNS·인증서를 준비한다.
- **도메인 링크 하나**를 돌려준다.

이후 변경도 자연어로 받는다.

## 사용자와 주고받는 것

| 단계 | 사용자가 주는 것 | 사용자가 받는 것 |
|---|---|---|
| 첫 배포 | 폴더(업로드). 필요하면 "DB가 필요해" 같은 요청 | 링크, AI가 고친 파일의 diff, 적용된 기본값 요약 |
| 변경 | 자연어 요청 | 미리보기(무엇이 바뀌는지, 비용 등급, 재시작·데이터 영향) → 확인 → 같은 링크 |
| 실패 | 없음 | 원인, 근거, 사용자가 할 일, 그대로 보낼 수 있는 변경 요청문 |

- 사용자는 HCL, 매니페스트, 클라우드 자격을 주지 않는다.
- 요청하지 않은 항목은 `contract/stack-contract.md` §4의 기본값으로 채운다. 기본값은 Pod Security Standards restricted 프로필과 일반적인 PaaS 기본값을 따른다.

## 파일

```
platform/
  README.md                  이 문서
  mcp/TOOLS.md               MCP 도구 계약, 상태를 두는 곳, 신·구 스펙, Agent Gateway
  contract/
    stack-contract.md        "우리 스택에 맞는다"의 정의와 기본값 (에이전트·게이트·렌더러가 함께 읽음)
    paths.yaml               쓰기 허용·보호 경로, 패치 한도, 금지 패턴 (L0 게이트)
    catalog.yaml             플랫폼이 제공하는 것 (자연어 변경의 한계)
    failure-classes.md       실패 분류 F1–F9와 다음 행동
  agents/
    SAFETY.md                모든 LLM 에이전트 공통 규칙
    adapter/INSTRUCTION.md   레포 → Dockerfile + jasmin.yaml
    fixer/INSTRUCTION.md     게이트 실패 → 최소 수정
    change/INSTRUCTION.md    자연어 변경 → jasmin.yaml diff + 영향
    diagnoser/INSTRUCTION.md 배포 실패 → 원인 설명
  runner/
    profiles.yaml            역할별 지침·스키마·쓰기 경로, 공급자별 설정 (Agent API 계층)
    run_agent.py             Claude Agent SDK 또는 Codex exec로 역할 실행, 파일 경로 검사 후 기록
  poc/intake.py              업로드 검사, 에이전트 지침 파일 제거, ir.json
  schemas/*.schema.json      jasmin.yaml과 에이전트 출력 스키마 (draft-07)
  scenarios/db-migration.md  DB 마이그레이션 시험 시나리오 M0–M6
  TOPOLOGY.md                요청 경로·배포 경로, OSS 원천 개념 대응
```

모델이 읽는 파일(`agents/`, `contract/`, `schemas/`)은 영어로, 팀이 읽는 문서는 한국어로 썼다.

## 구성 요소와 권한

| 구성 요소 | 종류 | 입력 → 출력 | 모델 키 | 쓰기 | 클라우드 |
|---|---|---|---|---|---|
| intake | 결정론 | upload → 정리된 작업 사본, `ir.json`(인벤토리) | 없음 | 없음 | 없음 |
| adapter | LLM | `ir.json`, 작업 사본 → Dockerfile류, `jasmin.yaml`, 보고서 | 있음 | 패치 아티팩트만 | 없음 |
| render | 결정론 | `jasmin.yaml` → 매니페스트, `infra.tfvars.json`(기본값 적용) | 없음 | 없음 | 없음 |
| gate | 결정론 | 패치와 렌더 결과 → `verdict.json`, `failure.txt` | 없음 | 없음 | plan 역할(읽기) |
| classifier | 결정론 | verdict → F-코드, 다음 행동 | 없음 | 없음 | 없음 |
| fixer | LLM | `failure.txt`, `lessons.md` → 패치, 보고서 | 있음 | 패치 아티팩트만 | 없음 |
| committer | 결정론 | 통과한 패치 → `jasmin/fix` 커밋, `plan_hash` | 없음 | App 토큰(앱 레포만) | 없음 |
| change | LLM | 자연어, 현재 spec → 새 `jasmin.yaml`, 영향 | 있음 | 패치 아티팩트만 | 없음 |
| deployer | 결정론 | `plan_hash` → 이미지 digest, apply, GitOps 커밋, 스모크, 링크, LKG 롤백 | 없음 | App 토큰(gitops) | apply 역할(권한 경계) |
| diagnoser | LLM | 결정론적으로 모은 증거 → 진단 | 있음 | 없음 | 없음 |

설계 원칙과 근거:
1. **LLM은 제안만 한다.** 판정은 게이트, 쓰기는 커밋 잡, 클라우드 변경은 deployer가 한다. Codex 공식 가이드와 GitHub Agentic Workflows가 같은 구조다(①).
2. **Rule of Two.** 한 에이전트는 다음 셋 중 둘까지만 가진다: 비신뢰 입력, 비밀·민감 시스템, 상태 변경·외부 통신. 우리 LLM 잡은 비신뢰 입력만 가진다(모델 키 제외)(①).
3. **LLM이 쓰는 것을 작게 만든다.** Dockerfile류와 `jasmin.yaml`뿐이고, 매니페스트와 tfvars는 렌더러가 기본값으로 만든다. 그래서 HCL을 받지 않아도 베스트 프랙티스로 배포되고, 자연어 변경은 `jasmin.yaml` 한 파일의 diff로 모인다.
4. **판정 권위는 게이트에 있다.** 에이전트 보고서의 "성공" 주장은 보지 않는다. DeployBench에서 실패의 56%가 에이전트의 자기 완료 선언이었다(①).
5. **모든 시도를 `evidence.json`에 남긴다.** 시도 수, 분류, 패치 해시, 모델, 프롬프트 해시, `give_up` 사유를 기록한다.

## 흐름

**첫 배포:** upload → intake → adapter → render → gate → (실패 시 classifier → fixer 반복) → committer → deploy
- intake 다음의 규칙 기반 어댑터(우리 코드)로 해결되면 LLM을 0회 부른다.
- 반복 한도: 최대 3회, 같은 실패 서명이 2번 나오면 중단, `give_up`이면 중단. F7(앱 코드 결함)과 F8(일시 장애)에는 LLM을 부르지 않는다.
- 시도당 한도: `--max-turns 30`, 잡 `timeout-minutes: 10`, 비용 상한. 전체 30분 이내.

**변경:** `change` 에이전트 → render → `terraform plan`과 매니페스트 diff → 위험 등급(low·restart·cost·destructive) → 미리보기와 확인 → `apply`(저장된 plan만, plan 이후 spec이 바뀌었으면 거부) → deployer. 자세한 규칙은 `mcp/TOOLS.md`

**실패:** deployer가 LKG로 롤백 → diagnoser가 원인 설명 → MCP `status`·`explain`으로 반환

## 지침 전달: Agent API 계층 (`runner/`)

LLM은 Agent API로 부른다. 공급자는 두 가지이고, 지침과 설정은 `runner/profiles.yaml` 한 곳에 둔다.

| 공급자 | 호출 방식 | 지침 | 권한 |
|---|---|---|---|
| claude | Claude Agent SDK(Python) `query()` | 기본 시스템 프롬프트 뒤에 SAFETY + INSTRUCTION 덧붙임 | 도구는 Read·Glob·Grep만. `setting_sources=[]`로 디스크의 CLAUDE.md·설정·hook을 읽지 않음. PreToolUse hook으로 작업 사본·계약 밖 읽기와 `.env`·키 파일 읽기를 거부 |
| codex | `codex exec` | `-c developer_instructions=…` | `--sandbox read-only`, `approval_policy=never`, `--ignore-user-config`, `--ignore-rules` |

- **두 공급자 모두 파일을 직접 쓰지 못한다.** 바꿀 파일은 JSON 출력의 `files`에 전체 내용으로 담는다. `run_agent.py`가 `contract/paths.yaml`로 경로를 검사한 뒤 쓰고, L0 게이트가 다시 검사한다.
- **출력 검증:** 어느 공급자든 같은 스키마로 검증한다. Codex는 모든 필드를 필수로 요구하는 엄격 모드라, 실행기가 선택 필드를 nullable로 바꾼 변형 스키마를 넘기고 받은 뒤 null을 지운다.
- **증거 기록:** 공급자, 모델, 지침 해시, 쓴 파일과 거부된 파일, 턴·비용·시간을 `RUN/<role>.json`에 남긴다.
- **intake가 먼저 뺀다:** 작업 사본에서 CLAUDE.md, AGENTS.md, `.claude/`, `.cursor*`, `.github/copilot-*`를 제거한다. Codex는 AGENTS.md를 읽으므로 이 단계가 필수다.

```bash
python3 -m pip install pyyaml jsonschema claude-agent-sdk   # codex만 쓰면 claude-agent-sdk 불필요
python3 platform/runner/run_agent.py adapter --provider claude|codex \
  --workspace "$WORK" --run "$RUN" --task "$RUN/task.md"
python3 platform/runner/run_agent.py --self-test   # 경로 검사·스키마 변환 확인
```

`task.md`는 파이프라인이 매번 만든다. 예: "Attempt 2/3. Read $RUN/failure.txt and $RUN/lessons.md." 사용자 요청문은 `request.txt` 파일로만 넘긴다.

자격 (사람이 등록):
- **사용자 코드를 처리하는 CI:** Anthropic API 키(가능하면 WIF), Codex는 `CODEX_API_KEY`.
  - 개인 구독(`claude setup-token`, ChatGPT 로그인)은 소비자 약관이 적용되므로 여기에 쓰지 않는다. 학습 동의 시 5년 보존이다.
- **팀원 로컬 개발:** `claude auth login` / `codex login`
- **Actions 시크릿:** 에이전트 잡에는 모델 키 하나만 두고, `id-token`과 쓰기 권한은 주지 않는다.

## SoftBank AGENTIC STAR와의 관계

AGENTIC STAR의 공개된 IaC 사례를 배포 전 구간으로 넓히고, 사람 손에 남아 있던 부분을 결정론 장치로 옮긴 "다음 단계"로 설명한다. 공개 사례는 사내 정보시스템 부문의 AWS·Azure 구축이고, 10인일이 0.5인일로 줄었다(2026-03-16 블로그). 비판이 아니라 보완·확장이라는 톤을 유지한다.

| AGENTIC STAR 공개 내용 ✅ | 배포 자동화에서 흔히 남는 과제 | RAILSHOT의 장치 |
|---|---|---|
| Terraform 생성 → 실행 → 오류 원인 특정 → 수정 → 재실행을 자율 반복 | 반복 상한·중단 조건이 없으면 같은 오류가 반복되고 시간·비용이 늘어남 | 최대 3회, 같은 실패 서명 2회면 중단, `give_up` 채널, 시도별 예산, 실패 분류로 F7·F8은 LLM 제외 |
| 사람이 앞단에서 설계 정보(PPT 구성도·설정값)와 목표를 줌 | 구성도와 설정값을 사람이 정리해야 함 | 폴더만 받고 스택 감지 + 베스트 프랙티스 기본값. 부족한 것은 자연어로 보충 |
| 사람이 뒷단에서 품질 검증. "AI가 품질 보증을 완전히 대체하는 단계는 아님" | 검증이 사람 병목으로 남음 | 4층 결정론 게이트(빌드·기동·동작·규정). 판정 권위는 게이트 |
| plan 승인·롤백·정책·재시도 상한은 공개 글에 없음 | 반쯤 적용된 인프라, 되돌리기 어려움 | plan/apply 분리, `plan_hash`, 에이전트는 tfvars도 직접 쓰지 않음(렌더러), LKG 롤백 |
| 비밀번호 입력·최종 승인 단계에서 사람에게 확인 | 자격 증명을 다루는 부담 | 자격 증명 자체가 없음. 플랫폼 계정 OIDC 역할만 쓰고 사용자는 링크만 받음 |
| 장기 메모리로 설계 배경 유지 | 무엇을 왜 바꿨는지 추적 | `jasmin.yaml`(Git), 커밋된 AI diff, `evidence.json` |
| 가드레일·감사 로그·MCP 연결 관리·승인 워크플로 | 에이전트 권한 과다 | Rule of Two 분리, 쓰기 경로 allowlist, 도구 인가, 선택적으로 Agent Gateway |
| 채팅마다 독립 가상 환경 | 실행 격리 | 실행마다 일회성 러너, 비밀 0 게이트 잡 |
| AWS·Azure 구축 | 클라우드마다 절차가 다름 | Provider Interface 계약(ensure/get/destroy/plan), 온프렘은 같은 계약의 거울상 |

출처:
- 제품: https://www.softbank.jp/biz/services/ai/agentic-star/
- IaC 사례: https://www.softbank.jp/business/content/blog/202603/agentic-star-iac
- 출시: https://www.softbank.jp/corp/news/press/sbkk/2025/20251211_01/

발표 서사: "공개 사례에서 사람 몫으로 남은 두 지점, 즉 앞단의 설계 정리와 뒷단의 품질 검증을 기본값과 게이트로 옮겼다." 데모 드릴로 이를 보여 준다.
1. 테스트 삭제 패치 → L0에서 차단
2. `HEALTHCHECK exit 0` → 플랫폼 프로브가 잡음
3. 로그 속 인젝션 문구 → 에이전트에 쓰기·비밀이 없어서 무해
4. 같은 오류 2회 → 중단
5. 앱 코드 결함 → `give_up` 보고
6. "메모리 늘려줘" → 미리보기 → 확인 → 같은 링크

## 팀이 정할 것

| # | 질문 | 권고 | 근거 |
|---|---|---|---|
| D1 | 앱 소스·의존성 매니페스트 수정 허용 | **재검토:** DB 연결·설정 계층 같은 좁은 범위의 코드 수정을 허용하고, diff를 사용자에게 보여 주며 앱 테스트 + 마이그레이션 게이트를 통과해야 반영 | 킥오프 원본이 "코드를 자동으로 변경하면서", "SQLite → 적절히 설정된 RDS"를 예로 듦(`scenarios/db-migration.md`). 처음 권고였던 "배포 산출물만"은 사람 리뷰가 없다는 점과 gh-aw의 매니페스트 보호를 근거로 했음(①) |
| D2 | 반복 상한 N | 3 | 공개된 상한이 3으로 수렴. 되먹임이 늘면 부정 수정도 늘어남(①) |
| D3 | 에이전트 Bash 허용 | 금지 | 문자열 필터형 allowlist 우회 GuardFall 10/11(①) |
| D4 | 루프 구현 | 에이전트 잡과 게이트 잡을 다른 잡으로(정적 펼침) | 같은 러너를 공유하지 않게 |
| D5 | IaC 산출물 | 렌더러가 만드는 tfvars. 에이전트는 spec만 | 자유 HCL은 plan 중 코드 실행 위험 |
| D6 | 워크로드 명세 형식 | `jasmin.yaml` v0 (Score 매핑은 QA-5에서) | 스키마가 작을수록 자연어 변경이 안정적 |
| D7 | 소스 입력 | 업로드 + 앱별 private 레포 | 보관 초안 D5(zip 업로드 기각)를 번복. Jasmin 레포는 public이라 사용자 코드 CI는 private에서 돌림(②) |
| D8 | 도메인 | HTTPS 시연 전 도메인 구매, sslip.io는 HTTP 스모크까지만 | 와일드카드 인증서 불가, 발급 한도를 공유(③) |
| D9 | 변경 확인 주체 | low·restart·cost는 요청자 1회 확인. 파괴적 변경과 비용 상한 초과만 서버 elicitation(또는 2인 승인) | 모든 변경에 "승인자 ≠ 요청자"(QA-7)를 걸면 1인 사용 흐름이 막힘(⑤) |
| D10 | `plan` 도구 | `change`에 흡수. 실행은 `deploy`(첫 배포)와 `apply`뿐 | 자연어 에이전트 제품은 모두 plan 카드 → 승인 구조(⑤) |
| D11 | spec에서 빠진 리소스 | 분리 후 7일 보존. 삭제는 별도 파괴적 변경 | Render 방식. DB·볼륨 데이터는 어느 플랫폼도 롤백하지 않음(⑤) |
| D12 | CNI | k3s 번들 Flannel + 내장 NetworkPolicy 컨트롤러로 MVP, Cilium은 P1 | 렌더러의 NetworkPolicy는 표준 API라 번들로도 강제된다. 의존성 원칙(PRD §7) |

참고로, 기본 헬스체크를 HTTP readiness로 두는 것 자체가 차별점이 될 수 있다. 조사한 PaaS 기본값은 대부분 TCP 포트 확인이거나 헬스체크가 없다(Fly는 없음, Coolify는 꺼짐)(⑤).
