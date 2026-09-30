# 토폴로지: 요청 경로와 배포 경로 (초안)

> 목적: 전체 흐름을 한 장에서 맞추고, 각 칸을 성숙한 OSS·엔터프라이즈의 원천 개념에 붙여 이해도를 높인다. 베스트 프랙티스 문서가 아니다.
> 경로 사다리와 증상 표는 개인 학습용 네트워크 트러블슈팅 플레이북(Calico 클러스터 기준 초안)을 RAILSHOT(EC2 위 k3s + Cilium + 번들 Traefik)에 맞게 옮긴 것이다.

## 1. 요청 경로 (데이터 플레인): 링크 → 컨테이너

안쪽에서 바깥쪽으로 확인하고, **처음 실패하는 칸이 원인 계층이다**. diagnoser에게 넘기는 증거는 이 사다리를 결정론적으로 돌린 결과다. LLM은 이 결과를 설명만 한다.

| 칸 | Jasmin 구성 | 확인 (증거 수집 명령) | 여기서 처음 실패하면 |
|---|---|---|---|
| 1 | 컨테이너 안 `localhost:PORT` | `kubectl exec POD -- wget -qO- localhost:PORT/HEALTH` | 앱이 안 뜸, 바인딩·포트 틀림 → F4·F7 |
| 2 | 파드 IP (veth, Cilium) | netshoot 임시 파드에서 `curl POD_IP:PORT/HEALTH` | CNI·NetworkPolicy → F5·PLATFORM (`hubble observe --verdict DROPPED`) |
| 3 | 다른 노드의 파드 | MVP는 단일 노드라 생략. 멀티 노드면 오버레이(VXLAN·Geneve)·MTU | underlay·보안 그룹·MTU → PLATFORM |
| 4 | Service ClusterIP | `kubectl get endpointslices -l kubernetes.io/service-name=SVC` | endpoint 없음 = readiness·selector·targetPort → F4 |
| 5 | Service DNS | `nslookup SVC.NS.svc.cluster.local` (CoreDNS) | DNS → PLATFORM |
| 6 | Traefik + HTTPRoute | `kubectl get httproute -n NS -o yaml`의 `status.parents` 조건(Accepted, ResolvedRefs), Traefik 로그 | 라우팅 규칙 → F5·PLATFORM |
| 7 | 노드 진입 (k3s ServiceLB의 hostPort 80/443, 또는 ALB) | `curl -v --resolve HOST:443:EIP https://HOST/HEALTH` | 보안 그룹·LB·TLS → PLATFORM |
| 8 | DNS 이름 (Route 53 와일드카드) | `dig +short HOST` | 레코드·위임 → PLATFORM |

증상에서 계층 고르기 (플레이북 §4.2 축약):

| 증상 | 먼저 의심할 곳 |
|---|---|
| Pending | 스케줄러(자원 부족). 네트워크 문제 아님 |
| ContainerCreating + FailedCreatePodSandBox | CNI |
| ImagePullBackOff | 레지스트리 도달·인증·egress 정책 |
| CrashLoopBackOff | 앱 또는 liveness probe |
| readiness만 실패 | 재시작이 아니라 Service endpoint에서 빠짐(칸 4) |
| 연결 timeout(무응답) vs connection refused | timeout = DROP(정책·SG), refused = 리슨 없음(RST) |
| 큰 응답만 멈춤 | 오버레이 MTU + ICMP frag-needed 차단 |
| 502/504 | 칸 6·7 중 누가 끊었는지. Traefik 접근 로그의 upstream 상태 |

## 2. 배포 경로 (컨트롤 플레인): 호출 → 파드

```
MCP deploy/apply ─▶ jasmin-api ─▶ Actions plan: intake → 에이전트 → 게이트 → 커밋
   ─▶ Actions deploy: plan_hash 재검증 → GHCR digest → terraform apply(OIDC) → gitops 커밋
   ─▶ Argo CD sync(wave -1 DB ─▶ wave 1 migrate Job ─▶ wave 2 앱) ─▶ kube-apiserver ─▶ scheduler ─▶ kubelet
   ─▶ containerd pull ─▶ CNI ADD ─▶ probes ─▶ EndpointSlice ─▶ HTTPRoute ─▶ 스모크(칸 8→1)
```

모든 단계는 **선언 → 컨트롤러 → 실제 상태** 모양이다. Git(선언)을 Argo CD(컨트롤러)가 클러스터에 맞추고, HTTPRoute(선언)를 Traefik이 라우팅 테이블로 바꾼다. 아래 OVN과 같은 구조다.

## 3. 원천 레퍼런스에 붙이기

