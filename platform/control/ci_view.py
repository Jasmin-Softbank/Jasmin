"""Read-only CI presentation from trusted stage events and the final receipt."""
from execution import GATE_ORDER

TITLES = {'L0':'변경 범위 검사', 'L1':'구성·실행 계약 검사', 'Q':'의존성·코드 품질·테스트',
          'L2':'컨테이너 이미지 빌드', 'L4':'취약점·비밀 검사', 'L3':'실제 기동·상태 검사'}
TERMINAL = {'PASS','FAIL','BLOCKED','UNKNOWN','CANCELLED'}


def project(job, observations, receipt=None):
    steps = {key:{'id':key,'title':TITLES[key],'status':'QUEUED','started_at':None,
                  'finished_at':None,'duration_s':None,'evidence':'NOT_OBSERVED'} for key in GATE_ORDER}
    for event in observations['records']:
        key=event['phase']
        if key not in steps: continue
        step=steps[key]; attrs=event['attributes']; outcome=event['outcome']
        step.update(status=outcome, evidence='STAGE_EVENT', sequence=event['sequence'])
        if outcome=='RUNNING': step['started_at']=attrs.get('started_at') or event['occurred_at']
        else:
            step['started_at']=attrs.get('started_at') or step['started_at']
            step['finished_at']=event['occurred_at']
            step['duration_s']=attrs.get('duration_s')
        if attrs.get('check'): step['check']=attrs['check']
    # Earlier runs have only final stage observations. Missing timings stay unknown.
    for layer in (receipt or {}).get('details',{}).get('layers',[]):
        if layer.get('layer') in steps:
            step=steps[layer['layer']]
            step.update(status=layer['outcome'],evidence='FINAL_RECEIPT')
            for key in ('started_at','duration_s'):
                if layer.get(key) is not None: step[key]=layer[key]
    if job['status'] in TERMINAL:
        for step in steps.values():
            if step['status']=='QUEUED': step['status']='NOT_RUN'
            elif step['status']=='RUNNING': step['status']='UNKNOWN'
    finished=sum(step['status'] in TERMINAL for step in steps.values())
    return {'job_id':job['id'],'status':job['status'],'created_at':job['created_at'],
            'steps':list(steps.values()),'completed':finished,'total':len(GATE_ORDER),
            'passed':sum(step['status']=='PASS' for step in steps.values()),
            'percent':round(finished/len(GATE_ORDER)*100),'progress_basis':'completed_gate_stages',
            'events_truncated':observations['truncated'],'error':job.get('error')}
