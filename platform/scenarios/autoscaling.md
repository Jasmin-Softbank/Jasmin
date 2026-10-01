# Deployment 자동 확장 — 공통 렌더와 제한된 GCP 실검증

사용자 spec은 확장을 **요청**한다. 신뢰하는 플랫폼 호출자가 KEDA·Kubernetes Metrics Server의 준비 상태, 노드 여유, 최대치의 비용 예약을 확인한 뒤 `render(..., enable_keda=True, autoscaling_profile="bounded-v1")` 또는 CLI `--enable-keda --autoscaling-profile bounded-v1`를 전달해야 렌더링된다. 두 값은 사용자 spec에 넣을 수 없다. 기본값은 차단이며, 플래그 자체가 실시간 readiness 검사나 예산 승인을 수행하지 않는다. 기존 gate/caller가 이 승인을 전달하지 않으면 autoscaling spec은 실패한다.

```yaml
services:
  - name: api
    build: {dockerfile: Dockerfile}
    port: 8000
    route: /
    size: S
    autoscaling:
      minReplicas: 1
      maxReplicas: 3
      cpu: 70
      memory: 80
      cooldownSeconds: 300
      scaleDownStabilizationSeconds: 300
```

CPU·memory 중 하나 이상을 사용한다. 수치는 자원 **requests 대비 이용률(20–90%)**이다. Deployment(`rolling`)만 지원하며 `replicas` 동시 지정과 autoscaled Rollout은 거부한다. 최소 1개, 서비스별 최대 5개, `min <= max`를 검사한다. 임의 Prometheus URL/query, 외부 scaler, scale-to-zero는 받지 않는다. 추가 scaler는 관리자 승인 구현이 필요하다.

`bounded-v1`은 앱의 최대 정상 파드 합 15개, quota 파드 30개, requests 4 CPU·8Gi, limits 16 CPU·24Gi를 상한으로 둔다. 실제 namespace quota는 각 서비스의 **maxReplicas + rolling surge 1**, 정적 Rollout의 두 revision+surge, smoke, DB primary+임시 DB 파드, grants·migration Job 자원을 합산한다. 상한 초과는 렌더링 전에 거부한다. 계산 결과는 `meta.json.autoscaling.quota`로 비용·용량 예약 호출자가 읽는다. quota는 허용량이며 노드에 실제 자원이 있다는 증거는 아니다. 종료 중 파드 등으로 여유가 소진되면 Kubernetes가 추가 파드 생성을 막을 수 있다.

KEDA가 ScaledObject에서 HPA를 만들고 HPA가 replicas를 소유한다. 해당 Deployment는 정적 `spec.replicas`를 출력하지 않는다. AppSet은 `railshot.dev/replica-owner=keda` Deployment의 replicas에만 `ignoreDifferences`를 적용하고 `RespectIgnoreDifferences=true`로 sync 때도 존중한다. 신규 Deployment는 Kubernetes 기본 1개로 생성된 후 KEDA가 min을 적용한다. 확장 속도는 최대 1개/60초, scale-up 안정화 30초, scale-down 안정화 기본 300초다. `cooldownPeriod`는 KEDA의 **0개로 축소할 때만** 적용되므로 이 min>=1 프로필의 축소 지연은 HPA stabilization이 담당한다.

자동 확장을 해제하면 렌더러는 빈 `23-autoscaling.yaml`로 이전 산출물을 지우고 정적 replicas를 되돌린다. 기존 HPA 삭제와 정적 소유권 인계는 운영 중 동시 sync로 가정하지 않는다. 운영자는 먼저 ScaledObject를 `autoscaling.keda.sh/paused: "true"`로 일시정지하고 HPA 삭제를 확인한 다음 해제 spec을 적용해야 한다. `PruneLast` 환경에서 이전 HPA와 정적 replicas가 잠시 경합하는 것을 피하기 위한 절차이며, 현재 CLI가 이 다단계 전환을 자동 수행하지 않는다.

플랫폼 선언은 공식 Helm chart/app **2.21.0**으로 고정했다. chart SHA256은 `a7da56bf41a33dad9d2fc82a6fdecee552fff947d80bf1e1dbcf4db0f9e26dee`다. 공식 tested Kubernetes 범위는 **1.34–1.36**이며 플랫폼의 K3s `v1.36.4+k3s1`과 맞췄다(chart의 최소 버전 조건만으로 호환성을 판단하지 않는다). 설치 선언만으로 준비 완료를 판단하지 않는다. 관리자 확인 항목은 ScaledObject CRD Established, KEDA operator/admission/metrics deployment Available, `metrics.k8s.io` API Available 및 실제 PodMetrics 응답이다. 운영 중 metric 장애는 min/max 범위의 성공적인 확장을 보장하지 않으므로 ScaledObject/HPA conditions를 관측해야 한다.

로컬 검증: `python3 platform/render/render.py --self-test`, `python3 -m unittest discover -s platform/render -p 'test_autoscaling.py' -v` (기존 PyYAML/jsonschema와 jq 필요). capability·profile 거부, 잘못된 범위, Rollout/임의 URL 거부, HPA 소유권, quota의 최대치·여유 합산, opt-out 잔존 파일, 실제 Argo JQ 선택 범위를 검사한다. 실제 부하·Metrics Server·KEDA/HPA·Argo sync 검증은 별도 클러스터 단계다.

KEDA는 **앱 파드 수**를 조절한다. 노드/VM 증설, 청구 조회, 예산 차단은 별도 플랫폼 제어가 담당한다. maxReplicas는 그 제어가 사전에 승인·예약한 ceiling이다.

공식 근거: [Helm index](https://kedacore.github.io/charts/index.yaml), [Kubernetes 호환성](https://keda.sh/docs/2.21/operate/cluster/), [CPU prerequisites](https://keda.sh/docs/2.21/scalers/cpu/), [Memory scaler](https://keda.sh/docs/2.21/scalers/memory/), [ScaledObject와 cooldown/HPA](https://keda.sh/docs/2.21/reference/scaledobject-spec/), [Argo diff customization](https://argo-cd.readthedocs.io/en/stable/user-guide/diffing/), [RespectIgnoreDifferences](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/#respect-ignore-differences-configs).

## 2026-10-01 GCP 실검증

서울 GCP k3s에서 Metrics Server와 KEDA 2.21.0을 설치하고 Argo root/KEDA의 Synced·Healthy를 확인했다. 격리 CPU fixture에서 파드 1→3 확장과 부하 제거 뒤 3→1 축소를 관측했으며 fixture를 삭제했다. VM stop/start 뒤 KEDA 정상 복귀도 확인했다. 이 fixture는 사용자 앱 renderer/ApplicationSet의 전체 배포·replica 소유권 전환 시험은 아니다. 조건과 남은 인수 시험은 [cloud-validation.md](cloud-validation.md)를 따른다.