| Jasmin 칸 | AWS (PoC) | OpenStack | K8s·CNCF | 네트워크·스토리지 OSS |
|---|---|---|---|---|
| 컴퓨트 | EC2 | Nova | (노드) | — |
| 가상 네트워크 | VPC·서브넷·라우팅 | Neutron (ML2/OVN) | — | OVN 논리 스위치·라우터, Geneve 터널 |
| 보안 경계 | 보안 그룹 | Neutron 보안 그룹 → OVN ACL | NetworkPolicy | Calico(Felix), Cilium(eBPF) |
| L4/L7 진입 | EIP, ALB·NLB | Octavia (amphora = HAProxy VM, 또는 OVN provider = L4) | Service LoadBalancer, Gateway API | MetalLB, Cilium LB-IPAM, Traefik |
| DNS | Route 53 | Designate | CoreDNS, external-dns | — |
| 비밀·인증서 | Secrets Manager, ACM | Barbican | Secret, ESO, Traefik ACME(번들) | — |
| 블록 스토리지 | EBS | Cinder | PV·PVC, CSI | Ceph RBD (Rook) |
| 오브젝트 스토리지 | S3 | Swift | — | Ceph RGW, MinIO |
| DB | (RDS 대신) EC2 k3s 위 CloudNativePG, 볼륨 EBS gp3, 백업 S3 | Trove | Operator (CloudNativePG) | Ceph RBD 볼륨, Ceph RGW·MinIO 백업 |
| IaC·오케스트레이션 | Terraform | Heat | Cluster API, Crossplane | — |
| K8s 클러스터 | EC2 + k3s (또는 EKS) | Magnum | k3s, kubeadm | — |

Provider Interface의 작업(컴퓨트, LB, 네트워크, DNS, 비밀, 블록·오브젝트 스토리지)이 OpenStack 서비스와 거의 1:1로 대응한다. 그래서 OpenStack이 온프렘 거울상으로 가장 완전하다(③ 조사: VM·LB·DNS·비밀 저장소가 모두 Terraform 리소스로 있음). Proxmox에는 LB·DNS가 없어서 그 칸을 클러스터 안(MetalLB·CoreDNS)이나 박스 밖에서 채워야 한다.

## 4. 칸마다 떠올릴 원천 개념

- **OVN:** northbound DB에 논리 스위치·라우터·ACL을 선언하면 `ovn-northd`가 논리 흐름으로 바꾸고, 각 호스트의 `ovn-controller`가 OpenFlow로 설치한다. 선언 → 컨트롤러 → 데이터패스 모양이 GitOps와 같다.
- **Neutron ML2/OVN:** Neutron API의 network·port·router·보안 그룹이 OVN northbound 객체로 번역된다. 보안 그룹은 OVN ACL이 된다.
- **Octavia:** LB 하나가 amphora VM(HAProxy) 또는 OVN provider(L4 전용)다. ALB 같은 L7 대응은 amphora 쪽이다.
- **Calico:** 노드마다 Felix가 iptables·eBPF 규칙을 만들고, BGP(BIRD)로 파드 경로를 광고하거나 VXLAN·IPIP 오버레이를 쓴다. Calico VXLAN 클러스터에서 MTU 1450 함정이 자주 나온다.
- **Cilium (Jasmin 선택, D12):** eBPF로 kube-proxy를 대체하고(RAILSHOT은 대체 모드), 정책은 IP가 아니라 identity 기준이다. Hubble로 흐름 단위 verdict를 본다.
- **K8s Service:** EndpointSlice에는 readiness를 통과한 파드만 들어간다. readiness 실패는 재시작이 아니라 트래픽 제외다.
- **Gateway API:** GatewayClass·Gateway(인프라 제공자)와 HTTPRoute(앱 개발자)로 역할이 나뉜다. 렌더러가 HTTPRoute만 만들고 Gateway는 플랫폼이 소유하는 이유다.
- **Ceph:** RADOS 위에 RBD(블록)·RGW(S3 호환 오브젝트)·CephFS가 올라가고, CRUSH가 데이터 배치를 정한다. Rook이 K8s 오퍼레이터다. 온프렘에서 EBS·S3의 거울상이다.
- **k3s:** 기본으로 Traefik, ServiceLB(klipper-lb, hostPort), Flannel, local-path provisioner가 번들되어 있다. RAILSHOT은 Flannel·NetworkPolicy 컨트롤러·kube-proxy를 끄고 Cilium을 쓴다. servicelb의 hostPort는 Cilium eBPF가 처리한다.

## 5. 원천 문서

- OVN 아키텍처: https://www.ovn.org/support/dist-docs/ovn-architecture.7.html
- Neutron OVN: https://docs.openstack.org/neutron/latest/admin/ovn/
- Octavia: https://docs.openstack.org/octavia/latest/
- Designate: https://docs.openstack.org/designate/latest/
- Calico 네트워킹: https://docs.tigera.io/calico/latest/networking/
- Cilium kube-proxy 대체: https://docs.cilium.io/en/stable/network/kubernetes/kubeproxy-free/
- Kubernetes Service: https://kubernetes.io/docs/concepts/services-networking/service/
- Gateway API: https://gateway-api.sigs.k8s.io/
- k3s 네트워킹: https://docs.k3s.io/networking
- Ceph 아키텍처: https://docs.ceph.com/en/latest/architecture/
- Rook: https://rook.io/docs/rook/latest/
