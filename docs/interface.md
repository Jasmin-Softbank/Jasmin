# 세 진입점의 공통 계약

대시보드, CLI, MCP는 동일한 로컬 HTTP API를 사용한다. 인증은 PoC에서 localhost 바인딩과 서버에만 저장한 GitHub 토큰으로 제한한다. 브라우저의 쓰기 요청은 커스텀 헤더와 Origin 검사로 다른 사이트의 호출을 막는다. 원격 공개 서비스의 인증·인가를 대체하지 않는다.

## 배포 시작

`POST /api/deploy` · `multipart/form-data` · `X-Jasmin-Request: deploy`

- 필드: `app`(소문자, 숫자, 하이픈 1~30자)과 **소스 하나**. ZIP은 `archive` 파일, 폴더는 `files` 파일 반복과 같은 순서의 상대 경로 배열 `paths`(JSON 문자열), GitHub는 공개 저장소 기본 URL을 `repository_url`로 전달한다. 서버가 이 세 필드 중 존재하는 것을 보고 입력 경로를 판별한다. 선택적인 `source_type`을 보내면 실제 필드와 일치해야 한다.
- 대시보드·CLI·MCP는 소스 이름으로 `app`을 자동 생성한다. 같은 이름은 기존 앱의 전체 파일 목록을 갱신하고 다시 배포한다. 별도 앱으로 등록할 때만 이름을 바꾼다.
- 서버가 `JASMIN_TENANT`와 GitHub 저장소 설정을 결정한다.
- 서버 내부 흐름: `uploadedSource(request)` → 검증된 파일 목록 `{path, content}[]` → `service.deploy({app, files, source?})` → Git blobs/tree/commit/ref → workflow dispatch. GitHub URL은 기본 브랜치의 SHA를 확인한 뒤 그 커밋의 ZIP을 받아 공통 파일 목록으로 변환한다.
- 응답 `202`: `{ "run_id": 123, "tenant": "demo", "app": "my-app", "changes": { "added": 1, "updated": 0, "deleted": 0, "unchanged": 2 }, "actions_url": "…" }`
- 기존 파일과 SHA를 비교해 새 blob은 변경된 파일에만 만든다. 입력에서 빠진 기존 파일은 제거한다. 내용이 같아도 커밋 없이 Actions를 재실행한다. 업로드/검증 오류는 `400` 또는 `413`.

`uploadedSource`는 현재 단일 요청 업로드 어댑터이며 GitHub 입력은 서버 측 다운로드 어댑터다. 추후 presigned URL로 바꿀 때 S3 객체를 같은 파일 목록으로 변환해 `service.deploy({app, files})`에 전달한다. 여기서 '검증'은 파일 개수·총 크기·상대 경로·중복 경로·일부 비밀키 파일 이름·ZIP 심볼릭 링크 검사다. 앱이 정상 빌드/배포된다는 뜻이나 파일 내용의 비밀값 검사까지 마쳤다는 뜻은 아니다. 대용량 파일에는 스트리밍 처리와 별도 업로드 상태가 필요하다.

## 배포 상태

`GET /api/runs/:runId`

- `run_id`는 GitHub Actions 실행 ID. 서버 메모리/DB에 실행 상태를 저장하지 않는다.
- 응답: `status` (`queued`, `in_progress`, `completed`), `conclusion`, `steps` (`loop`, `release`, `gitops`), `actions_url`, 성공 시 `url`. Actions 전체가 성공으로 표시돼도 `release` 또는 `gitops`가 건너뛰어졌으면 배포 결과는 실패로 표시한다.
- 공개 URL은 워크플로의 `rendered` artifact 안 `render.json`에서 읽는다. artifact가 사라지면 URL은 `null`이다.

## 로컬 MCP PoC

`npm run mcp`는 공식 MCP SDK의 stdio 전송으로 `deploy(source, app?)`와 `status(run_id)` 두 도구를 제공한다. `source`는 공개 GitHub 저장소 URL 또는 로컬 ZIP/폴더의 절대 경로다. MCP가 URL/로컬 경로를 판별하고 CLI와 같은 HTTP API를 호출한다. 대시보드·CLI·MCP는 소스 이름에서 앱 이름을 자동 생성한다. 로컬 파일은 `JASMIN_SOURCE_ROOT` 아래에 있어야 하며, URL만 사용할 때는 이 환경 변수가 필요 없다. GitHub 비밀값은 MCP 프로세스에 주지 않는다.

AI 호스트에서 사용자가 코드를 첨부한 경우, **호스트가 첨부파일을 접근 가능한 로컬 경로로 저장하고 그 경로를 도구의 `source`에 전달해야 한다.** MCP 서버가 대화의 첨부파일을 자동으로 가져올 수는 없다. 첨부 경로가 없으면 호스트는 공개 GitHub URL 또는 저장된 파일 경로를 요청해야 한다. 이 PoC는 소스 수집 경로만 자동 판별하며, Dockerfile/Railpack 같은 빌드 전략과 실제 배포 판단은 기존 Actions 워크플로가 수행한다.

```json
{
  "mcpServers": {
    "jasmin-poc": {
      "command": "node",
      "args": ["/Users/llokr/Desktop/softbank-hackathon/jasmin-entrypoints-poc/src/mcp.js"],
      "env": {
        "JASMIN_SOURCE_ROOT": "/Users/llokr/Desktop/softbank-hackathon",
        "JASMIN_API_URL": "http://127.0.0.1:4173"
      }
    }
  }
}
```

팀의 원격 `/mcp` 설계는 사용자의 로컬 파일을 읽을 수 없어서 `upload_create`/presigned URL과 `deploy(upload_id)`를 전제로 한다. 현재 단일 업로드 API를 선택한 PoC에서는 로컬 stdio를 사용한다. 원격 배포로 전환할 때 업로드 어댑터와 MCP 전송을 함께 바꾼다.

## 현재 워크플로와 맞춰야 하는 지점

- [실제 워크플로](../../railshot-apps/.github/workflows/railshot-deploy.yml)는 `workflow_dispatch`에 `tenant`, `app`만 받는다.
- Actions 성공은 URL 확인까지 통과했을 때로 표시한다. 현재 워크플로의 `no change` 조기 종료는 따로 확인이 필요하다.
- 온프레미스 대상은 현재 워크플로에 없으므로 선택지로 표시하지 않는다.
- 원격 MCP, `upload_id` 기반 업로드, OAuth, 롤백 도구는 팀의 제안 설계이며 이 PoC의 후속 작업이다.
