/* Metric browsing, version maintenance and offline forms for the shared library. */
window.createMetricLibrary = function({state,api,e,json,btn,label,modal,close,toast,refresh,newSession,update,importProgress,activityText,formatTime,formatDateTime}) {
  const $=id=>document.getElementById(id);
  let current=null, manualId=null;
  let metrics=null,metricsKey='',pendingKey='',metricsError='',requestEpoch=0,metricPage=0;
  const pageSize=25,announcedJobs=new Set();
  const selectedMetrics=new Map();
  let deleteSelection=[];
  const metricKey=m=>JSON.stringify([m.definitionId,m.id]);
  const availableMetrics=()=>metrics?.items||[];
  function bulkActions(){
    $('libraryBulkActions').innerHTML=`<span aria-live="polite">已选 ${selectedMetrics.size} 项${selectedMetrics.size?'（支持跨页选择）':''}</span>${btn('libBulkDelete','批量删除指标','',!selectedMetrics.size?'disabled':'')}${selectedMetrics.size?btn('libClearSelection','取消选择'):''}`;
  }
  const active=id=>[...(state.boot?.processing||[]),...(state.boot?.libraryActivities?.jobs||[])].some(j=>j.scope===id&&['QUEUED','RUNNING'].includes(j.status));
  const versionStatus=d=>d.status==='PUBLISHED'?'已发布':d.status==='DRAFT'?'草稿':label(d.status);
  const choices=(items,value)=>items.map(([id,name])=>`<option value="${e(id)}" ${id===value?'selected':''}>${e(name)}</option>`).join('');
  const running=job=>['QUEUED','RUNNING'].includes(job.status);
  const phaseNames={STARTED:'准备说明原文',CATALOG:'已整理待填位置',EXTRACTING:'在线识别指标与统计口径',BATCH_DONE:'当前批次已保存',RESUMED:'已恢复上次成功批次',SPLITTING:'请求超时或截断，正在缩小批次',REPAIRING:'正在纠正当前批次的取数意图',VALIDATING:'校验指标规则、原文证据与全文覆盖',READY:'解析结果已保存'};
  const progressEvents=job=>(state.boot?.libraryActivities?.events||[]).filter(item=>item.data?.jobId===job.id);
  function progressText(data){
    let text=phaseNames[data.phase]||label(data.phase);
    if(Number.isFinite(data.completed)&&Number.isFinite(data.total))text+=` · ${data.completed}/${data.total} ${data.unit==='metrics'?'项指标':data.unit==='all_slots'?'个待填位置':'个数字待填位置'}`;
    if(data.phase==='CATALOG')text+=` · 全部待填位置 ${data.slots} 处（含日期与文字）`;
    if(data.batchSize)text+=` · 本批 ${data.batchSize} 项`;
    if(data.batchLocators?.length)text+=` · 原文位置 ${data.batchLocators.join('、')}`;
    if(data.names?.length)text+=` · ${data.names.join('、')}`;
    if(data.errorCode)text+=` · ${data.errorCode}`;
    if(data.message)text+=` · ${data.message}`;
    return text;
  }
  function failure(job){
    if(!['FAILED','CANCELLED'].includes(job.status))return '';
    const draft=state.boot.definitions.find(d=>d.id===job.scope&&d.status==='DRAFT');
    return `<div class="job-failure"><strong>${job.status==='CANCELLED'?'处理已停止':'处理未完成'}</strong><p>${e(job.error?.message||'请检查处理记录后重试')}</p><small>${e(job.error?.code||job.status)} · 已保存的原文与成功批次仍保留</small>${draft?btn('libRetry','重试解析',job.scope):''}</div>`;
  }
  function extraction(job){
    const events=progressEvents(job).filter(item=>item.type==='definition.progress'),data=events.at(-1)?.data||{};
    const active=running(job),failed=['FAILED','CANCELLED'].includes(job.status),done=job.status==='SUCCEEDED';
    const stage=job.status==='QUEUED'?'已排队，等待开始':done?'解析完成，待复核发布':failed?'解析未完成':progressText(data)||'正在准备说明原文';
    const hasCount=Number.isFinite(data.total)&&data.total>0&&Number.isFinite(data.completed);
    const percent=hasCount?Math.min(100,Math.max(0,Math.floor(100*data.completed/data.total))):null;
    const steps=['读取说明','整理原文','提取指标口径','校验证据与覆盖','保存草稿'];
    const phaseIndex=done?5:job.status==='QUEUED'?1:({STARTED:1,CATALOG:2,EXTRACTING:2,BATCH_DONE:2,RESUMED:2,SPLITTING:2,REPAIRING:2,VALIDATING:3,READY:4}[data.phase]??1);
    const start=events.find(item=>item.data.phase==='STARTED')?.created;
    const time=start||job.created;
    const duration=`<span data-library-started="${e(time)}" ${active?'':`data-library-ended="${e(job.updated)}"`}></span>`;
    const counter=done?`已保存 ${job.metricCount} 项${data.numericCount!==undefined?`，其中数字待填位置 ${data.numericCount} 处`:''}${data.clearCount!==undefined?` · 可计算 ${data.clearCount} 项 · 待补充 ${data.skippedCount||0} 项`:''}`:hasCount?`已完成 ${data.completed}/${data.total} ${data.unit==='metrics'?'项指标':'个数字待填位置'}`:'模型返回后更新已识别项数';
    const latest=events.at(-1);
    const timeline=items=>items.map(item=>`<li class="activity-item ${item.data.phase==='READY'||['BATCH_DONE','RESUMED'].includes(item.data.phase)?'done':''}"><span class="activity-dot"></span><span>${e(progressText(item.data))}</span><time>${e(formatTime(item.created))}</time></li>`).join('');
    return `<div class="library-progress-body"><ol class="library-progress-stages">${steps.map((step,i)=>`<li class="${i<phaseIndex?'done':i===phaseIndex?(failed?'failed':'current'):''}">${step}</li>`).join('')}</ol><p class="library-current-stage">${e(stage)}</p><div class="library-progress-facts"><span>原文 ${job.nodeCount} 个节点</span><span>${e(counter)}</span><span>${job.status==='QUEUED'?'已等待':start?'耗时':'自排队起'} ${duration}</span></div>${hasCount||done?`<div class="import-progress-track" role="progressbar" aria-label="${done?'解析完成':data.unit==='metrics'?'指标校验进度':'数字待填位置识别进度'}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${done?100:percent}"><span style="width:${done?100:percent}%"></span></div>`:''}${active&&['EXTRACTING','REPAIRING'].includes(data.phase)?'<p class="muted library-model-wait">正在等待模型返回本批结果；返回并校验后更新数量。</p>':''}${latest?`<small class="muted">最近进展：${e(formatDateTime(latest.created))}</small>`:''}${events.length?`<ul class="activity-timeline">${timeline(events.slice(-6))}</ul>${events.length>6?`<details class="activity-detail" data-library-log="${e(job.id)}"><summary>查看本次全部处理记录（最近 ${events.length} 条）</summary><ul class="activity-timeline">${timeline(events)}</ul></details>`:''}`:''}${failure(job)}${done?btn('libShow','查看与复核解析结果',job.scope):''}</div>`;
  }
  function tick(){
    document.querySelectorAll('[data-library-started]').forEach(el=>{
      const start=Date.parse(el.dataset.libraryStarted),end=el.dataset.libraryEnded?Date.parse(el.dataset.libraryEnded):Date.now();
      const seconds=Math.max(0,Math.floor((end-start)/1000));
      el.textContent=Number.isFinite(seconds)?(seconds<60?`${seconds} 秒`:`${Math.floor(seconds/60)} 分 ${seconds%60} 秒`):'—';
    });
  }
  function renderProgress(){
    const activities=state.boot?.libraryActivities||{jobs:[],events:[]};
    const seen=new Set();
    const jobs=[...activities.jobs].sort((a,b)=>String(b.created).localeCompare(String(a.created))).filter(job=>{if(job.kind!=='extract')return true;if(seen.has(job.scope))return false;seen.add(job.scope);return true;}).sort((a,b)=>Number(running(b))-Number(running(a)));
    const activeJobs=jobs.filter(running);
    $('libraryProgressCount').textContent=activeJobs.length?`${activeJobs.length} 项处理中`:`${jobs.length} 条最近记录`;
    if(activeJobs.some(job=>!announcedJobs.has(job.id)))$('libraryProgressPanel').open=true;
    activeJobs.forEach(job=>announcedJobs.add(job.id));
    $('libraryProgressPanel').hidden=!jobs.length;
    const opened=new Set([...document.querySelectorAll('[data-library-log][open]')].map(el=>el.dataset.libraryLog));
    $('libraryStatus').innerHTML=jobs.length?`<div class="library-progress-title"><strong>导入与解析进度</strong><small>自动更新 · 展示进行中与最近处理记录</small></div>${jobs.map(job=>`<section class="library-progress-card ${running(job)?'active':''} ${['FAILED','CANCELLED'].includes(job.status)?'failed':''}" data-library-job="${e(job.id)}"><div class="activity-head"><strong>${job.kind==='import'?'文件导入':e(job.name)}</strong><span class="pill ${running(job)?'purple':job.status==='SUCCEEDED'?'green':'warm'}">${label(job.status)}</span><small>${e(formatDateTime(job.created))}</small></div>${job.kind==='import'?importProgress(job,activities.events)+failure(job)+(job.result?.failures||[]).map(f=>`<div class="job-failure"><strong>${e(f.name)}</strong><p>${e(f.message)}</p><small>${e(f.code)}</small></div>`).join(''):extraction(job)}</section>`).join('')}`:'';
    const dialog=document.querySelector('[data-library-definition-progress]');
    if(dialog){const job=jobs.find(j=>j.kind==='extract'&&j.scope===dialog.dataset.libraryDefinitionProgress);dialog.innerHTML=job?extraction(job):'';}
    document.querySelectorAll('[data-library-log]').forEach(el=>{el.open=opened.has(el.dataset.libraryLog);});
    tick();
  }
  const metricStatus=m=>m.status==='CLEAR'?'可计算':m.status==='REPORT'?'分析时核验':'待补充';
  function syncSelectAll(){
    const boxes=[...document.querySelectorAll('#libraryMetrics [data-action="libSelectMetric"]:not(:disabled)')],all=document.querySelector('[data-action="libSelectPage"]');
    if(all){const count=boxes.filter(b=>b.checked).length;all.checked=!!boxes.length&&count===boxes.length;all.indeterminate=count>0&&count<boxes.length;all.disabled=!boxes.length;}
  }
  function confirmMetricDelete(items){
    if(!items.length)return;
    deleteSelection=items;
    modal(`删除 ${items.length} 项指标`,`<p>仅删除所选指标。已发布版本会自动生成新草稿，复核发布后用于新分析，历史版本与结果保留。原文中的对应位置将标记为“指标已删除，不参与计算”。</p><ul class="metric-delete-list">${items.map(m=>`<li>${e(m.name)} <small>${e(m.versionLabel)}</small></li>`).join('')}</ul><label>维护人<input id="libDeleteMetricsActor" value="本机使用者" maxlength="200"></label><label>请输入“确认删除”<input id="libDeleteMetricsText" aria-label="确认删除所选指标" autocomplete="off"></label>`,btn('libDeleteMetricsConfirm','确认删除所选指标')+btn('libCancelMetricDelete','取消'));
  }
  function editMetric(id){
    manualId=id;
    const c=current.candidates.find(c=>c.id===id),p=c.plan||{},note=current.metricNotes?.[id];
    const row=availableMetrics().find(m=>m.id===id&&m.definitionId===current.id);
    const input=(key,title,value='',extra='')=>`<label>${title}<input id="metricEdit${key}" value="${e(value)}" ${extra}></label>`;
    const select=(key,title,items,value)=>`<label>${title}<select id="metricEdit${key}">${choices(items,value)}</select></label>`;
    const plan=current.reportPlan?`<div class="manual-grid">${select('Method','取数方式',[['SURGERY_COUNT','总手术量计数'],['EMERGENCY_COUNT','急诊手术量计数'],['DURATION_PATIENTS','时差条件人数'],['READ_SUMMARY','读取已有汇总'],['UNRESOLVED','待补充']],p.method)}${select('Room','手术室维度',[['','全部 / 未限定'],['第一手术室','第一手术室'],['第二手术室','第二手术室'],['第三手术室','第三手术室']],p.room)}${select('Start','时差起点',[['','不适用'],['entry','入室'],['anesthesia','麻醉']],p.start)}${select('End','时差终点',[['','不适用'],['anesthesia','麻醉'],['incision','开刀']],p.end)}${input('Minutes','严格超过分钟数',p.minutes,'type="number" min="0" max="1440"')}</div><p class="muted">取数方式须符合原文；非时差指标起止时点为空、分钟数为 0。修改原文要求请更新 Word 后重新上传。</p>`:`${select('Status','执行状态',[['CLEAR','可计算'],['SKIP','待补充 / 跳过']],c.status)}<details><summary>高级：计算规则</summary><p class="muted">保留真实原文证据，保存时校验完整规则。普通名称与说明修改无需调整规则。</p><textarea id="metricEditRule" class="code-editor">${json(c.rule||{})}</textarea></details>`;
    modal('编辑指标',`<p>${e(current.versionLabel)} · ${e(current.name)}${current.status==='PUBLISHED'?' · 保存时自动创建新草稿，历史版本保留':''}</p>${input('Name','指标名称',c.name,'maxlength="200"')}<label>维护说明<textarea id="metricEditDescription" maxlength="10000">${e(note?.description||row?.description||current.nodes.filter(n=>c.locators.includes(n.locator)).map(n=>n.text).join('\n'))}</textarea></label>${plan}<label>待补充 / 跳过原因<textarea id="metricEditReason" maxlength="300">${e(p.reason||c.reason||'')}</textarea></label>${input('Actor','维护人',note?.actor||'本机使用者','maxlength="200"')}`,btn('libSaveMetric','校验并保存指标')+btn('libManualCancel','取消'));
    if(current.reportPlan)$('metricEditMethod').onchange=()=>{if($('metricEditMethod').value!=='DURATION_PATIENTS'){['Start','End'].forEach(k=>$('metricEdit'+k).value='');$('metricEditMinutes').value=0;}};
  }
  function renderMetrics(){
    bulkActions();
    const loading=!!pendingKey;
    $('libraryMetrics').setAttribute('aria-busy',String(loading));
    if(!metrics){
      $('libraryMetricStats').innerHTML='';$('librarySelection').innerHTML='';$('libraryMetricPagination').innerHTML='';$('libraryMetricCount').textContent='';
      $('libraryMetrics').innerHTML=`<div class="notice-box">${metricsError?e(metricsError)+btn('libReloadMetrics','重新加载'):loading?'正在读取指标列表…':'暂无指标，可导入指标说明或手工新增。'}</div>`;
      return;
    }
    const query=$('librarySearch').value.trim().toLowerCase();
    const rows=metrics.items.filter(m=>[m.name,m.description,m.method,m.unit,m.reason,m.definitionName,m.versionLabel,metricStatus(m)].join(' ').toLowerCase().includes(query));
    metricPage=Math.min(metricPage,Math.max(0,Math.ceil(rows.length/pageSize)-1));
    const start=metricPage*pageSize,visible=rows.slice(start,start+pageSize),missing=rows.filter(m=>m.status==='SKIP').length;
    $('libraryMetricStats').innerHTML=[['指标总数',rows.length,'当前版本与搜索条件下'],['已明确口径',rows.length-missing,'可计算或已登记取数方案'],['待补充',missing,'需要进一步明确口径']].map(([title,count,note])=>`<div class="library-stat"><span>${title}</span><strong>${count}</strong><small>${note}</small></div>`).join('');
    $('libraryMetricCount').textContent=`共 ${rows.length} 项${query?` · 当前版本共 ${metrics.items.length} 项`:''}`;
    const selected=metrics.versions.length===1?metrics.versions[0]:null;
    $('librarySelection').innerHTML=selected?`<div class="library-selected-version"><span><strong>${e(selected.versionLabel||'v1')}</strong> ${e(selected.name)} <small>${versionStatus(selected)}${selected.disabled?' · 已停用':''}${active(selected.id)?' · 正在解析':''}</small></span>${btn('libShow','维护版本',selected.id)}</div>`:metrics.versions.length?`<p>已汇总 ${metrics.versions.length} 个指标集的最新版本（含草稿），各项所属版本见下表。</p>`:'';
    const empty=metrics.versions.some(d=>active(d.id))?'指标正在解析，完成后自动更新列表。可在下方查看详细进度。':query?'没有匹配的指标，请调整搜索条件。':metrics.versions.length?'此版本暂无已保存的指标，可维护版本或导入说明后解析。':'暂无指标，可导入指标说明或手工新增。';
    $('libraryMetrics').innerHTML=visible.length?`<div class="table-scroll library-metric-table"><table class="real-table"><thead><tr><th><input type="checkbox" data-action="libSelectPage" aria-label="全选本页指标"> 序号</th><th>指标名称 / 统计口径</th><th>统计方法</th><th>单位</th><th>所属版本</th><th>状态</th><th>操作</th></tr></thead><tbody>${visible.map((m,i)=>`<tr data-metric-id="${e(m.id)}" data-definition-id="${e(m.definitionId)}"><td class="metric-number"><input type="checkbox" data-action="libSelectMetric" data-id="${e(m.id)}" data-definition="${e(m.definitionId)}" aria-label="选择指标：${e(m.name)}" ${selectedMetrics.has(metricKey(m))?'checked':''} ${active(m.definitionId)?'disabled':''}> ${start+i+1}</td><td class="metric-name-cell"><strong>${e(m.name)}</strong>${m.kind&&m.kind!=='NUMBER'?` <small class="muted">${m.kind==='DATE'?'日期待填项':'文字待填项'}</small>`:''}<p class="metric-description" title="${e(m.description||m.reason)}">${e(m.description||m.reason||'暂无口径说明')}</p></td><td>${e(m.method)}</td><td>${e(m.unit)}</td><td><strong>${e(m.versionLabel)}</strong><small class="metric-version-name">${e(m.definitionName)}</small></td><td><span class="pill ${m.status==='SKIP'?'warm':'green'}">${metricStatus(m)}</span>${m.definitionStatus==='DRAFT'?'<small class="metric-version-name">草稿</small>':''}${m.disabled?'<small class="metric-version-name">版本已停用</small>':''}</td><td>${btn('libMetricShow','查看指标',m.id,`data-definition="${e(m.definitionId)}"`)}</td></tr>`).join('')}</tbody></table></div>`:`<div class="notice-box">${empty}</div>`;
    syncSelectAll();
    $('libraryMetricPagination').innerHTML=rows.length?`<small>第 ${start+1}–${start+visible.length} 项，共 ${rows.length} 项</small><div>${btn('libMetricPrevious','上一页','',metricPage===0?'disabled':'')}<span>${metricPage+1} / ${Math.ceil(rows.length/pageSize)}</span>${btn('libMetricNext','下一页','',start+pageSize>=rows.length?'disabled':'')}</div>`:'';
  }
  function loadMetrics(){
    const version=$('libraryVersionFilter').value,key=JSON.stringify([version,(state.boot?.definitions||[]).map(d=>[d.id,d.revision])]);
    if(key===metricsKey||key===pendingKey)return;
    const epoch=++requestEpoch;
    pendingKey=key;metricsKey='';metrics=null;metricsError='';selectedMetrics.clear();renderMetrics();
    api('library.metrics',{version}).then(result=>{
      if(epoch!==requestEpoch)return;
      metrics=result;metricsKey=key;pendingKey='';renderMetrics();
    }).catch(error=>{
      if(epoch!==requestEpoch)return;
      metricsKey=key;pendingKey='';metricsError=error.message;renderMetrics();
    });
  }
  function filter(){metricPage=0;render();}
  function render(){
    const definitions=state.boot?.definitions||[],selection=$('libraryVersionFilter').value;
    $('libraryVersionFilter').innerHTML='<option value="latest">最新版本（每个指标集）</option>'+definitions.map(d=>`<option value="${e(d.id)}">${e(d.versionLabel||'v1')} · ${e(d.name)} · ${versionStatus(d)}${d.disabled?' · 已停用':''}</option>`).join('');
    $('libraryVersionFilter').value=definitions.some(d=>d.id===selection)?selection:'latest';
    $('libraryVersionCount').textContent=`${definitions.length} 个版本`;
    $('versions').innerHTML=definitions.map(d=>`<article class="version-item"><span class="version-num">${e(d.versionLabel||'v1')}</span><div class="version-main"><strong>${e(d.name)}</strong><div class="version-meta">${versionStatus(d)}${d.disabled?' · 已停用':''}${d.default?' · 默认版本':''} · ${d.metricCount} 项指标 · ${d.sourceKind==='MANUAL'?'手工维护':'说明文档'}${active(d.id)?' · 正在解析':''}</div></div><div class="version-ops">${btn('libShow','查看与维护',d.id)}${d.status==='PUBLISHED'&&!d.disabled?btn('libNewSession','新建对话使用',d.id):''}</div></article>`).join('')||'<div class="notice-box">暂无匹配的指标集。可导入说明文档，或点击“手工新增指标”。</div>';
    renderProgress();
    $('libraryPending').innerHTML=(state.boot?.libraryAttachments||[]).map(a=>`<div class="version-item"><div class="version-main"><strong>${e(a.name)}</strong><p>${label(a.status)} · ${label(a.route.role)} · 请确认用途或拆分混合文件</p></div>${btn('libRoute','确认用途',a.id)}</div>`).join('');
    renderMetrics();loadMetrics();
  }
  async function showMetric(definitionId,metricId){
    current=await api('definition.get',{definition_id:definitionId});
    const candidate=current.candidates.find(c=>c.id===metricId);
    if(!candidate){toast('指标已变化，请刷新后查看');return refresh(true);}
    const row=metrics?.items.find(m=>m.definitionId===definitionId&&m.id===metricId),manual=current.manualMetrics?.[metricId],locators=new Set(candidate.locators||[]);
    const sources=[...current.nodes,...current.amendments].filter(n=>locators.has(n.locator));
    const description=manual?.description||current.metricNotes?.[metricId]?.description||sources.map(n=>n.text).join('\n');
    modal(candidate.name,`<div class="notice-box">${e(current.versionLabel)} · ${versionStatus(current)}${current.disabled?' · 已停用':''} · ${e(current.name)}</div><dl class="metric-detail-facts"><div><dt>统计方法</dt><dd>${e(row?.method||'详见取数规则')}</dd></div><div><dt>单位</dt><dd>${e(row?.unit||'详见原文')}</dd></div><div><dt>状态</dt><dd>${e(row?metricStatus(row):candidate.status==='CLEAR'?'可计算':candidate.status==='REPORT'?'分析时核验':'待补充')}</dd></div></dl><h3>统计口径</h3><p class="metric-detail-description">${e(description||'暂无口径说明')}</p>${candidate.reason||candidate.plan?.reason?`<div class="notice-box">待补充：${e(candidate.reason||candidate.plan.reason)}</div>`:''}<h3>原文与依据</h3>${sources.map(n=>`<div class="source-node"><p>${e(n.text)}</p><small>${e(n.locator)}</small></div>`).join('')||'<p class="muted">暂无原文依据</p>'}<details><summary>完整取数规则与证据</summary><pre>${json(candidate.rule||candidate.plan||candidate)}</pre></details>`,btn('libEditMetric','编辑指标',metricId,active(current.id)?'disabled':'')+btn('libMetricDelete','删除指标',metricId,active(current.id)?'disabled':'')+btn('libShow','维护所属版本',definitionId));
  }
  async function show(id){
    current=await api('definition.get',{definition_id:id});const d=current,draft=d.status==='DRAFT',busy=active(id);
    const cards=d.candidates.filter(c=>!d.metricExclusions?.[c.id]).map(c=>`<div class="result-metric"><strong>${e(c.name)}</strong><p>${c.status==='CLEAR'?'可计算':c.status==='REPORT'?'待填位置':e(c.reason||'跳过')}</p>${btn('libEditMetric','编辑指标',c.id,busy?'disabled':'')}${btn('libMetricDelete','删除指标',c.id,busy?'disabled':'')}<details><summary>规则与证据</summary><pre>${json(c.rule||c.plan||c)}</pre></details></div>`).join('');
    const advanced=draft&&!d.reportPlan?`<details><summary>高级：结构化规则与全文覆盖</summary><p>可维护文档提取结果。更改口径须先添加人工补充说明，再引用真实证据；表单新增指标无需编辑 JSON。</p><label>复核政策<select id="libPolicy">${choices([['DETERMINISTIC_APPROVED','人工批准确定性规则'],['AI_REQUIRED','每次分析需在线复核']],d.reviewPolicy)}</select></label><label>候选指标<textarea id="libCandidates" class="code-editor">${json(d.candidates)}</textarea></label><label>全文处理清单<textarea id="libCoverage" class="code-editor">${json(d.coverage)}</textarea></label>${btn('libSaveRules','校验并保存规则',id,busy?'disabled':'')}</details>`:'';
    modal(d.name,`<div class="notice-box">${e(d.versionLabel)} · ${versionStatus(d)}${d.disabled?' · 已停用':''}${busy?' · 正在解析，请等待后维护':''}</div><div data-library-definition-progress="${e(id)}"></div>${draft?`<label>版本标签<input id="libVersion" maxlength="40" value="${e(d.versionLabel)}"></label><label>版本说明<textarea id="libNotes" maxlength="2000">${e(d.notes||'')}</textarea></label>${btn('libMetadata','保存版本说明',id,busy?'disabled':'')}`:`<p>${e(d.notes||'')}</p>`}<h3>指标（${d.candidates.filter(c=>!d.metricExclusions?.[c.id]).length} 项）</h3>${Object.keys(d.metricExclusions||{}).length?'<p class="muted">已删除指标的原文与规则保留用于追溯，发布后不参与计算。</p>':''}${cards||'<p>尚无指标，可手工新增或从说明中提取。</p>'}${advanced}<details><summary>原文与人工补充</summary>${d.nodes.map(n=>`<div class="source-node"><code>${e(n.locator)}</code><p>${e(n.text)}</p></div>`).join('')}<pre>${json(d.amendments)}</pre></details>`,
      draft?(d.reportPlan?'':btn('libAddMetric','＋ 新增指标',id,busy?'disabled':''))+(d.sourceKind==='MANUAL'?'':btn('libExtract','在线提取',id,busy?'disabled':''))+btn('libAmend','人工补充',id,busy?'disabled':'')+btn('libPublish','复核并发布',id,busy?'disabled':'')+btn('libDelete','删除指标说明',id,busy?'disabled':''):
      btn('libClone','创建新版本',id)+btn('libCompare','比较版本',id)+(d.disabled?'':btn('libNewSession','新建对话使用',id)+(state.id&&state.detail?.session.state==='DRAFT'?btn('libBind','用于当前对话',id):'')+btn('libDefault','设为默认版本',id)+btn('libDisable','停用',id))+btn('libDelete','删除指标说明',id,busy?'disabled':''));
    renderProgress();
  }
  function form(d=null,id=null){
    current=d;manualId=id;
    const s=d?.manualMetrics?.[id]||{domain:'surgery',entity:'surgery_event',method:'count_distinct',dateField:'operation_date',keyField:'surgery_event_id',unit:'case',scale:0,actor:'本机使用者'};
    const input=(key,title,value='',extra='')=>`<label>${title}<input id="manual_${key}" value="${e(value)}" ${extra}></label>`;
    const select=(key,title,items,value)=>`<label>${title}<select id="manual_${key}">${choices(items,value)}</select></label>`;
    modal(id?'编辑手工指标':'手工新增指标',`${d?`<p>指标集：${e(d.name)} · ${e(d.versionLabel)}${d.status==='PUBLISHED'?' · 保存时自动创建新草稿，历史版本保留':''}</p>`:input('collection','指标集名称',s.name||'','maxlength="120" placeholder="例如：手术室常用指标"')}${input('name','指标名称',s.name||'','maxlength="200"')}<label>统计口径<textarea id="manual_description" maxlength="10000" placeholder="写明统计对象、统计范围和业务含义">${e(s.description||'')}</textarea></label><div class="manual-grid">${select('domain','领域',[['surgery','手术'],['hospital_ops','医院运营']],s.domain)}${select('entity','统计对象',[['surgery_event','手术事件'],['department_day','科室每日记录'],['discharge_event','出院事件'],['revenue_event','收入事件']],s.entity)}${select('method','统计方法',[['count_distinct','按唯一编号去重计数'],['count','记录数'],['sum','求和'],['mean','平均值'],['min','最小值'],['max','最大值'],['ratio','符合条件记录占比']],s.method)}${select('unit','结果单位',[['case','台次'],['person','人'],['item','项'],['minute','分钟'],['day','天'],['bed_day','床日'],['currency','金额'],['percent','百分比'],['ratio','比例']],s.unit)}${input('dateField','日期字段标识',s.dateField)}<div id="manualKey">${input('keyField','唯一编号字段标识',s.keyField||'')}</div><div id="manualValue">${input('valueField','数值字段标识',s.valueField||'')}</div>${input('scale','小数位数',s.scale,'type="number" min="0" max="8"')}</div><p class="muted">字段标识使用英文字母、数字和下划线。实际 Excel 中文列名可在“字段映射”中选择。日期列采用 ISO 日期（如 2026-10-01）。</p><fieldset><legend id="manualConditionTitle">筛选条件（可选）</legend><div class="manual-grid">${input('conditionField','筛选字段标识',s.conditionField||'')}${select('conditionType','筛选值类型',[['string','文本'],['decimal','数值']],s.conditionType||'string')}${select('conditionOperator','比较方式',[['eq','等于'],['ne','不等于'],['gt','大于'],['gte','大于等于'],['lt','小于'],['lte','小于等于']],s.conditionOperator||'eq')}${input('conditionValue','筛选值',s.conditionValue||'')}</div></fieldset><p class="notice-box">按所选日期字段统计，时区为上海，日期范围左闭右开；“当前全部数据”覆盖所有日期。缺失或非法值导致该指标跳过；计数空集为 0，数值聚合空集跳过，比例分母为 0 时跳过。比例的分母为全部记录，分子由筛选条件确定。</p>${input('actor','维护人',s.actor,'maxlength="200"')}`,btn('libSaveManual','校验并保存')+btn('libManualCancel','取消'));
    $('manual_method').onchange=()=>{const m=$('manual_method').value;$('manualKey').hidden=m!=='count_distinct';$('manualValue').hidden=!['sum','mean','min','max'].includes(m);$('manualConditionTitle').textContent=m==='ratio'?'分子筛选条件（必填）':'筛选条件（可选）';if(m==='ratio'&&!['percent','ratio'].includes($('manual_unit').value)){$('manual_unit').value='percent';$('manual_scale').value=2;}else if(['count','count_distinct'].includes(m)&&!['case','person','item'].includes($('manual_unit').value)){$('manual_unit').value='case';$('manual_scale').value=0;}};
    $('manual_method').onchange();
    $('manual_domain').onchange=()=>{$('manual_entity').value=$('manual_domain').value==='surgery'?'surgery_event':'department_day';$('manual_entity').onchange();};
    $('manual_entity').onchange=()=>{const defaults={surgery_event:['surgery','operation_date','surgery_event_id','case'],department_day:['hospital_ops','stat_date','department','item'],discharge_event:['hospital_ops','discharge_date','encounter_id','person'],revenue_event:['hospital_ops','revenue_date','revenue_id','item']}[$('manual_entity').value];['domain','dateField','keyField','unit'].forEach((k,i)=>$('manual_'+k).value=defaults[i]);};
  }
  async function changed(id){
    const previous=current?.id,follow=previous&&previous!==id&&$('libraryVersionFilter').value===previous;
    await refresh(true);
    if(follow){$('libraryVersionFilter').value=id;filter();}
    return show(id);
  }
  async function handle(action,id,el){
    if(action==='libMetricShow')return showMetric(el.dataset.definition,id);
    if(action==='libMetricPrevious'||action==='libMetricNext'){metricPage=Math.max(0,metricPage+(action==='libMetricNext'?1:-1));return renderMetrics();}
    if(action==='libReloadMetrics'){metricsKey='';return loadMetrics();}
    if(action==='libSelectMetric'){
      const m=availableMetrics().find(m=>m.id===id&&m.definitionId===el.dataset.definition);
      if(m){if(el.checked)selectedMetrics.set(metricKey(m),m);else selectedMetrics.delete(metricKey(m));}
      bulkActions();syncSelectAll();return;
    }
    if(action==='libSelectPage'){
      document.querySelectorAll('#libraryMetrics [data-action="libSelectMetric"]').forEach(box=>{
        const m=availableMetrics().find(m=>m.id===box.dataset.id&&m.definitionId===box.dataset.definition);
        if(m&&!box.disabled){box.checked=el.checked;if(el.checked)selectedMetrics.set(metricKey(m),m);else selectedMetrics.delete(metricKey(m));}
      });bulkActions();syncSelectAll();return;
    }
    if(action==='libClearSelection'){selectedMetrics.clear();renderMetrics();return;}
    if(action==='libCancelMetricDelete'){close();return;}
    if(action==='libBulkDelete')return confirmMetricDelete([...selectedMetrics.values()]);
    if(action==='libMetricDelete')return confirmMetricDelete([{id,definitionId:current.id,definitionRevision:current.revision,name:current.candidates.find(c=>c.id===id).name,versionLabel:current.versionLabel}]);
    if(action==='libDeleteMetricsConfirm'){
      const result=await api('definition.metrics.delete',{selections:deleteSelection.map(m=>({definition_id:m.definitionId,revision:m.definitionRevision,metric_id:m.id})),confirmation:$('libDeleteMetricsText').value,actor:$('libDeleteMetricsActor').value});
      const old=$('libraryVersionFilter').value,target=result.versions.find(v=>v.sourceId===old);
      selectedMetrics.clear();close();await refresh(true);
      if(target){$('libraryVersionFilter').value=target.id;filter();}
      toast(`已删除 ${result.deletedCount} 项指标${result.versions.some(v=>v.id!==v.sourceId)?'，已创建新草稿，复核发布后用于新分析':''}`);return;
    }
    if(action==='libShow')return show(id);
    if(action==='libRetry'){await api('job.queue',{kind:'extract',scope:id,payload:{definitionId:id}});await refresh(true);toast('已重新排队，可在本页查看处理进度');return;}
    if(action==='libAddMetric')return form(current);
    if(action==='libEditMetric')return current.manualMetrics?.[id]?form(current,id):editMetric(id);
    if(action==='libSaveMetric'){
      const candidate=current.candidates.find(c=>c.id===manualId);
      const changes={name:$('metricEditName').value.trim(),description:$('metricEditDescription').value.trim(),actor:$('metricEditActor').value.trim()};
      if(current.reportPlan){changes.plan={slot:manualId,name:changes.name,method:$('metricEditMethod').value,room:$('metricEditRoom').value,start:$('metricEditStart').value,end:$('metricEditEnd').value,minutes:Number($('metricEditMinutes').value),reason:$('metricEditReason').value.trim()};}
      else{changes.status=$('metricEditStatus').value;changes.reason=$('metricEditReason').value.trim();if(changes.status==='CLEAR')changes.rule=JSON.parse($('metricEditRule').value);}
      const result=await api('definition.metric.edit',{definition_id:current.id,revision:current.revision,metric_id:candidate.id,changes});
      toast('指标修改已保存；复核发布后用于新分析');return changed(result.id);
    }
    if(action==='libManualCancel'){if(current)return show(current.id);close();return;}
    if(action==='libSaveManual'){
      const keys=['name','description','actor','domain','entity','method','dateField','keyField','valueField','unit','scale','conditionField','conditionType','conditionOperator','conditionValue'];
      const spec=Object.fromEntries(keys.map(k=>[k,k==='scale'?Number($('manual_'+k).value):$('manual_'+k).value.trim()]));
      const result=manualId?await api('definition.metric.edit',{definition_id:current.id,revision:current.revision,metric_id:manualId,changes:spec}):await api('definition.manual.save',{spec,...(current?{definition_id:current.id,revision:current.revision,metric_id:manualId}:{name:$('manual_collection').value.trim()})});
      toast('手工指标已保存，无需模型解析；复核发布后可用于分析');return changed(result.id);
    }
    if(action==='libNewSession'){close();await newSession(id);toast('已使用所选指标版本新建对话');return;}
    if(action==='libBind'){await update({definitionId:id});close();state.library=false;state.switchView();toast('已绑定指标版本，请核对字段映射');return;}
    if(action==='libClone'){const result=await api('definition.clone',{definition_id:id,revision:current.revision});return changed(result.id);}
    if(action==='libMetadata'){await api('definition.metadata',{definition_id:id,revision:current.revision,version_label:$('libVersion').value,notes:$('libNotes').value});return changed(id);}
    if(action==='libSaveRules'){await api('definition.save',{definition_id:id,revision:current.revision,candidates:JSON.parse($('libCandidates').value),coverage:JSON.parse($('libCoverage').value),policy:$('libPolicy').value});return changed(id);}
    if(action==='libExtract'&&current.candidates.length){modal('重新提取当前草稿','<p>重新提取将替换当前草稿的候选指标，包括手工维护的指标。原文与人工补充证据仍保留。需要保留当前规则时，请取消并先发布当前版本。</p>',btn('libExtractConfirm','确认重新提取',id)+btn('libShow','取消',id));return;}
    if(action==='libExtract'||action==='libExtractConfirm'){await api('job.queue',{kind:'extract',scope:id,payload:{definitionId:id}});await changed(id);toast('已加入提取队列');return;}
    if(action==='libAmend'){modal('人工补充口径','<label>补充条款<textarea id="libAmendText" maxlength="10000"></textarea></label><label>维护人<input id="libActor" value="本机使用者"></label><label>适用范围<input id="libAmendScope"></label>',btn('libSaveAmend','保存补充',id));return;}
    if(action==='libSaveAmend'){await api('definition.amend',{definition_id:id,revision:current.revision,text:$('libAmendText').value,actor:$('libActor').value,scope:$('libAmendScope').value,replaces:[]});return changed(id);}
    if(action==='libPublish'){modal('复核并发布指标版本','<p>请核对全部指标的口径、计算规则、原文与人工补充证据。发布后该版本将冻结。</p><label>复核人<input id="libActor" value="本机使用者"></label><label><input type="checkbox" id="libReviewed">我已核对全部指标及证据，确认发布</label>',btn('libPublishConfirm','发布',id));return;}
    if(action==='libPublishConfirm'){await api('definition.publish',{definition_id:id,revision:current.revision,actor:$('libActor').value,reviewed:$('libReviewed').checked});toast('指标版本已发布');return changed(id);}
    if(action==='libDefault'){await api('definition.default',{definition_id:id});return changed(id);}
    if(action==='libDisable'){modal('停用指标版本','<p>停用后不能新绑定，已有会话与历史结果仍保留。需要继续维护时请创建新版本。</p>',btn('libDisableConfirm','确认停用',id));return;}
    if(action==='libDisableConfirm'){await api('definition.disable',{definition_id:id,disabled:true});return changed(id);}
    if(action==='libDelete'){modal('删除指标说明 / 版本','<p>将从指标管理中移除此说明版本及其指标列表。若已有会话、运行或其他版本引用，保留原文与指标快照用于历史追溯。请输入“确认删除”。</p><input id="libDeleteText" aria-label="确认删除指标说明">',btn('libDeleteConfirm','删除指标说明',id));return;}
    if(action==='libDeleteConfirm'){const result=await api('definition.delete',{definition_id:id,revision:current.revision,confirmation:$('libDeleteText').value});close();await refresh(true);toast(result.retainedForHistory?'指标说明已删除，已引用的快照保留用于历史追溯':'指标说明已删除');return;}
    if(action==='libCompare'){modal('比较指标版本',`<label>比较版本<select id="libCompareVersion">${choices((state.boot.definitions||[]).filter(d=>d.id!==id).map(d=>[d.id,`${d.name} · ${d.versionLabel}`]),'')}</select></label>`,btn('libCompareShow','查看差异',id));return;}
    if(action==='libCompareShow'){const diff=await api('definition.compare',{left_id:id,right_id:$('libCompareVersion').value});modal('指标版本差异',`<pre>${json(diff)}</pre>`,btn('libShow','返回版本',id));return;}
    if(action==='libImports'){const jobs=await api('job.list');modal('指标管理导入记录',jobs.filter(j=>j.scope==='library').slice(0,10).map(j=>`<h3>${label(j.status)}</h3><pre>${json(j.error||j.result||{})}</pre>`).join(''));return;}
    if(action==='libRoute'){modal('确认库文件用途','<p>指标说明仅支持 Word；混合内容需要拆分后导入。</p><select id="libRouteRole"><option value="METRIC_DEFINITION">指标说明（Word）</option><option value="OTHER">其他 / 排除</option></select>',btn('libRouteSave','确认用途',id));return;}
    if(action==='libRouteSave'){await api('attachment.route',{attachment_id:id,role:$('libRouteRole').value});close();return refresh(true);}
  }
  return {render,filter,show,form,handle,tick};
};
