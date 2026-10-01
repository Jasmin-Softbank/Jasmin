# Railshot

Railshot은 온프레미스(직접 관리하는 서버 환경)와 클라우드에 웹 애플리케이션을 배포하는 시스템을 목표로 합니다.

이 문서는 팀에서 합의한 저장소 구조와 각 영역의 책임을 안내합니다. AI agent(작업을 수행하는 AI)의 상세 작업 규칙은 [AGENTS.md](AGENTS.md)에 명시되어 있습니다.

## 합의된 디렉터리 구조

아래 구조는 팀의 합의 사항입니다. 현재 브랜치에 모든 디렉터리가 구현되어 있다는 의미는 아니며, 구조에 맞추기 위한 파일 이동이나 디렉터리 생성은 이번 문서 작업에 포함하지 않습니다. `railshot/`은 로컬 폴더 이름과 관계없이 저장소 루트를 의미합니다.

```text
railshot/
├── apps/
│   ├── dashboard/
│   ├── api/
│   └── agent/
├── ci/
│   ├── workflows/
│   ├── policies/
│   └── scripts/
├── deployment/
│   ├── bootstrap/
│   ├── cilium/
│   ├── cloudflared/
│   ├── sealed-secrets/
│   ├── cnpg/
│   ├── manifests/
│   └── scripts/
├── infrastructure/
│   ├── providers/
│   │   ├── aws/
│   │   ├── openstack/
│   │   └── proxmox/
│   ├── terraform/
│   └── ansible/
├── gitops/
│   ├── argo/
│   ├── applications/
│   └── rollouts/
├── observability/
│   ├── metrics/
│   ├── logs/
│   └── dashboards/
├── docs/
│   ├── architecture/
│   ├── api/
│   ├── decisions/
│   └── poc/
├── AGENTS.md
└── README.md
```

## 영역별 역할

| 디렉터리 | 책임 |
| --- | --- |
| `apps/` | 프론트엔드(사용자 화면), API(기능 호출 인터페이스), MCP(AI가 도구를 호출하는 연결 규약), agent(AI 작업 수행 기능)를 담당합니다. |
| `ci/` | 빌드(실행 가능한 결과물 생성), 테스트, 컨테이너 이미지(앱 실행 패키지) 파이프라인을 담당합니다. |
| `deployment/` | 준비된 Linux 노드(서버)에서 Kubernetes(컨테이너 실행·관리 시스템) 환경을 구성하고 앱을 배포합니다. |
| `infrastructure/` | provider(인프라 제공 환경)의 자원 생성과 Terraform(코드 기반 인프라 구성), Ansible(서버 설정 자동화)을 담당합니다. |
| `gitops/` | Argo CD(저장소 설정을 클러스터에 적용하는 도구)와 GitOps(저장소를 기준으로 배포 상태를 관리하는 방식)를 담당합니다. |
| `observability/` | metrics(상태 측정값), logs(실행 기록), dashboards(상태 확인 화면)를 담당합니다. |
| `docs/` | 아키텍처(전체 구성), API 명세, 결정 기록, PoC(최소 동작 검증) 문서를 관리합니다. |

## 구조 변경 원칙

> **Do not redesign or restructure the agreed Railshot repository layout without explicit team approval.**
>
> 명시적인 팀 승인 없이 합의된 Railshot 저장소 구조를 재설계하거나 변경하지 않습니다.

- 기존 디렉터리의 이름 변경, 이동, 병합, 삭제를 임의로 수행하지 않습니다.
- 새로운 최상위 디렉터리를 임의로 추가하지 않습니다.
- 기능 코드는 해당 책임 영역의 기존 디렉터리 아래에 추가합니다. 대상 디렉터리가 없거나 책임이 불명확하면 먼저 위치를 제안합니다.
- 구조 변경이 필요하면 이유와 영향을 먼저 제안하고, 명시적인 팀 승인 후 진행합니다.
- 기존 코드가 합의된 구조와 다른 위치에 있어도 이번 문서 작업에서는 이동하지 않습니다.
- 다른 담당 영역의 코드를 불필요하게 수정하지 않습니다.

상세한 적용 규칙은 [Railshot Agent Guidelines](AGENTS.md)를 확인해 주시기 바랍니다.
