# Jasmin Deployment PoC 실제 검증 — 2026-10-01

새 Linux VM(가상 컴퓨터)에서 최초 설치를 수행하고, 장애·재실행·재배포·네트워크·제거를 포함한 자동 검사 18개를 두 차례 통과했다. Mac 클라이언트의 NodePort(노드 포트로 앱을 공개하는 방식) 접근도 확인했다. 강제 VM 종료 뒤 Linux 서비스는 자동 복구됐지만, Mac에 남은 SSH 연결 때문에 외부 접근이 실패하는 문제를 추가 발견해 복구했다. 인터넷 공개 URL 및 실제 클라우드 검증은 수행하지 않았다.

## 작업 기준과 환경

- 기존 `deployment-poc`의 `install.sh → install-k3s → install-cilium → deploy-sample → verify` 구조를 유지했다. 기존 시연용 `jasmin` VM을 대상으로 장애를 주입하지 않았다.
- 작업 디렉터리는 Git 저장소가 아니므로 비교할 branch(개발 작업 분기)나 commit(저장된 변경 기록)이 없었다. 로컬 파일을 기준으로 검토·수정했다.
- 전용 테스트 VM: Lima `jasmin-poc-test`, Ubuntu 24.04.4, arm64, Linux 6.8.0-134, CPU 2개, RAM 약 4 GiB, 디스크 20 GiB, swap 없음. [환경 기록](test-environment.log)
- K3s(경량 Kubernetes) `v1.34.11+k3s1`, Cilium(컨테이너 네트워크 구성 도구) `1.20.2`, CLI(명령행 도구) `v0.20.1`, nginx(HTTP 웹 서버) `1.28.0-alpine`. 업데이트 검사는 `1.28.1-alpine`으로 교체한 뒤 원래 버전으로 복원했다.
- K3s의 Flannel(기본 컨테이너 네트워크)과 기본 network-policy controller(통신 정책 적용 기능)는 비활성화했다. Cilium의 실제 `kube-proxy-replacement` 값은 `false`로 확인했다.

## 실제 실행 순서와 결과

1. 기존 스크립트·manifest(클러스터에 적용할 설정 파일)를 읽고 로컬 검사를 수행했다.
2. 새 VM에서 K3s 실행 파일과 데이터 경로가 없음을 확인한 뒤 기존 `install.sh`를 실행했다. 최초 설치 및 HTTP 200·본문 확인에 성공했다. [신규 설치 증거](baseline-clean-install.log)
3. 잘못된 이미지로 기존 오류 출력을 확인했다. 실패 단계는 나오지만 원인 상태를 직접 추가 조회해야 했으므로 자동 진단을 보강했다. [수정 전 오류](baseline-failure-diagnostics.log)
4. 자동 검사와 필요한 보완을 적용하고 전용 VM에서 전체 검사 두 차례 수행: 각각 **18 PASS / 예상 밖 FAIL 0 / Clean install SKIP 1**. 검사 시작 시 이미 설치됐기 때문에 신규 설치 항목은 정직하게 SKIP했다. 최초 설치는 2번에서 별도 수행했으며 전체 제거·재설치는 각 검사에서 다시 수행했다. [첫 실행 결과](run-1/results.tsv), [최종 실행 결과](final/results.tsv), [최종 콘솔](final-test-console.log)
5. VM을 강제 종료한 뒤 다시 시작했다. boot ID(부팅마다 바뀌는 식별값)가 변경됐으며 설치 스크립트를 재실행하지 않고 Node·Cilium·앱·HTTP가 정상화됐다. 이 검사는 무중단을 측정한 것이 아니라 재부팅 뒤 복구를 확인한 것이다. [종료 기록](vm-force-stop.log), [시작 기록](vm-restart.log), [복구 기록](vm-recovery.log)
6. Mac에서 포트 30081을 통해 테스트 VM의 NodePort 30080에 접근했다. 초기 실패를 복구한 뒤 HTTP 200·본문 및 동시 요청 100개를 검증했다. 기존 시연 VM의 Mac 포트 30080도 HTTP 200을 확인했다. [테스트 VM HTTP](mac-http-fixed.log), [Mac 부하](mac-load-fixed.json), [기존 시연 VM](original-demo-http.log)

