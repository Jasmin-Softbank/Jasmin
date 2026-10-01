# RAILSHOT 배포 진입점 PoC

웹 대시보드, CLI, MCP에서 앱 배포를 시작하고 GitHub Actions 실행 상태를 확인하는 독립 PoC다. 앱 폴더, ZIP 파일, 공개 GitHub 저장소 URL을 받는다. 작은 Node HTTP 서버가 정적 UI와 API를 함께 제공한다. DB 없이 GitHub Actions의 `run_id`를 상태의 기준으로 사용한다.

## 실행

Node.js 22 이상이 필요하다.

```sh
npm ci
cp .env.example .env
# .env의 GITHUB_TOKEN 값을 설정한다.
npm start
```

브라우저에서 `http://127.0.0.1:4173`을 연다. 소스 입력 영역에 ZIP 파일, 로컬 폴더 또는 공개 GitHub URL 하나를 넣으면 입력 경로와 앱 이름이 자동 결정된다. 서버는 localhost에만 바인딩된다. GitHub 토큰은 브라우저, CLI, MCP에 전달하지 않고 서버에만 둔다. private `railshot-apps` 저장소에 대해 Contents 읽기/쓰기와 Actions 읽기/쓰기 권한이 있는 fine-grained PAT 또는 설치 토큰이 필요하다. `.env`는 Git에서 제외된다.
`.env`를 수정했다면 서버를 재시작한다. `npm start`와 `npm run dev`는 프로젝트의 `.env`를 로드하지만 `node src/server.js`를 직접 실행하면 로드하지 않는다.

서버를 띄운 뒤 CLI를 사용할 수 있다.

```sh
npm run cli -- deploy /path/to/my-app --app my-app
npm run cli -- deploy /path/to/my-app.zip
npm run cli -- deploy https://github.com/owner/repo
npm run cli -- status 123456789
```

환경 변수 `JASMIN_API_URL`로 CLI가 호출할 로컬 서버 URL을 바꿀 수 있다. MCP 실행과 설정은 `docs/interface.md`를 참고한다.

## 요청 흐름

1. 웹은 하나의 소스 입력 영역에서 ZIP·폴더·공개 GitHub URL을 받는다. CLI와 MCP는 로컬 경로 또는 공개 GitHub URL 하나를 받는다. 서버가 요청의 실제 필드로 소스 경로를 판별한다. CLI와 MCP는 폴더를 ZIP으로 만들어 전송한다. GitHub URL은 서버가 인증 없이 공개 여부를 확인하고 기본 브랜치의 커밋 SHA를 고정해 소스를 가져온다.
2. 서버가 입력을 공통 `{path, content}` 파일 목록으로 바꾸고 경로, 용량, 파일 수, 확장자 기반 비밀키 파일을 검사한다. ZIP의 심볼릭 링크도 거부한다.
3. GitHub Git API로 `railshot-apps/apps/<tenant>/<app>` 파일을 등록하거나 갱신한다. 기존 파일과 SHA를 비교해 바뀐 파일만 새 blob으로 올리고, 입력에서 빠진 파일은 앱 트리에서 제거한다. 내용이 같으면 커밋을 만들지 않는다.
4. `railshot-deploy.yml`의 `workflow_dispatch(tenant, app)`을 호출하고 `run_id`를 바로 반환한다.
5. `GET /api/runs/:runId`가 GitHub Actions의 실행과 세 작업(`loop`, `release`, `gitops`)을 조회한다. 성공한 실행의 `rendered` artifact에서 공개 URL을 읽는다.

같은 `tenant/app`으로 다시 요청하면 새 소스의 전체 파일 목록을 기준으로 앱을 갱신하고 Actions를 다시 실행한다. 파일이 완전히 같아도 실패한 배포를 재시도할 수 있다. 소스 변경분만 커밋하지만 Actions의 컨테이너 빌드와 배포 검사는 매번 실행된다. 대상은 기존 워크플로에 구현된 AWS 경로다. 업로드는 100 MB, 압축 해제도 100 MB, 파일 2,000개로 제한한다. 큰 변경에는 GitHub API 업로드 시간이 걸린다. 실제로 어떤 앱 형식을 배포할 수 있는지는 [소스 형식별 판단](docs/source-formats.md)을 참고한다.

`railshot-apps` 저장소의 Actions 변수 `PLATFORM_REF`, `RAILSHOT_DOMAIN`, `GITOPS_REPO`와 비밀값 `ANTHROPIC_API_KEY`, `GITOPS_TOKEN`이 설정되어 있어야 뒤쪽 배포가 성공한다. 이 프로젝트는 해당 값을 만들거나 클라우드 배포 자체를 수행하지 않는다. 실제 GitHub 토큰이 없는 환경에서는 모의 GitHub API 통합 테스트로만 검증할 수 있다.
앱 등록은 설정한 `GITHUB_REF`에 직접 커밋하므로, 해당 브랜치의 보호 규칙이 직접 쓰기를 허용해야 한다. 그렇지 않으면 앱 등록을 PR 방식으로 바꿔야 한다.

보안상 현재 검사는 알려진 비밀키 **파일 이름**과 ZIP 구조를 차단한다. 파일 **내용**의 비밀값 탐지는 아직 없다. 신뢰할 수 없는 코드를 실제 저장소에 올리기 전에는 서버 측 secret scan을 추가해야 한다. 원격 서비스로 공개하려면 사용자 인증과 테넌트 권한 검사가 별도로 필요하다.

GitHub URL 입력은 `https://github.com/owner/repo` 형식의 **공개 저장소 기본 URL**만 지원한다. 비공개 저장소, 브랜치·하위 폴더 선택, Git push 이후 자동 재배포는 지원하지 않는다. GitHub 소스 조회에는 서버 토큰을 보내지 않는다. 다운로드한 파일은 대상 `railshot-apps` 저장소에 복사되며, 응답과 등록 커밋에는 원본 SHA가 남는다. 공개 API의 익명 호출 한도가 적용된다.

## 설계와 근거

- Pen 시안 PNG: [새 배포](design/deploy-create.png), [진행 상태](design/deploy-status.png). 원본 경로는 [design/README.md](design/README.md)에 있다.
- 팀 자료 및 규칙: [docs/context.md](docs/context.md), [AGENTS.md](AGENTS.md)
- HTTP/API 계약: [docs/interface.md](docs/interface.md)
- 소스 형식별 배포 가능성: [docs/source-formats.md](docs/source-formats.md)

## 테스트

```sh
npm test
```
