# RAILSHOT 제로 트러스트 기준

기준 문서는 [NIST SP 800-207](https://csrc.nist.gov/pubs/sp/800/207/final)이다. 일곱 원칙을 RAILSHOT에 맞게 여섯 줄로 줄였다. 설정 위치와 미구현 경계를 함께 기록한다. 로컬 정책 검사와 실제 클러스터 통신 검증은 별개다.

## 원칙

1. **위치를 믿지 않는다.** 클러스터 안도 기본 거부다. 허용은 IP가 아니라 신원(네임스페이스·파드 라벨, IAM 역할) 기준이다.
2. **들어오는 문은 하나다.** 외부 관리 포트(SSH·Kubernetes API·Argo UI)를 열지 않는다. 공개 앱의 Traefik 80/443과 제품 API의 HTTPS는 별도 서비스 진입이다. Git polling과 SSM/runner의 outbound 인증 채널을 구분한다.
3. **나가는 길도 선언한 것만.** 테넌트 이그레스는 DNS와 `jasmin.yaml`에 적은 호스트의 443뿐이다. IMDS·API 서버·SMTP는 명시 거부라서 어떤 허용보다 우선한다.
4. **자격은 최소로, 키는 없다.** 클라우드는 OIDC·인스턴스 역할, 파드는 SA 토큰 미마운트, DB는 역할 셋(소유자·런타임·읽기).
5. **침해를 가정한다.** 앱 하나 = 네임스페이스 하나 = 격리 단위. 뚫린 앱이 닿을 수 있는 범위를 정책으로 먼저 자른다.
6. **변경은 Git으로만, 차단은 라벨 하나로.** 클러스터의 입력은 GitOps 레포뿐이다. 흐름은 Hubble, AWS는 CloudTrail, 에이전트는 `evidence.json`에 남는다.

## 신뢰 경계

| 대상 | 신뢰 | 가진 권한 |
|---|---|---|
| 사용자 코드·spec | 없음 | 없음. 게이트·렌더러를 통과한 결과만 GitOps로 간다 |
| LLM 에이전트 | 없음 (비신뢰 입력을 읽음) | 읽기뿐. 파일은 JSON으로 내고 실행기가 경로 검사 후 쓴다 |
| 사용자 검증 CI | 비신뢰 코드를 실행 | 격리 빌드·테스트, 클라우드/registry/GitOps/cluster 자격 없음 |
| 신뢰된 release | 검증 artifact만 소비 | 동일 이미지 GHCR/OCI push; 해당 package에 허용된 게시 자격만 사용하며 사용자 코드를 실행하지 않음. [연결·검증 상태](contract/registry.md) |
| 전용 CD worker | 보호된 private workflow만 실행 | GitOps 쓰기 + 자기 클러스터의 Argo Application `get`; Secret·Pod exec·클러스터 변경 권한 없음 |
| GitOps 레포 | 입력의 정본 | 클러스터가 당겨 가는 유일한 원천 |
| 노드 | 플랫폼 경계 | 명시적으로 등록한 GitOps read-token만 선택적으로 사용. 제품 이미지 pull은 namespace별 Secret 참조가 목표이며, 노드 전체 GHCR 자격을 tenant 격리로 취급하지 않음. 기존 live 노드 설정의 자동 변경·회전은 하지 않음 |

## 외부 공개 진입과 클러스터 내부 허용

| 출발 → 도착 | 포트 | 비고 |
|---|---|---|
| 인터넷 → 노드(Traefik) | 80 | HTTPS 켜면 443 추가, 80은 리다이렉트만 |
| Traefik → 테넌트 파드 | 라우트된 서비스 포트 | 라벨 `railshot.dev/tenant`가 있는 네임스페이스의 HTTPRoute만 붙는다 |
| 같은 앱 네임스페이스 안 | 전부 | 앱 서비스끼리, 앱 → DB |
| CNPG 오퍼레이터 → DB 파드 | 8000 | 인스턴스 관리 |
| 노드(host identity) → 파드 | 포트 제한 없음 | 현재 baseline 범위다. kubelet probe만 허용한다고 보장하지 않는다 |

없는 것: SSH(22), 쿠버네티스 API(6443) 공개, Argo CD·Grafana UI 공개, Argo CD webhook, Ingress·IngressRoute(Traefik 공급자를 끔), 테넌트 LoadBalancer·NodePort(쿼터 0).

## 아웃바운드 중심 제어면

관리 포트를 공개하지 않는다. 전용 CD worker는 GitHub에 outbound로 연결하고, 등록된 로컬 클러스터만 조회한다. 내부 Kubernetes API 통신은 필요하지만 외부 방화벽을 여는 이유가 아니다.

| 흐름 | 방식 | 없앤 인바운드 |
|---|---|---|
| 노드 구성 | 최초 bootstrap 또는 명시 재구성 때 Git/bundle 적용 | SSH, Ansible push |
| 배포 | Argo CD가 Git을 폴링 | CI → 클러스터 자격, webhook |
| 관리 접속 | SSM Session Manager(에이전트가 밖으로 연결), 포트 포워딩으로 UI | SSH, 배스천, 공개 UI |
| 비밀 | 노드가 SSM에서 당김, DB 비밀번호는 클러스터 안 생성 | 비밀 주입 API |
| 이미지 | containerd가 등록된 GHCR/OCI endpoint에서 당김 | 앱 노드가 레지스트리 push를 수신하지 않음 |
| Argo 관측 | 내부 CD worker가 Application을 읽고 GitHub artifact로 결과 전송 | 공개 Argo API·6443 |
| 외부 도달성 | 별도 hosted runner의 공개 URL probe | 관리 포트 불필요. HTTP 성공만으로 새 revision 성공을 단정하지 않음 |
| 온프렘 | 같은 pull 모델 | 제어용 인바운드 0 |

AWS 앱 SG는 outbound TCP80/443만 명시한다. AWS DNS·IMDS·시간 동기화는 SG 대상이 아니다. GCP·Azure는 현재 provider 기본 egress 허용이 남아 있어 같은 보안 수준으로 표시하지 않는다. GCP `allow_iap_ssh=true`는 제한된 출처라도 TCP22 관리 인바운드이므로, 엄격한 0 profile에서는 false다.

## 테넌트 이그레스

| 대상 | 판정 |
|---|---|
| kube-dns 53 | 허용 (Cilium DNS 프록시 경유, 조회가 모두 기록된다) |
| 같은 앱 네임스페이스 | 허용 |
| `egress:`에 적은 호스트의 443 | DNS에서 학습한 IP로 허용. TLS hostname/HTTP 목적지 검증 프록시는 아님 |
| 169.254.169.254 (IMDS) | 명시 거부 |
| kube-apiserver | 명시 거부 (CNPG 파드만 예외) |
| 인터넷 SMTP 25·465·587 | 명시 거부 |
| 그 외 전부 | 기본 거부 |

## 멀티테넌시

| 층 | 격리 수단 |
|---|---|
| 이름 | 테넌트 ID는 영숫자 20자(하이픈 금지). 네임스페이스 `t-<tenant>-<app>`이 다른 테넌트와 겹치지 않는다 |
| 네임스페이스 | 앱마다 하나. 라벨 `railshot.dev/tenant`·`railshot.dev/app`, PSS `restricted` 강제 |
| 네트워크 | 플랫폼 네임스페이스 밖은 전부 기본 거부(fail-closed) + 앱별 NetworkPolicy |
| API | 테넌트는 쿠버네티스 자격이 없다(MCP만). AppProject가 종류와 `t-*` 목적지를 제한한다 |
| 자원 | ResourceQuota(LoadBalancer·NodePort 0, PVC 2, 저장 5Gi, 파드 30), LimitRange(컨테이너 최대 2 CPU·2Gi) |
| 데이터 | 앱별 CNPG 클러스터와 역할 셋. 비밀은 Git에 없다 |
| 커널 | 공유(soft multi-tenancy). 비신뢰 코드라서 user namespace(P1), gVisor·전용 노드(P2) |

## 설정 위치

| 무엇 | 파일 |
|---|---|
| 클러스터 기본 거부·명시 거부·킬스위치 | `gitops-template/clusters/aws/platform/30-network-baseline.yaml` |
| 앱별 허용·FQDN 이그레스·쿼터 | `platform/render/render.py` → `01-guardrails.yaml`, `30-netpol.yaml` |
| 네임스페이스 라벨·AppProject | `gitops-template/clusters/aws/platform/20-tenants.yaml` |
| 진입 | `infra/ansible/files/traefik-config.yaml.j2` |
| 노드 방화벽·IMDS | `infra/terraform/aws/main.tf` (SG, 홉 제한 1) |
| 노드 부트스트랩 무결성 | `infra/ansible/node.yml` (k3s 바이너리·설치 스크립트 sha256 고정) |

## 운영

```bash
# 킬스위치: 공개 진입과 외부 이그레스를 끊는다 (앱과 DB는 그대로)
kubectl label ns t-<tenant>-<app> railshot.dev/suspended=true
# 막힌 흐름 보기
hubble observe --namespace t-<tenant>-<app> --verdict DROPPED
# 관리 접속: SSH 대신 SSM
aws ssm start-session --target <instance-id>
```

## 알려진 한계와 다음 단계

- 단일 노드에서는 노드가 신뢰 경계다. 노드 역할이 자기 read-token 두 개를 읽으므로 파드가 노드 자격에 닿지 않게 IMDS 홉 제한 1과 명시 거부를 둘 다 둔다.
- 플랫폼 네임스페이스(`argocd`, `cnpg-system`, `external-secrets`, `monitoring`, `kube-system`)의 outbound는 아직 기본 허용이다. KEDA는 tenant API deny에서 제외하고 별도 DNS/API/metrics 통신으로 제한한다. 전체 baseline + KEDA 동작은 새 live 재검증이 필요하다. Argo/registry host pull까지 FQDN 제한 완료로 표시하지 않는다.
- ESO가 AWS 비밀을 읽게 되는 변경(P1)에는 테넌트 경로 접두어로 제한한 스토어와 ExternalSecret 출처를 막는 VAP를 함께 넣는다.
- HTTPS·HSTS는 도메인 구매 뒤(cert-manager). 그때 SG 443을 연다(`https_enabled`).
- 파드 간 암호화는 단일 노드라 두지 않는다. 멀티 노드에서 Cilium WireGuard.
- 이미지 서명 검증은 P2.
- 다른 CNI(온프렘)는 `30-network-baseline.yaml`과 같은 의미의 정책이 필요하다. 앱별 정책은 표준 NetworkPolicy라 그대로 쓴다. FQDN 이그레스만 Cilium 전용이다.

참고: [Kubernetes 멀티테넌시](https://kubernetes.io/docs/concepts/security/multi-tenancy/), [Cilium 거부 정책](https://docs.cilium.io/en/stable/security/policy/language/), [Pod Security Standards](https://kubernetes.io/docs/concepts/security/pod-security-standards/).
