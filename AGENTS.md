# 개발 규칙

- 사용자 응답과 진행 보고는 한국어 존댓말을 사용합니다.
- docs/contracts.md의 초기 기능·메서드·데이터 계약을 따릅니다.
- 주 에이전트는 domain, ports, 설정·인증·조립, 의존성, 통합 시험과 Git 커밋을 담당합니다.
- 서브에이전트 A는 api와 tests/unit/api, B는 application과 tests/unit/application, C는 infrastructure와 tests/unit/infrastructure를 담당합니다.
- 담당 경로 밖 변경은 주 에이전트에게 먼저 요청합니다. 공통 규약을 임의로 바꾸지 않습니다.
- 비밀 값은 커밋하거나 로그에 출력하지 않습니다. 실제 OpenStack 자원을 생성·삭제하는 시험은 별도 지정된 시험 범위에서만 실행합니다.
- 모의 시험 성공을 실제 OpenStack 연결 성공으로 보고하지 않습니다.
- 기능별 구현 후 pytest, Ruff, mypy로 검증합니다. 설치된 SDK의 실제 시그니처와 동작을 확인합니다.
