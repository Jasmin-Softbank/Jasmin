/* A validated draft is display data. Rendering never invokes an action or sends a request. */
'use strict';
(() => {
  const kinds = {vm:'가상 머신',cluster:'클러스터',service:'앱 서비스',database:'데이터베이스',network:'네트워크',gateway:'게이트웨이',registry:'이미지 저장소',agent:'에이전트',runner:'실행기',control:'제어 서비스'};
  const owners = {terraform:'Terraform',ansible:'Ansible',argocd:'Argo CD',external:'외부 관리'};
  const relations = {depends_on:'의존',routes_to:'트래픽 전달',deploys_to:'배포',runs_on:'실행 위치',reads_from:'읽기'};
  const reasons = {topology_apply_not_connected:'이 초안을 실제 환경에 적용하는 기능은 아직 연결되지 않았어요.',ownership_not_supported:'이 구성 요소의 관리 주체를 다시 검토해야 해요.',service_limit_exceeded:'현재 앱 구성에서 허용하는 서비스 수를 넘었어요.'};
  const svgNS = 'http://www.w3.org/2000/svg';
  let counter = 0;
  const el = (tag,text,className) => {
    const value=document.createElement(tag); value.textContent=text;
    if (className) value.className=className;
    return value;
  };
  const svg = (tag,attributes={},text='') => {
    const value=document.createElementNS(svgNS,tag);
    Object.entries(attributes).forEach(([key,entry])=>value.setAttribute(key,String(entry)));
    value.textContent=text; return value;
  };
  function checked(proposal,review) {
    if (!proposal || proposal.status!=='DRAFT' || proposal.version!==1 || !['describe','create','change'].includes(proposal.intent) || !review || review.status!=='REVIEW_REQUIRED' || review.apply_supported!==false ||
        !Array.isArray(proposal.nodes) || !proposal.nodes.length || proposal.nodes.length>24 || !Array.isArray(proposal.edges) || proposal.edges.length>48 ||
        !Array.isArray(review.unsupported) || !Array.isArray(review.conflicts)) return false;
    const ids=new Set();
    for (const node of proposal.nodes) {
      if (!node || typeof node.id!=='string' || !/^[a-z][a-z0-9_-]{0,47}$/.test(node.id) || ids.has(node.id) || !Object.hasOwn(kinds,node.kind) || !Object.hasOwn(owners,node.managed_by) ||
          typeof node.label!=='string' || !node.label.length || Array.from(node.label).length>100 || /[<>\u0000-\u001f]/.test(node.label)) return false;
      ids.add(node.id);
    }
    return proposal.edges.every(edge=>edge && ids.has(edge.from) && ids.has(edge.to) && Object.hasOwn(relations,edge.kind));
  }
  window.renderTopology = function renderTopology(proposal,review,onEdit) {
    const section=el('section','','nv-topology');
    section.setAttribute('aria-label','토폴로지 초안');
    if (!checked(proposal,review)) {
      section.append(el('p','검증된 토폴로지 초안을 읽을 수 없어요. 실제 환경은 변경되지 않았습니다.','nv-topology-note'));
      return section;
    }
    const descriptive=proposal.intent==='describe';
    const heading=el('div','','nv-topology-heading');
    heading.append(el('strong',descriptive?'시스템 구성 설명':'구성 초안'),el('span',descriptive?'설명도 · 상태는 메시지 관측 시점 기준':'DRAFT · 미반영','nv-topology-status'));
    section.append(heading,el('p',descriptive?'제공된 시스템 계약과 관측을 바탕으로 작성한 설명도입니다. 자원 변경을 실행하지 않습니다.':'연결 방향과 관리 주체를 검토해 주세요. 실제 자원 상태를 나타내는 화면이 아닙니다.','nv-topology-note'));
    const viewport=el('div','','nv-topology-viewport'); viewport.tabIndex=0;
    viewport.setAttribute('aria-label','구성도. 넓은 그래프는 가로로 스크롤할 수 있습니다.');
    const columns=proposal.nodes.length===1?1:2, width=columns*264, height=Math.ceil(proposal.nodes.length/columns)*156+24;
    const diagram=svg('svg',{viewBox:`0 0 ${width} ${height}`,width,height,role:'img','aria-labelledby':`nv-topology-title-${++counter}`});
    diagram.append(svg('title',{id:`nv-topology-title-${counter}`},`${descriptive?'관측과 계약 기반 시스템 설명':'아직 적용하지 않은 구성 제안'}. 전체 이름과 연결은 아래 구성 상세에서 읽을 수 있습니다.`));
    const markerId=`nv-topology-arrow-${counter}`;
    const defs=svg('defs'),marker=svg('marker',{id:markerId,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto-start-reverse'});
    marker.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#829b8a'})); defs.append(marker); diagram.append(defs);
    const positions=new Map(proposal.nodes.map((node,index)=>[node.id,{x:12+(index%columns)*264,y:12+Math.floor(index/columns)*156}]));
    proposal.edges.forEach(edge=>{
      const a=positions.get(edge.from),b=positions.get(edge.to);
      const sameRow=a.y===b.y, forward=a.x<b.x;
      const x1=sameRow?a.x+(forward?238:0):a.x+119, y1=sameRow?a.y+60:a.y+(a.y<b.y?120:0);
      const x2=sameRow?b.x+(forward?0:238):b.x+119, y2=sameRow?b.y+60:b.y+(a.y<b.y?0:120);
      const line=svg('path',{d:`M ${x1} ${y1} L ${x2} ${y2}`,class:'nv-topology-edge','marker-end':`url(#${markerId})`});
      line.append(svg('title',{},`${edge.from} → ${edge.to}: ${relations[edge.kind]}`)); diagram.append(line);
    });
    proposal.nodes.forEach(node=>{
      const {x,y}=positions.get(node.id),group=svg('g');
      group.append(svg('rect',{x,y,width:238,height:120,rx:12,class:'nv-topology-node'}));
      group.append(svg('text',{x:x+14,y:y+23,class:'nv-topology-kind'},kinds[node.kind]));
      const label=svg('text',{x:x+14,y:y+45,class:'nv-topology-label'});
      const chars=Array.from(node.label), lines=[];
      while(chars.length) lines.push(chars.splice(0,19).join(''));
      lines.slice(0,3).forEach((line,index)=>label.append(svg('tspan',{x:x+14,dy:index?18:0},line+(index===2 && lines.length>3?'…':''))));
      label.append(svg('title',{},node.label)); group.append(label);
      group.append(svg('text',{x:x+14,y:y+106,class:'nv-topology-owner'},owners[node.managed_by])); diagram.append(group);
    });
    viewport.append(diagram);section.append(viewport);
    const details=el('details','','nv-topology-details');details.append(el('summary','구성 상세와 전체 이름'));
    const list=el('ul','');const names=new Map(proposal.nodes.map(node=>[node.id,node.label]));
    proposal.nodes.forEach(node=>list.append(el('li',`${node.label} — ${kinds[node.kind]}, ${owners[node.managed_by]}`)));
    proposal.edges.forEach(edge=>list.append(el('li',`${names.get(edge.from)} → ${names.get(edge.to)} (${relations[edge.kind]})`)));
    (proposal.assumptions || []).forEach(text=>list.append(el('li',`가정: ${text}`)));
    (proposal.evidence_refs || []).forEach(text=>list.append(el('li',`근거: ${text}`)));
    details.append(list);section.append(details);
    const notices=[...(descriptive?[]:review.unsupported),...review.conflicts.map(conflict=>conflict.reason)];
    [...new Set(notices)].forEach(reason=>section.append(el('p',reasons[reason] || '이 구성에는 아직 지원이 확인되지 않은 항목이 있어요.','nv-topology-note')));
    if (typeof onEdit==='function') {
      const edit=el('button','수정 요청 쓰기','nv-topology-edit');edit.type='button';
      edit.addEventListener('click',()=>onEdit('방금 제안한 토폴로지를 수정하고 싶어요. 원하는 변경: '));
      section.append(edit);
    }
    return section;
  };
})();