| 검사 | 최종 결과 | 검증 근거 |
| --- | --- | --- |
| 최초 설치 | PASS, 별도 수행 | 새 VM의 설치 전 부재 확인 → 순서대로 설치 → HTTP 정상 |
| K3s / Cilium / workload(실행 앱) | PASS | Node Ready, Cilium status, Deployment 준비 |
| 반복 실행 2회 | PASS | Deployment와 Pod(앱 실행 단위)의 UID(고유 번호) 유지 |
| 강제 Pod 삭제 | PASS | 새 UID 생성 및 HTTP 복구, 최종 검사 6초 |
| rollout restart(앱 순차 교체 재시작) | PASS | 교체 완료, 20초 HTTP 요청 실패 0 |
| 이미지 업데이트 | PASS | 새 이미지 rollout 완료, 25초 HTTP 요청 실패 0 |
| 잘못된 이미지 형식 / 없는 태그 | PASS | 비정상 종료 및 InvalidImageName / 이미지 다운로드 오류 출력 |
| 잘못된 Service(앱 연결 주소) 포트 | PASS | 내부 HTTP 검사 시간 초과 및 Service·endpoint 진단 |
| readiness(요청 처리 준비 확인) 실패 | PASS | HTTP 404 이벤트·rollout 실패·비정상 종료 확인 |
| Cilium 비정상 / 미설치 | PASS | 각각 실패 감지 후 정상 이미지 복원 / 재설치 |
| DNS(이름을 주소로 조회) / Pod→Service | PASS | 실제 Pod 내부 요청과 정상 본문 |
| 최소 NetworkPolicy(통신 허용·차단 규칙) | PASS | 적용 전 두 클라이언트 접근; 적용 후 허용 클라이언트만 HTTP 성공, 차단 클라이언트의 DNS는 정상 |
| 정상 상태 동시 요청 | PASS | 100개, 동시 8개, HTTP 실패 0 |
| K3s 서비스 재시작 | PASS | 재시작 후 전체 확인 복구, 검사 35초; 20초 요청 관찰 실패 0 |
| sample 제거·재배포 | PASS | namespace(리소스 묶음) 삭제 확인 후 다시 배포 |
| 전체 제거·재설치 | PASS | 공식 uninstall, 핵심 실행 파일·데이터·설정 부재 확인, 신규 설치 및 HTTP 정상 |
| VM 강제 종료·시작 | PASS, 별도 수행 | boot ID 변경, 설치 재실행 없이 Linux 앱 자동 복구 |
| Mac→VM HTTP | 초기 FAIL → 복구 PASS | 오래된 SSH 포트 점유를 해소하고 실제 NodePort로 전달 |

의도적으로 주입한 장애는 **예상한 실패를 감지하고 복원했기 때문에 PASS**이다. 실제 잘못된 설정이 정상 배포된다는 뜻이 아니다.

## 부하 관찰 수치

모든 요청은 HTTP 200뿐 아니라 `Jasmin Deployment PoC OK` 본문도 확인했다.

| 경로 / 상황 | 요청 수 | 동시 요청 | 실패 | 관찰 시간 |
| --- | ---: | ---: | ---: | ---: |
| VM NodePort 정상 상태 | 100 | 8 | 0 | 0.106초 |
| 앱 재시작 중 | 1,533 | 4 | 0 | 20.011초 |
| 이미지 업데이트 중 | 1,920 | 4 | 0 | 25.025초 |
| K3s 서비스 재시작 중 | 1,545 | 4 | 0 | 20.022초 |
| Mac→VM, 전달 복구 후 | 100 | 8 | 0 | 0.041초 |

[정상 상태](final/load.json), [앱 재시작](final/restart-traffic.json), [이미지 업데이트](final/update-traffic.json), [K3s 재시작](final/k3s-restart-traffic.json), [Mac](mac-load-fixed.json).

짧은 관찰 창의 요청 실패율이다. 최대 처리량, 장시간 안정성, 임의 환경의 무중단 보장이 아니다. K3s 재시작은 복구 검사 전체가 35초이지만 트래픽 관찰은 20초이므로 전체 35초를 연속 측정했다고 해석하면 안 된다. 강제 Pod 삭제와 VM 전원 종료 구간의 요청 실패율은 측정하지 않았다.

## 발견한 문제와 변경

