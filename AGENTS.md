# Jasmin entrypoints PoC — Agent Guide

이 문서는 `Jasmin`의 `feature/poc-cloud-jihwan` 브랜치에 있는 `AGENTS.md`의 협업 규칙을 이 독립 작업 디렉터리에 맞게 옮긴 것이다. 원본 `CLAUDE.md`는 `@AGENTS.md`를 참조한다.

## 먼저 확인할 자료

- 팀 [Notion 홈](https://app.notion.com/p/3ebe90a5f40d806bb6def0fab82b07aa): 결정 로그와 미결 안건이 설계의 정본이다.
- `Jasmin` 기능 브랜치의 `platform/README.md`, `platform/PRD.md`, `platform/TOPOLOGY.md`, `platform/mcp/TOOLS.md`.
- 실제 배포 진입 계약은 `../railshot-apps/.github/workflows/railshot-deploy.yml`의 `workflow_dispatch(tenant, app)`이다.

## 협업 규칙

- GitFlow 제안: `main`(제출용) ← `develop` ← `feature/<이슈번호>-<slug>`, PR로 병합.
- 커밋 메시지 권장 형식: `type: 요약` (`feat`, `fix`, `docs`, `chore`, `refactor`, `test`).
- 비밀키, 토큰, 개인정보를 커밋하지 않는다. 조사 원자료는 공개 저장소에 올리지 않는다.
- README와 코드가 일치해야 한다. 성능이나 성공률 수치는 실제 측정값만 쓴다.
- 배포 시스템 PoC에 집중하고 데모 앱 구현은 최소화한다.

## 이 프로젝트의 경계

- 대시보드, CLI, MCP는 같은 배포 API 계약을 사용한다.
- GitHub 자격 증명과 Actions 호출은 서버 코드에만 둔다.
- 실제 클라우드 반영은 `railshot-apps` Actions와 `railshot-gitops`/Argo CD 담당이다.
- 미결 안건과 충돌하는 구현, 특히 앱 수집·인증·배포 대상에 관한 변경은 사용자에게 확인한다.

`platform/agents/*/INSTRUCTION.md`는 RAILSHOT이 실행하는 에이전트의 지침이며, 이 저장소를 개발하는 코딩 에이전트 지침과 혼동하지 않는다.
