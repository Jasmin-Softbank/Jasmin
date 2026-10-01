# 조사한 팀 컨텍스트

## 근거

- [팀 Notion 홈](https://app.notion.com/p/3ebe90a5f40d806bb6def0fab82b07aa): 협업 규칙, 역할, 일정. 결정 로그가 정본이다.
- [킥오프 미결 안건](https://app.notion.com/p/2f5e90a5f40d82b68a1381d6612aff08): GitFlow, PR/이슈 템플릿, 에이전트 문서가 제안됨. 앱 입력은 GitHub push와 ZIP 업로드 사이에서 제안이 달랐다.
- [백엔드 위치 미결 안건](https://app.notion.com/p/6ece90a5f40d82e8b0fe815531f29b00): 원격 `/mcp`와 `/login`, GitHub App으로 Actions 실행하는 구성이 제안됨.
- `Jasmin`의 `origin/feature/poc-cloud-jihwan`: `AGENTS.md`, `CLAUDE.md`, PR·이슈 템플릿, `platform/mcp/TOOLS.md`가 있음. 이 작업 디렉터리의 지침과 템플릿은 해당 브랜치를 토대로 작성함.
- `railshot-apps/main`: `.github/workflows/railshot-deploy.yml`은 `workflow_dispatch`의 `tenant`, `app`을 받아 `apps/<tenant>/<app>` 경로를 배포함.

## 이 PoC에서 확인한 범위

- 대시보드, CLI, MCP라는 세 진입점이 하나의 배포 계약을 사용한다.
- ZIP/로컬 폴더 업로드와 공개 GitHub 저장소 기본 URL의 첫 배포 및 같은 앱 이름의 재배포를 지원한다.
- 실제 GitHub Actions 실행과 상태 조회를 연결한다.
- 현재 구현된 배포 대상은 AWS 경로이며, 온프레미스는 이 PoC 범위 밖이다.
- 서버 메모리나 별도 DB에 실행 상태를 두지 않고, GitHub Actions 실행 ID를 조회한다.

## 아직 확정하지 않은 항목

- 사용자 선택: 로컬 전용 + 서버 비밀키, 작은 Node 서버 + 정적 UI, 단일 업로드·배포 API와 presigned URL 확장 경계.
- 원격 MCP의 완전한 OAuth 지원과 11개 도구 구현은 이번 진입점 검증과 별도 범위다.