| 파일 | 변경 / 이유 |
| --- | --- |
| `scripts/common.sh` | 실패 시 Pod·Service·endpoint·최근 이벤트 및 컨테이너 대기 원인 자동 출력. 조회별 5초 제한으로 API 장애 시 진단 대기를 제한하며 환경변수·Secret은 조회하지 않음 |
| `scripts/verify.sh` | Cilium CLI 부재를 명확히 안내. 기존 DNS·Service·HTTP 200·본문 검사 유지 |
| `manifests/deployment.yaml` | 교체 시 정상 Pod 유지(`maxUnavailable: 0`), 추가 Pod 1개, 준비 3초·종료 전 5초 대기·진행 제한 120초 명시 |
| `scripts/test-poc.sh` — 신규 | 18개 실제 검사, 예상 장애 감지 및 복원, 결과·로그 자동 저장, 예상 밖 오류와 복원 실패 출력 |
| `scripts/cleanup.sh` — 신규 | 샘플 제거 / 전용 노드 전체 제거. 관리 설정을 확인한 뒤 Cilium과 공식 K3s uninstall 수행 |
| `scripts/load-http.py` — 신규 | Python 표준 라이브러리로 작은 동시 요청 검사와 실패 집계. 장애 관찰 모드에서도 실패 수 보존 |
| `tests/network-policy.yaml` — 신규 | 허용·차단과 DNS 유지를 검증할 최소 테스트 규칙. 테스트 후 제거 |
| `tests/test_load.py` — 신규 | 정상 본문·잘못된 앱 본문·HTTP 500이 집계와 종료 코드에 반영되는지 실제 로컬 HTTP 서버로 확인 |
| `README.md` | 자동 검사·제거·전용 VM 실행·Lima 오류 복구·확장 입력과 출력·운영 TODO 기록 |
| `reports/` — 신규 | 두 차례 검사 원본 로그, 최초 설치, VM 복구, Mac 실패·복구 및 이 보고서 보존 |

`install.sh`, K3s/Cilium 설치 방식, sample Service 및 기존 로컬 verify 테스트는 구조를 유지했다. Production(실제 서비스 운영) 자동 rollback(이전 정상 버전 복원)은 추가하지 않았다.

추가 발견한 Lima 문제: 일반 종료가 완료되지 않아 전용 VM을 강제 종료했고, 종료 전 SSH 프로세스가 Mac 포트 30081을 계속 점유했다. 새 host agent(호스트 측 VM 관리 프로세스) 로그에서 `failed to set up static TCP forwarding`을 확인했다. VM 내부의 loopback(자기 자신 주소) 및 InternalIP(노드 내부 주소)는 모두 HTTP 정상인데 Mac에서는 초기 curl 시간 초과와 **100/100 요청 실패**였다. [초기 실패 수치](mac-load.json)

해당 VM에 속한 오래된 SSH 프로세스만 종료하고, Lima 생성 `ssh.config`로 `127.0.0.1:30081 → guest 127.0.0.1:30080` 전달을 새로 만들었다. 이후 100/100 정상 응답했다. 이는 Kubernetes API를 우회하는 Pod port-forward가 아니라 VM NodePort까지 연결하는 SSH 전달이다. Lima 강제 종료 뒤 자동 전달 재설정이 항상 성공한다고 보장할 수 없으며, 이 환경에서는 별도 복구가 필요했다. 원본 실패 로그를 성공 로그로 덮어쓰지 않았다.

## 로컬 코드 검사

- 모든 Bash 파일 문법 검사 및 ShellCheck(셸 스크립트 정적 검사) 통과.
- manifest / NetworkPolicy YAML(설정 파일 형식) 파싱 통과.
- 기존 verify 검사 5개와 신규 부하 검사 3개: 총 8개 통과. 실제 로컬 HTTP 서버를 사용하지만 Kubernetes 호출은 일부 모킹(가짜 응답 제공)하므로 Linux 실검증과 별도로 분류한다.

## 검증하지 않은 범위와 남은 위험

