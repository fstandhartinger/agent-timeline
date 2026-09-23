(() => {
  'use strict';
  let TOPICS = [];
  const PALETTE = ['#ff725e','#67a9ff','#54d2bd','#bb93ff','#ffae5d','#f27cac','#a8a0ff','#80d6a8','#6fd0df','#e8c96a','#b6d96e','#94a1b6','#5ecbb0'];
  const COLORS = {other:'#778397'};
  const RANGE = { '4w':28*86400, '1w':7*86400, '1d':86400, '6h':21600, '1h':3600 };
  const $ = (s) => document.querySelector(s);
  const el = {
    loginScreen:$('#login-screen'), loginForm:$('#login-form'), loginError:$('#login-error'),
    app:$('#app'), scroll:$('#timeline-scroll'), canvas:$('#timeline-canvas'), spacer:$('#scroll-spacer'),
    area:$('#area-chart'), areaEmpty:$('#area-empty'), legend:$('#legend'), tooltip:$('#tooltip'),
    search:$('#search'),
  };
  const state = {
    agents:[],stats:{},rows:[],rangeKey:'4w',span:RANGE['4w'],to:Math.floor(Date.now()/1000),
    from:Math.floor(Date.now()/1000)-RANGE['4w'],selected:new Set(),collapsed:new Set(),
    collapsedGroups:new Set(),search:'',rowHeight:29,axisHeight:50,labelWidth:265,plotX:265,plotW:0,
    dragging:false,lastX:0,dragMoved:false,hover:null,theme:localStorage.getItem('agent-theme')||'dark',
    requestId:0,loading:false,agentById:new Map(),childCounts:new Map(),
  };
  document.documentElement.dataset.theme=state.theme;
  const passwordInput=$('#password'),passwordToggle=$('#password-toggle');
  if(!(window.CSS&&CSS.supports&&(CSS.supports('-webkit-text-security','disc')||CSS.supports('text-security','disc')))){
    passwordInput.type='password';passwordInput.classList.remove('masked');
  }
  passwordToggle.addEventListener('click',()=>{
    const show=passwordInput.classList.toggle('revealed');passwordInput.classList.toggle('masked',!show);
    passwordToggle.setAttribute('aria-pressed',String(show));passwordToggle.setAttribute('aria-label',show?'Hide password':'Show password');
  });

  function authView(ok){el.loginScreen.hidden=ok;el.app.hidden=!ok;}
  function setMessage(text){el.loginError.textContent=text||'';}
  async function checkSession(){
    try{const r=await fetch('/api/session',{credentials:'same-origin',cache:'no-store'});if(r.ok){authView(true);await loadData();return;}}catch(_e){}
    authView(false);
  }
  el.loginForm.addEventListener('submit',async e=>{
    e.preventDefault();setMessage('Signing in…');
    const username=$('#username').value,password=$('#password').value;
    try{
      const r=await fetch('/api/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})});
      const data=await r.json();
      if(!r.ok){setMessage(data.error||'Login failed.');return;}
      $('#password').value='';setMessage('');authView(true);await loadData();
    }catch(_err){setMessage('Could not reach the timeline. Try again.');}
  });
  $('#logout').addEventListener('click',async()=>{try{await fetch('/api/logout',{method:'POST',credentials:'same-origin'});}catch(_e){}authView(false);});
  $('#theme-toggle').addEventListener('click',()=>{
    state.theme=state.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=state.theme;
    localStorage.setItem('agent-theme',state.theme);draw();drawArea();
  });
  $('#clear-filters').addEventListener('click',()=>{state.selected=new Set(TOPICS);buildRows();drawArea();});
  $('#zoom-in').addEventListener('click',()=>zoom(0.6));$('#zoom-out').addEventListener('click',()=>zoom(1.7));
  document.querySelectorAll('.preset').forEach(b=>b.addEventListener('click',()=>setRange(b.dataset.range)));
  el.search.addEventListener('input',()=>{state.search=el.search.value.trim().toLowerCase();buildRows();});
  el.scroll.addEventListener('scroll',()=>requestAnimationFrame(draw));
  window.addEventListener('resize',()=>{resizeCanvases();draw();drawArea();});

  async function loadData(){
    if(state.loading)return;state.loading=true;const id=++state.requestId;
    try{
      const sr=await fetch('/api/stats',{credentials:'same-origin',cache:'no-store'});
      if(sr.status===401){authView(false);return;}if(!sr.ok)throw Error('stats');
      state.stats=await sr.json();
      TOPICS=Object.keys(state.stats.topics||{}).sort((a,b)=>a.localeCompare(b));
      if(!TOPICS.length)TOPICS=['other'];
      TOPICS.forEach((topic,i)=>COLORS[topic]=topic==='other'?'#778397':PALETTE[i%PALETTE.length]);
      state.selected=new Set(TOPICS);
      const now=Math.floor(Date.now()/1000),min=Number(state.stats.earliest||0);
      const from=Math.max(0,min-3600);
      const ar=await fetch(`/api/agents?from=${from}&to=${now+1}`,{credentials:'same-origin',cache:'no-store'});
      if(ar.status===401){authView(false);return;}if(!ar.ok)throw Error('agents');
      const data=await ar.json();if(id!==state.requestId)return;
      state.agents=data.agents||[];state.to=now;state.from=now-state.span;
      renderStats();buildLegend();buildRows();resizeCanvases();drawArea();draw();
      $('#source-status').textContent=`${fmtCount(state.agents.length)} records indexed`;
    }catch(_err){$('#source-status').textContent='Waiting for history service';}
    finally{state.loading=false;}
  }
  function renderStats(){
    const inView=state.agents.filter(a=>overlaps(a,state.from,state.to));
    $('#metric-agents').textContent=fmtCount(inView.length);
    $('#metric-links').textContent=fmtCount(inView.filter(a=>a.parent_id).length);
    $('#metric-active').textContent=fmtCount(inView.filter(a=>!a.end).length);
    $('#metric-projects').textContent=fmtCount(new Set(inView.map(a=>a.topic)).size);
    $('#metric-range').textContent=({'4w':'Last 4 weeks','1w':'Last 7 days','1d':'Last 24 hours','6h':'Last 6 hours','1h':'Last hour'})[state.rangeKey]||'Selected window';
    $('#metric-collected').textContent=state.stats.collected_at?`Updated ${timeAgo(state.stats.collected_at)}`:'Waiting for collector';
    const max=peakCount(inView);$('#peak-count').textContent=fmtCount(max);
    updateClock();
  }
  function overlaps(a,from,to){const end=a.end||Math.floor(Date.now()/1000);return a.start<=to&&end>=from;}
  function buildLegend(){
    const counts={};for(const a of state.agents)if(overlaps(a,state.from,state.to))counts[a.topic]=(counts[a.topic]||0)+1;
    el.legend.replaceChildren();
    for(const topic of TOPICS){if(!counts[topic])continue;
      const b=document.createElement('button');b.className='legend-item'+(state.selected.has(topic)?'':' off');b.type='button';
      const sw=document.createElement('span');sw.className='legend-swatch';sw.style.color=COLORS[topic];sw.style.background=COLORS[topic];
      const label=document.createElement('span');label.className='legend-label';label.textContent=topic;
      const count=document.createElement('span');count.className='legend-count';count.textContent=counts[topic];
      b.append(sw,label,count);b.title=`Filter ${topic}`;
      b.addEventListener('click',()=>{if(state.selected.has(topic))state.selected.delete(topic);else state.selected.add(topic);b.classList.toggle('off',!state.selected.has(topic));buildRows();drawArea();});
      el.legend.append(b);
    }
  }
  function buildRows(){
    state.agentById=new Map(state.agents.map(a=>[a.id,a]));state.childCounts=new Map();
    for(const agent of state.agents)if(agent.parent_id)state.childCounts.set(agent.parent_id,(state.childCounts.get(agent.parent_id)||0)+1);
    const eligible=state.agents.filter(a=>overlaps(a,state.from,state.to)&&state.selected.has(a.topic)&&(!state.search||`${a.name} ${a.job_dir||''} ${a.engine||''} ${a.model||''} ${a.topic}`.toLowerCase().includes(state.search)));
    const ids=new Set(eligible.map(a=>a.id)),byId=new Map(eligible.map(a=>[a.id,a]));
    const kids=new Map();for(const a of eligible){if(!a.parent_id||!ids.has(a.parent_id))continue;if(!kids.has(a.parent_id))kids.set(a.parent_id,[]);kids.get(a.parent_id).push(a);}
    for(const list of kids.values())list.sort((a,b)=>a.start-b.start);
    const roots=eligible.filter(a=>!a.parent_id||!ids.has(a.parent_id)).sort((a,b)=>a.start-b.start);
    const buckets=new Map();
    for(const root of roots){const topic=root.topic||'other';if(!buckets.has(topic))buckets.set(topic,[]);buckets.get(topic).push(root);}
    const topics=[...buckets.keys()].sort((a,b)=>TOPICS.indexOf(a)-TOPICS.indexOf(b));
    const rows=[];const seen=new Set();
    for(const topic of topics){
      const group=buckets.get(topic);rows.push({kind:'group',topic,count:countTree(group,kids)});
      if(state.collapsedGroups.has(topic))continue;
      const walk=(agent,depth)=>{
        if(seen.has(agent.id))return;seen.add(agent.id);const children=kids.get(agent.id)||[];
        rows.push({kind:'agent',agent,depth,childCount:(state.childCounts.get(agent.id)||0)});
        if(state.collapsed.has(agent.id))return;for(const child of children)walk(child,depth+1);
      };
      for(const root of group)walk(root,0);
    }
    // A cycle or incomplete parent reference must not make a real session disappear.
    for(const a of eligible)if(!seen.has(a.id))rows.push({kind:'agent',agent:a,depth:0,childCount:0});
    state.rows=rows;state.filteredCount=eligible.length;
    $('#row-count').textContent=`${fmtCount(eligible.length)} agents`;
    el.spacer.style.height=`${Math.max(0,rows.length*state.rowHeight)}px`;
    if(el.scroll.scrollTop>rows.length*state.rowHeight)el.scroll.scrollTop=Math.max(0,rows.length*state.rowHeight);
    resizeCanvases();draw();
  }
  function countTree(roots,kids){let n=0;const seen=new Set();const visit=a=>{if(seen.has(a.id))return;seen.add(a.id);n++;for(const c of kids.get(a.id)||[])visit(c);};roots.forEach(visit);return n;}

  function resizeCanvases(){
    for(const canvas of [el.canvas,el.area]){
      const rect=canvas.getBoundingClientRect(),dpr=Math.min(2,window.devicePixelRatio||1);
      const w=Math.max(1,Math.round(rect.width*dpr)),h=Math.max(1,Math.round(rect.height*dpr));
      if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;}
      const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);
    }
    const width=el.canvas.clientWidth;
    state.labelWidth=Math.min(305,Math.max(147,width*(width<560?.37:.275)));
    state.plotX=state.labelWidth;state.plotW=Math.max(80,width-state.plotX-16);
  }
  function getContext(canvas){const ctx=canvas.getContext('2d');const dpr=Math.min(2,window.devicePixelRatio||1);ctx.setTransform(dpr,0,0,dpr,0,0);return ctx;}
  function draw(){
    if(el.app.hidden)return;
    const c=el.canvas,ctx=getContext(c),w=c.clientWidth,h=c.clientHeight;
    if(!w||!h)return;
    ctx.clearRect(0,0,w,h);
    const css=getComputedStyle(document.documentElement),surface=css.getPropertyValue('--surface').trim(),surface2=css.getPropertyValue('--surface-2').trim(),line=css.getPropertyValue('--line-soft').trim(),text=css.getPropertyValue('--text').trim(),muted=css.getPropertyValue('--muted').trim(),muted2=css.getPropertyValue('--muted-2').trim(),coral=css.getPropertyValue('--coral').trim();
    const rowH=state.rowHeight,scrollTop=el.scroll.scrollTop,axis=state.axisHeight,plotX=state.plotX,plotW=state.plotW,span=Math.max(1,state.to-state.from);
    ctx.fillStyle=surface;ctx.fillRect(0,0,w,h);ctx.fillStyle=surface2;ctx.fillRect(0,0,plotX,h);
    ctx.fillStyle=surface;ctx.fillRect(0,0,w,axis);
    const step=niceStep(span);ctx.font='9px "DM Mono",monospace';ctx.textAlign='center';ctx.textBaseline='middle';
    let tick=Math.ceil(state.from/step)*step;
    for(;tick<=state.to;tick+=step){const x=plotX+(tick-state.from)/span*plotW;ctx.strokeStyle=line;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x,axis);ctx.lineTo(x,h);ctx.stroke();ctx.fillStyle=muted2;ctx.fillText(formatTick(tick,span),x,axis-13);}
    ctx.strokeStyle=line;ctx.beginPath();ctx.moveTo(plotX,axis);ctx.lineTo(w,axis);ctx.stroke();
    const first=Math.max(0,Math.floor(scrollTop/rowH));const last=Math.min(state.rows.length,first+Math.ceil((h-axis)/rowH)+3);
    const rowYs=new Map();
    for(let i=first;i<last;i++)if(state.rows[i].kind==='agent')rowYs.set(state.rows[i].agent.id,axis+i*rowH-scrollTop+rowH/2);
    // Soft project clouds sit below the session branches.
    let groupStart=null;
    for(let i=0;i<state.rows.length;i++){
      if(state.rows[i].kind==='group'){
        if(groupStart!==null)drawCloud(ctx,state.rows[groupStart].topic,groupStart,i-1,axis,scrollTop,rowH,plotX,plotW,state.from,state.to);
        groupStart=i;
      }
    }
    if(groupStart!==null)drawCloud(ctx,state.rows[groupStart].topic,groupStart,state.rows.length-1,axis,scrollTop,rowH,plotX,plotW,state.from,state.to);
    // Branch connectors are drawn first so the bars remain crisp.
    ctx.lineWidth=1;ctx.strokeStyle='rgba(149,161,184,.34)';
    for(let i=first;i<last;i++){
      const row=state.rows[i];if(row.kind!=='agent'||!row.agent.parent_id)continue;
      const parent=state.agentById.get(row.agent.parent_id);if(!parent)continue;
      const y=rowYs.get(row.agent.id),py=rowYs.get(parent.id);if(y===undefined||py===undefined)continue;
      const px=timeX(parent.end||state.to,state.from,state.to,plotX,plotW),cx=timeX(row.agent.start,state.from,state.to,plotX,plotW);
      ctx.beginPath();ctx.moveTo(px,py);ctx.bezierCurveTo(px+16,py,cx-16,y,cx,y);ctx.stroke();
    }
    for(let i=first;i<last;i++){
      const row=state.rows[i],y=axis+i*rowH-scrollTop;
      if(y+rowH<axis-1||y>h)continue;
      if(row.kind==='group'){
        ctx.fillStyle=surface2;ctx.fillRect(0,y,w,rowH);
        ctx.fillStyle=COLORS[row.topic]||COLORS.other;ctx.beginPath();ctx.arc(13,y+rowH/2,3.2,0,Math.PI*2);ctx.fill();
        ctx.fillStyle=muted;ctx.textAlign='left';ctx.textBaseline='middle';ctx.font='600 9px "DM Sans",sans-serif';
        const closed=state.collapsedGroups.has(row.topic);ctx.fillText(closed?'▸':'▾',24,y+rowH/2+0.3);
        ctx.fillStyle=text;ctx.font='600 9px "DM Sans",sans-serif';ctx.fillText(row.topic.toUpperCase(),39,y+rowH/2+.3);
        ctx.fillStyle=muted2;ctx.textAlign='right';ctx.font='9px "DM Mono",monospace';ctx.fillText(String(row.count),plotX-13,y+rowH/2+.3);
        ctx.strokeStyle=line;ctx.beginPath();ctx.moveTo(0,y+rowH);ctx.lineTo(w,y+rowH);ctx.stroke();
        continue;
      }
      const a=row.agent,color=COLORS[a.topic]||COLORS.other;
      if((i%2)===0){ctx.fillStyle='rgba(120,135,160,.025)';ctx.fillRect(0,y,w,rowH);}
      const indent=Math.min(row.depth,7)*13,labelX=29+indent;
      if(row.childCount){ctx.fillStyle=muted2;ctx.font='11px "DM Sans",sans-serif';ctx.textAlign='left';ctx.textBaseline='middle';ctx.fillText(state.collapsed.has(a.id)?'›':'⌄',10+indent,y+rowH/2);}
      if(row.depth>0){ctx.strokeStyle='rgba(149,161,184,.19)';ctx.beginPath();ctx.moveTo(15+(row.depth-1)*13,y-rowH/2);ctx.lineTo(15+(row.depth-1)*13,y+rowH/2);ctx.lineTo(labelX-7,y+rowH/2);ctx.stroke();}
      ctx.fillStyle=text;ctx.globalAlpha=.92;ctx.font=`${row.depth?'400':'500'} 10px "DM Sans",sans-serif`;ctx.textAlign='left';ctx.textBaseline='middle';
      let display=a.name||a.engine||'Agent';if(display.length>35)display=display.slice(0,33)+'…';ctx.fillText(display,labelX,y+rowH/2+.1);
      ctx.globalAlpha=.5;ctx.fillStyle=muted2;ctx.textAlign='right';ctx.font='8px "DM Mono",monospace';ctx.fillText(a.model||a.engine||'',plotX-8,y+rowH/2+.1);ctx.globalAlpha=1;
      const start=Math.max(state.from,a.start),end=Math.min(state.to,a.end||Math.floor(Date.now()/1000));
      const bx=timeX(start,state.from,state.to,plotX,plotW),ex=timeX(end,state.from,state.to,plotX,plotW),bw=Math.max(3,ex-bx);
      ctx.fillStyle=color;ctx.globalAlpha=.16;roundRect(ctx,bx,y+7,bw,rowH-14,5);ctx.fill();ctx.globalAlpha=1;
      ctx.fillStyle=color;ctx.globalAlpha=.9;roundRect(ctx,bx,y+9,Math.min(bw,Math.max(5,bw*.85)),rowH-18,4);ctx.fill();ctx.globalAlpha=1;
      if(!a.end){ctx.fillStyle=color;ctx.beginPath();ctx.arc(Math.min(w-6,bx+bw),y+rowH/2,2.6,0,Math.PI*2);ctx.fill();}
      ctx.strokeStyle=line;ctx.beginPath();ctx.moveTo(0,y+rowH);ctx.lineTo(w,y+rowH);ctx.stroke();
    }
    if(state.from<=Math.floor(Date.now()/1000)&&state.to>=Math.floor(Date.now()/1000)){
      const nx=timeX(Math.floor(Date.now()/1000),state.from,state.to,plotX,plotW);
      ctx.save();ctx.setLineDash([3,4]);ctx.strokeStyle=coral;ctx.globalAlpha=.9;ctx.lineWidth=1.25;ctx.beginPath();ctx.moveTo(nx,axis);ctx.lineTo(nx,h);ctx.stroke();ctx.restore();
      ctx.fillStyle=coral;roundRect(ctx,Math.min(w-36,Math.max(plotX,nx-15)),2,31,15,4);ctx.fill();ctx.fillStyle='#151720';ctx.textAlign='center';ctx.textBaseline='middle';ctx.font='600 7px "DM Mono",monospace';ctx.fillText('NOW',Math.min(w-20,Math.max(plotX+15,nx)),9.5);
    }
    const startLabel=new Date(state.from*1000),endLabel=new Date(state.to*1000);
    $('#window-label').textContent=`${dateShort(startLabel)} — ${dateShort(endLabel)}`;
  }
  function drawCloud(ctx,topic,startRow,endRow,axis,scrollTop,rowH,plotX,plotW,from,to){
    const selected=state.rows.slice(startRow+1,endRow+1).filter(r=>r.kind==='agent').map(r=>r.agent).filter(a=>a.topic===topic);
    if(selected.length<2)return;
    const min=Math.min(...selected.map(a=>a.start)),max=Math.max(...selected.map(a=>a.end||state.to));
    const x1=timeX(Math.max(from,min),from,to,plotX,plotW),x2=timeX(Math.min(to,max),from,to,plotX,plotW);
    const y1=axis+startRow*rowH-scrollTop,y2=axis+(endRow+1)*rowH-scrollTop;
    const cx=(x1+x2)/2,cy=(y1+y2)/2;const radius=Math.max(50,Math.min(plotW,Math.abs(x2-x1)+85));
    const grad=ctx.createRadialGradient(cx,cy,4,cx,cy,radius);
    grad.addColorStop(0,hexAlpha(COLORS[topic]||COLORS.other,.085));grad.addColorStop(1,hexAlpha(COLORS[topic]||COLORS.other,0));
    ctx.save();ctx.beginPath();ctx.rect(plotX,y1,plotW,Math.max(0,y2-y1));ctx.clip();ctx.fillStyle=grad;ctx.fillRect(plotX,y1,plotW,Math.max(0,y2-y1));ctx.restore();
  }
  function hexAlpha(hex,a){const h=hex.replace('#','');const n=parseInt(h,16);return `rgba(${(n>>16)&255},${(n>>8)&255},${n&255},${a})`;}
  function roundRect(ctx,x,y,w,h,r){if(w<=0||h<=0)return;const q=Math.min(r,w/2,h/2);ctx.beginPath();ctx.moveTo(x+q,y);ctx.arcTo(x+w,y,x+w,y+h,q);ctx.arcTo(x+w,y+h,x,y+h,q);ctx.arcTo(x,y+h,x,y,q);ctx.arcTo(x,y,x+w,y,q);ctx.closePath();}
  function timeX(t,from,to,x,w){return x+(t-from)/Math.max(1,to-from)*w;}
  function niceStep(span){const target=span/6;const units=[900,1800,3600,7200,10800,21600,43200,86400,172800,345600,604800,1209600,2419200];return units.find(x=>x>=target)||2419200;}
  function formatTick(t,span){const d=new Date(t*1000);if(span>3*86400)return d.toLocaleDateString(undefined,{month:'short',day:'numeric'});if(span>86400)return d.toLocaleDateString(undefined,{weekday:'short',hour:'2-digit'});return d.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit',hour12:false});}
  function dateShort(d){return d.toLocaleDateString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});}
  function duration(s){if(!Number.isFinite(s)||s<0)return '—';if(s<60)return `${Math.round(s)}s`;if(s<3600)return `${Math.floor(s/60)}m ${Math.floor(s%60)}s`;if(s<86400)return `${Math.floor(s/3600)}h ${Math.floor(s%3600/60)}m`;return `${Math.floor(s/86400)}d ${Math.floor(s%86400/3600)}h`;}
  function fmtTime(t){return t?new Date(t*1000).toLocaleString(undefined,{month:'short',day:'numeric',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';}
  function fmtCount(n){return new Intl.NumberFormat().format(Number(n)||0);}
  function timeAgo(t){const s=Math.max(0,Math.floor(Date.now()/1000-t));if(s<60)return 'just now';if(s<3600)return `${Math.floor(s/60)}m ago`;if(s<86400)return `${Math.floor(s/3600)}h ago`;return `${Math.floor(s/86400)}d ago`;}
  function peakCount(agents){const events=[];for(const a of agents){events.push([a.start,1]);if(a.end)events.push([a.end,-1]);}events.sort((a,b)=>a[0]-b[0]||b[1]-a[1]);let n=0,max=0;for(const e of events){n+=e[1];max=Math.max(max,n);}return max;}
  function drawArea(){
    if(el.app.hidden)return;resizeCanvases();const ctx=getContext(el.area),w=el.area.clientWidth,h=el.area.clientHeight;
    const agents=state.agents.filter(a=>state.selected.has(a.topic)&&overlaps(a,state.from,state.to));
    ctx.clearRect(0,0,w,h);if(!agents.length){el.areaEmpty.hidden=false;return;}el.areaEmpty.hidden=true;
    const n=Math.max(40,Math.min(160,Math.floor(w/8))),topics=TOPICS.filter(t=>agents.some(a=>a.topic===t));
    const points=[];let peak=0;
    for(let i=0;i<=n;i++){
      const t=state.from+(state.to-state.from)*i/n;const counts={};let total=0;
      for(const a of agents)if(a.start<=t&&(a.end||Math.floor(Date.now()/1000))>=t){counts[a.topic]=(counts[a.topic]||0)+1;total++;}
      peak=Math.max(peak,total);points.push({t,counts});
    }
    const top=Math.max(1,Math.ceil(peak/2)*2),base=h-5,chartH=h-12;ctx.font='8px "DM Mono",monospace';ctx.textAlign='right';ctx.textBaseline='middle';
    for(let k=1;k<=2;k++){const y=base-chartH*k/2;ctx.strokeStyle=getComputedStyle(document.documentElement).getPropertyValue('--line-soft').trim();ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke();ctx.fillStyle=getComputedStyle(document.documentElement).getPropertyValue('--muted-2').trim();ctx.fillText(String(Math.round(top*k/2)),w-2,y-6);}
    for(const topic of topics){ctx.beginPath();
      points.forEach((p,i)=>{let stack=0;for(const prev of topics){if(prev===topic)break;stack+=p.counts[prev]||0;}const v=p.counts[topic]||0;const x=i/n*w,y=base-chartH*(stack+v)/top;i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
      for(let i=points.length-1;i>=0;i--){const p=points[i];let stack=0;for(const prev of topics){if(prev===topic)break;stack+=p.counts[prev]||0;}const x=i/n*w,y=base-chartH*stack/top;ctx.lineTo(x,y);}
      ctx.closePath();ctx.fillStyle=hexAlpha(COLORS[topic]||COLORS.other,.52);ctx.fill();
    }
    $('#peak-count').textContent=fmtCount(peak);
  }

  function tooltipFor(agent){
    const childCount=state.childCounts.get(agent.id)||0;const parent=agent.parent_id?state.agentById.get(agent.parent_id):null;
    const now=Math.floor(Date.now()/1000),end=agent.end||now;
    const fields=[['Engine',agent.engine||'—'],['Model',agent.model||'—'],['Started',fmtTime(agent.start)],['Ended',agent.end?fmtTime(agent.end):'Running now'],['Duration',duration(end-agent.start)],['Topic',agent.topic||'other'],['Parent',parent?.name||'Top-level'],['Children',String(childCount)],['Job dir',agent.job_dir||agent.unit_name||'—']];
    const wrap=document.createElement('div');const title=document.createElement('div');title.className='tooltip-title';title.textContent=agent.name||agent.engine||'Agent';wrap.append(title);
    const topic=document.createElement('div');topic.className='tooltip-topic';topic.style.color=COLORS[agent.topic]||COLORS.other;topic.textContent=agent.topic||'other';wrap.append(topic);
    const grid=document.createElement('div');grid.className='tooltip-grid';for(const [k,v] of fields){const a=document.createElement('span'),b=document.createElement('span');a.textContent=k;b.textContent=v;grid.append(a,b);}wrap.append(grid);
    if(agent.board_url&&/^https:\/\//.test(agent.board_url)){const link=document.createElement('a');link.className='tooltip-link';link.href=agent.board_url;link.target='_blank';link.rel='noopener noreferrer';link.textContent='Open agent-board thread ↗';wrap.append(link);}
    return wrap;
  }
  function showTooltip(event){
    const rect=el.canvas.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;
    const idx=Math.floor((y-state.axisHeight+el.scroll.scrollTop)/state.rowHeight),row=state.rows[idx];
    if(!row||row.kind!=='agent'){el.tooltip.hidden=true;state.hover=null;return;}
    state.hover=row.agent;el.tooltip.replaceChildren(tooltipFor(row.agent));el.tooltip.hidden=false;
    const box=el.scroll.getBoundingClientRect();const left=Math.min(Math.max(6,x+13),box.width-302),top=Math.max(6,Math.min(el.scroll.scrollTop+y+12,el.scroll.scrollTop+box.height-210));
    el.tooltip.style.left=`${left}px`;el.tooltip.style.top=`${top}px`;
  }
  function activateAt(clientX,clientY){
    const rect=el.canvas.getBoundingClientRect(),x=clientX-rect.left,y=clientY-rect.top;
    const idx=Math.floor((y-state.axisHeight+el.scroll.scrollTop)/state.rowHeight),row=state.rows[idx];
    if(row&&row.kind==='group'&&x<state.labelWidth){if(state.collapsedGroups.has(row.topic))state.collapsedGroups.delete(row.topic);else state.collapsedGroups.add(row.topic);buildRows();}
    else if(row&&row.kind==='agent'&&x<state.labelWidth&&row.childCount&&x<36+row.depth*13){if(state.collapsed.has(row.agent.id))state.collapsed.delete(row.agent.id);else state.collapsed.add(row.agent.id);buildRows();}
    else if(row&&row.kind==='agent')showTooltip({clientX,clientY});
  }
  el.canvas.addEventListener('pointermove',e=>{
    if(e.pointerType==='touch')return;
    if(state.dragging){const dx=e.clientX-state.lastX;if(Math.abs(dx)>2)state.dragMoved=true;state.from-=dx/state.plotW*state.span;state.to-=dx/state.plotW*state.span;state.lastX=e.clientX;drawArea();draw();return;}
    showTooltip(e);
  });
  el.canvas.addEventListener('pointerleave',()=>{if(!state.dragging)el.tooltip.hidden=true;});
  el.canvas.addEventListener('pointerdown',e=>{if(e.pointerType==='touch'||e.button!==0)return;state.dragging=true;state.dragMoved=false;state.lastX=e.clientX;el.canvas.setPointerCapture(e.pointerId);el.canvas.style.cursor='grabbing';});
  el.canvas.addEventListener('pointerup',e=>{
    if(e.pointerType==='touch')return;
    state.dragging=false;el.canvas.style.cursor='';
    if(!state.dragMoved)activateAt(e.clientX,e.clientY);
    state.dragMoved=false;
  });
  el.canvas.addEventListener('pointercancel',()=>{state.dragging=false;state.dragMoved=false;el.canvas.style.cursor='';});
  let touchGesture=null;
  const pointDistance=(a,b)=>Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY);
  el.canvas.addEventListener('touchstart',e=>{
    e.preventDefault();el.tooltip.hidden=true;
    if(e.touches.length>=2){
      const a=e.touches[0],b=e.touches[1],rect=el.canvas.getBoundingClientRect(),mid=(a.clientX+b.clientX)/2-rect.left;
      const ratio=Math.min(1,Math.max(0,(mid-state.plotX)/Math.max(1,state.plotW)));
      touchGesture={mode:'pinch',distance:pointDistance(a,b),from:state.from,to:state.to,ratio,anchor:state.from+(state.to-state.from)*ratio,moved:true};
    }else if(e.touches.length===1){const t=e.touches[0];touchGesture={mode:'pending',startX:t.clientX,startY:t.clientY,lastX:t.clientX,lastY:t.clientY,moved:false};}
  },{passive:false});
  el.canvas.addEventListener('touchmove',e=>{
    e.preventDefault();if(!touchGesture)return;
    if(e.touches.length>=2){
      const a=e.touches[0],b=e.touches[1],rect=el.canvas.getBoundingClientRect(),mid=(a.clientX+b.clientX)/2-rect.left;
      if(touchGesture.mode!=='pinch'){
        const ratio=Math.min(1,Math.max(0,(mid-state.plotX)/Math.max(1,state.plotW)));
        touchGesture={mode:'pinch',distance:pointDistance(a,b),from:state.from,to:state.to,ratio,anchor:state.from+(state.to-state.from)*ratio,moved:true};
      }
      const span0=touchGesture.to-touchGesture.from,span=Math.max(3600,Math.min(400*86400,span0*touchGesture.distance/Math.max(12,pointDistance(a,b))));
      state.span=span;state.from=touchGesture.anchor-span*touchGesture.ratio;state.to=state.from+span;state.rangeKey=null;
      document.querySelectorAll('.preset').forEach(x=>x.classList.remove('selected'));renderStats();drawArea();draw();return;
    }
    const t=e.touches[0];if(touchGesture.mode==='pinch')return;
    const totalX=t.clientX-touchGesture.startX,totalY=t.clientY-touchGesture.startY;
    if(touchGesture.mode==='pending'&&Math.max(Math.abs(totalX),Math.abs(totalY))>7){touchGesture.mode=Math.abs(totalX)>Math.abs(totalY)?'pan-x':'pan-y';touchGesture.moved=true;}
    const dx=t.clientX-touchGesture.lastX,dy=t.clientY-touchGesture.lastY;
    if(touchGesture.mode==='pan-x'){state.from-=dx/Math.max(1,state.plotW)*state.span;state.to-=dx/Math.max(1,state.plotW)*state.span;drawArea();draw();}
    if(touchGesture.mode==='pan-y')el.scroll.scrollTop-=dy;
    touchGesture.lastX=t.clientX;touchGesture.lastY=t.clientY;
  },{passive:false});
  el.canvas.addEventListener('touchend',e=>{
    if(e.touches.length===1&&touchGesture?.mode==='pinch'){const t=e.touches[0];touchGesture={mode:'pending',startX:t.clientX,startY:t.clientY,lastX:t.clientX,lastY:t.clientY,moved:true};return;}
    if(e.touches.length>0)return;
    const gesture=touchGesture;touchGesture=null;
    if(gesture&&!gesture.moved&&e.changedTouches.length){const t=e.changedTouches[0];activateAt(t.clientX,t.clientY);}
  },{passive:false});
  el.canvas.addEventListener('wheel',e=>{
    if(e.ctrlKey||e.metaKey){e.preventDefault();const rect=el.canvas.getBoundingClientRect(),anchor=(e.clientX-rect.left-state.plotX)/Math.max(1,state.plotW);zoom(e.deltaY<0?.78:1.28,Math.min(1,Math.max(0,anchor)));}
    else if(e.shiftKey){e.preventDefault();const shift=e.deltaY||e.deltaX;state.from+=shift/state.plotW*state.span;state.to+=shift/state.plotW*state.span;drawArea();draw();}
    else {el.tooltip.hidden=true;}
  },{passive:false});
  function zoom(factor,anchor=.5){const current=state.to-state.from,next=Math.max(3600,Math.min(400*86400,current*factor)),t=state.from+current*anchor;state.from=t-next*anchor;state.to=state.from+next;state.span=next;state.rangeKey=null;document.querySelectorAll('.preset').forEach(x=>x.classList.remove('selected'));renderStats();buildLegend();drawArea();draw();}
  function setRange(key){state.rangeKey=key;state.span=RANGE[key];state.to=Math.floor(Date.now()/1000);state.from=state.to-state.span;document.querySelectorAll('.preset').forEach(b=>b.classList.toggle('selected',b.dataset.range===key));renderStats();buildLegend();buildRows();drawArea();}
  function updateClock(){const d=new Date();$('#clock').textContent=d.toLocaleTimeString(undefined,{hour12:false});$('#today-label').textContent=d.toLocaleDateString(undefined,{weekday:'long',month:'short',day:'numeric',year:'numeric'});}
  setInterval(updateClock,1000);updateClock();
  setInterval(()=>{if(!el.app.hidden)loadData();},60000);
  checkSession();
})();
