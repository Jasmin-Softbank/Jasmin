# Jasmin — Agent Guide

SoftBank Hackathon 2026 예선(10/3–4) Team Jasmin. 테마 "One Action, Infinite Clouds." — 로컬 웹앱을 AI로 온프레·클라우드에 원터치 배포하는 시스템. 플랫폼 가제는 RAILSHOT.

## 먼저 읽을 것

- `platform/README.md` — 구성 요소·권한, 에이전트 분리, 지침 전달, 결정 대기 항목
- `platform/PRD.md` — 요구사항, 고정 기술 스택, CD·관측성·에이전트 통신·MCP 도구
- `platform/TOPOLOGY.md` — 요청 경로와 배포 경로
- `platform/ZERO-TRUST.md` — 보안 원칙, 인바운드·아웃바운드 허용 목록, 멀티테넌시
- 결정은 팀 노션의 결정 로그(DEC-n)와 미결 안건(QA-n)이 정본이다. 이와 어긋나는 구현을 하기 전에 먼저 묻기.

## 레포 안의 두 종류 지침

- 이 파일(AGENTS.md)은 **이 레포를 개발하는** 코딩 에이전트용이다.
- `platform/agents/*/INSTRUCTION.md`는 **RAILSHOT이 실행하는** 에이전트(adapter·fixer·change·diagnoser)용이다. 둘을 섞지 않는다.

## 협업 규칙

- GitFlow: `main`(제출용) ← `develop` ← `feature/<이슈번호>-<slug>`. PR로 병합.
- 커밋 메시지(권장): `type: 요약` (feat, fix, docs, chore, refactor, test).
- 공개 레포: 비밀키·토큰·개인정보 커밋 금지. `docs/`의 조사 원자료는 올리지 않는다.
- 평가 대상은 배포 시스템이다. 데모 앱에는 최소한의 시간만.
- README와 코드가 같아야 한다. 수치는 측정값만 쓴다.