- 실제 실행 환경은 Ubuntu arm64 한 종류다. Debian, x86_64, 실제 물리 서버, AWS/OpenStack/Proxmox, 회장 네트워크, 인터넷 공개 도메인·HTTPS는 미검증이다.
- replica(실행 앱 개수) 1개이므로 강제 삭제·노드 전원 장애 때 일시 중단될 수 있다. 정상 교체도 추가 Pod를 실행할 여유 자원이 필요하다.
- 테스트 복원은 제공된 sample manifest 기준이다. 사용자가 변경한 실사용 앱이나 데이터 보존 기능은 아니다. `--disposable-node`는 전용 노드에서만 사용한다.
- Cilium/Helm(클러스터 패키지 설치 도구) 작업 중 강제 종료, 설정 변경 감지, 불완전한 release 복구는 완전 자동화하지 않았다. 외부 방화벽·CIDR(네트워크 주소 범위) 충돌·이미지 레지스트리 장애도 환경별 확인이 필요하다.
- 최소 정책은 테스트 앱/클라이언트 수준이다. 클러스터 전체 격리, 호스트 접근 차단, 테넌트(사용자별 구역) 보안 검증은 아니다.
- cleanup은 실제 제거·재설치에 성공했지만 모든 커널 네트워크 잔여 상태가 모든 환경에서 제거된다는 증거는 아니다. 완전 초기화 검사는 새 VM으로 수행한다.
- DB·cache·스토리지·HA·autoscaling·GitOps·cloudflared는 구현하지 않았다. 긴 부하·디스크 고갈·메모리 부족·설치 중 다운로드 중단 시험도 수행하지 않았다.

## Provider Interface 연결 시 입력·출력 제안

현재 계약 확정이 아닌 다음 연동을 위한 제안이다. Provider(인프라 제공 환경) 코드와 공통 Linux 설치 코드를 분리한다.

| 방향 | 필요한 정보 | 현재 처리 |
| --- | --- | --- |
| 입력 | 노드 ID, 실행 경로(local/cloud-init/SSM/SSH), root 권한 | adapter(연결 담당 코드)가 실행 전 준비 |
| 입력 | 내부 IPv4, NIC(네트워크 장치), CIDR 충돌, OS·커널·CPU 종류 | `NODE_IP`와 사전 조건. CIDR 변경은 K3s/Cilium 양쪽 변경 필요 |
| 입력 | 버전, 대기 시간 | `K3S_VERSION`, `CILIUM_VERSION`, `CILIUM_CLI_VERSION`, `WAIT_TIMEOUT` |
| 입력 | DNS·다운로드 가능 여부, 외부 주소·포트·방화벽 | adapter 준비. `VERIFY_URL`은 서버 측 추가 조회용 |
| 출력 | 종료 코드, 실패 단계와 원인, 버전·Node/Cilium/workload 상태 | 현재 로그·명령 결과로 수집 |
| 출력 | 내부 HTTP와 외부 클라이언트 HTTP를 구분한 결과, 증거 경로 | 외부 검사는 adapter 실행 머신에서 별도 수행 |

통합 기계 판독 JSON(프로그램이 읽는 결과 형식) 계약은 아직 없다. 부하 측정 JSON만 구현했다. kubeconfig(클러스터 접속 설정)와 토큰은 일반 결과나 로그에 반환하지 않는다.

## 운영 확장 우선순위와 후속 도구 위치

1. 실제 대상 OS/Provider와 외부 네트워크에서 동일 검사를 반복하고 설치 설정 차이·중단된 Helm 작업 복구를 다룬다.
2. root 권한 경계, RBAC(사용자·서비스별 권한), Pod 보안, 비밀정보 관리와 최소 통신 정책을 정한다.
3. 이미지 digest(바뀌지 않는 이미지 식별값), 업데이트 승인, LKG(마지막 정상 버전) 복구와 앱별 `/healthz` 검사를 연결한다.
4. cloudflared(외부에서 내부 앱으로 연결하는 터널 도구)는 앱·Service 준비 뒤 추가하고 **외부 URL에서** 검증한다. Sealed Secrets(암호화된 설정 비밀 관리)는 네트워크 준비 뒤, 비밀정보가 필요한 앱/터널 배포 전에 추가한다.
5. 저장소·백업/복구를 정한 뒤 CNPG(Kubernetes에서 PostgreSQL을 운영하는 도구)를 네트워크 준비 다음에 설치한다. DB 준비 확인 후 DB 의존 앱을 배포한다.
6. 이후 다중 노드·HA(일부 노드 장애에도 운영을 유지하는 구성)를 별도 목표로 검증한다.

현재 테스트 VM은 마지막 전체 재설치와 재부팅 뒤 정상 상태로 실행 중이다. Mac에서 `http://127.0.0.1:30081/`로 확인할 수 있으며 기존 시연 VM의 `http://127.0.0.1:30080/`도 유지했다.
