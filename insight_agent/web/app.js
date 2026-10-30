/* All business state is read from the local Python service. No demo authority. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const e = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const json = value => e(JSON.stringify(value, null, 2));
  const state = {boot:null, detail:null, id:null, library:false, archived:false, bridge:null, busy:false, refreshPending:false, refreshPendingForce:false, detailStamp:'', bootStamp:'', processingStamp:'', composing:false, draftTimer:null, offset:0, searchTimer:null, appliedPreferences:false, pasteRecovery:null, lastTextPaste:null, lastClearedAt:null};
  const status = {DRAFT:'准备中',LOCKED:'数据已锁定',PUBLISHED:'解析就绪',READY:'已就绪',NEEDS_CONFIRMATION:'待确认',QUEUED:'准备中',RUNNING:'处理中',SUCCEEDED:'已完成',SUCCEEDED_WITH_SKIPS:'部分完成',NO_ELIGIBLE_METRICS:'待补充资料',FAILED:'失败',CANCELLED:'已停止',PASS:'通过',SKIP:'跳过',VALUE:'已计算',PLANNED:'计划就绪',LAYOUT_UNVERIFIED:'版面未核验',RENDERED_REVIEW_REQUIRED:'待页面检查',PREPARING:'读取并校验数据',PLANNING:'生成执行计划',CALCULATING:'计算指标',AUDITING:'核对结果与证据',COMMITTING:'保存分析结果',COMPLETED:'处理完成',READING:'读取文件',PARSING:'解析文件',ROUTING:'识别文件类型'};
  Object.assign(status, {DATA_FILE:'数据文件',METRIC_DEFINITION:'指标说明',MIXED:'混合内容',UNKNOWN:'用途不明确',OTHER:'其他文件',EXCLUDED:'已排除',UNSUPPORTED_ROLE_FORMAT:'用途已识别，格式暂不支持',HIGH:'高',LOW:'低',CONFIRMED:'已确认',workbook:'Excel 工作簿',document:'Word 文档'});
  Object.assign(status,{metric_catalog:'指标说明解析'});
  const label = s => status[s] || s || '';
  const btn = (action, text, id='', extra='') => `<button class="soft-action" data-action="${action}" data-id="${e(id)}" ${extra}>${e(text)}</button>`;
  const library=window.createMetricLibrary({state,api,e,json,btn,label,modal,close,toast,refresh,newSession,update,importProgress,activityText,formatTime,formatDateTime});
  state.switchView=switchView;
  function formatTime(value){
    if(!value)return '';
    const date=new Date(value);
    if(Number.isNaN(date.getTime()))return String(value).slice(11,19);
    return date.toLocaleTimeString('zh-CN',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
  }
  function formatDateTime(value){
    if(!value)return '';
    const date=new Date(value);
    if(Number.isNaN(date.getTime()))return String(value);
    return `${date.toLocaleDateString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit'})} ${formatTime(value)}`;
  }
  function toast(message) { $('toast').textContent=message; $('toast').classList.add('show'); clearTimeout(toast.timer); toast.timer=setTimeout(()=>$('toast').classList.remove('show'),4500); }
  async function api(method,args={}) { if(!state.bridge) throw Error('请通过 Python 桌面应用打开工作台'); return new Promise((resolve,reject)=>state.bridge.request(method,JSON.stringify(args),raw=>{const value=JSON.parse(raw);value.ok?resolve(value.data):reject(Error(value.error.message+' ['+value.error.code+']'));})); }
  async function guarded(fn){try{return await fn();}catch(error){toast(error.message);return null;}}
  function modal(title,body,footer='',subtitle=''){ state.catalogDialog=null;$('dialogTitle').textContent=title;$('dialogSubtitle').textContent=subtitle;$('dialogBody').innerHTML=body;$('dialogFooter').innerHTML=footer;state.previousFocus=document.activeElement;$('overlay').classList.add('show');state.bridge?.allowFilePaste(false);setTimeout(()=>$('dialogBody').querySelector('input,textarea,select,button')?.focus(),0); }
  function close(){ state.catalogDialog=null;$('overlay').classList.remove('show');state.previousFocus?.focus(); }
  function table(headers, rows) {return `<div class="table-scroll"><table class="real-table"><thead><tr>${headers.map(h=>`<th>${e(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${e(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;}
  async function refresh(force=false){
    if(!state.bridge)return;
    if(state.busy){state.refreshPending=true;state.refreshPendingForce=state.refreshPendingForce||force;return;}
    state.busy=true;
    try{const boot=await api('bootstrap',{search:$('search').value.trim(),archived:state.archived,offset:state.offset});if(!state.appliedPreferences){$('app').classList.toggle('context-collapsed',boot.settings.contextCollapsed);state.appliedPreferences=true;}state.boot=boot; const stamp=JSON.stringify(boot);const processingStamp=JSON.stringify(boot.processing||[]);const processingChanged=processingStamp!==state.processingStamp;state.processingStamp=processingStamp;
      if(stamp!==state.bootStamp||force){state.bootStamp=stamp;renderHistory();library.render();$('readiness').textContent=boot.readiness.modelConfigured?'在线模型已配置':'请配置在线模型';}
      library.tick();
      if(state.id){const detail=await api('session.detail',{session_id:state.id});const stamp=JSON.stringify(detail);state.detail=detail;if(stamp!==state.detailStamp||force||processingChanged){state.detailStamp=stamp;renderSession();}}
      if(state.catalogDialog&&state.catalogDialog===state.id)await showMetricCatalog(false);
    }catch(err){toast(err.message);}finally{state.busy=false;if(state.refreshPending){const nextForce=state.refreshPendingForce;state.refreshPending=false;state.refreshPendingForce=false;setTimeout(()=>refresh(nextForce),0);}}
  }
  async function selectSession(id){await flushDraft();state.id=id;state.library=false;state.lastClearedAt=null;state.detailStamp='';state.bridge.setContext(id);switchView();await refresh(true);$('prompt').value=state.detail?.session.draft||'';}
  function switchView(){ $('chatView').hidden=state.library;$('chatView').style.display=state.library?'none':'flex';$('libraryView').hidden=!state.library;$('libraryView').style.display=state.library?'block':'none';$('app').classList.toggle('library-view',state.library);$('navChat').classList.toggle('active',!state.library);$('navLibrary').classList.toggle('active',state.library);$('crumb').textContent=state.library?'指标管理':'数据对话';state.bridge?.setContext(state.library?'library':state.id||''); }
  function renderHistory(){if(!state.boot)return;const sessions=state.boot.sessions,page=state.boot.sessionPage;$('histories').innerHTML=sessions.map(s=>`<button class="history-item ${s.id===state.id?'active':''}" data-action="session" data-id="${e(s.id)}"><span>◫</span><span class="history-name">${e(s.name)}</span><small>${label(s.state)}</small></button>`).join('')||'<p class="muted padded">没有匹配的对话</p>';$('histories').innerHTML+=`<div class="history-pages">${btn('pagePrevious','上一页','',page.offset===0?'disabled':'')}<small>${page.total?Math.floor(page.offset/page.limit)+1:0} / ${Math.ceil(page.total/page.limit)} · ${page.total} 条</small>${btn('pageNext','下一页','',page.offset+page.limit>=page.total?'disabled':'')}</div>`;$('showArchive').textContent=state.archived?'返回':'归档';}
  function routeEvidence(a){
    const route=a.route||{};
    const evidence=route.evidence||[];
    const sheets=[
      ...(route.dataSheets||[]).map(name=>`数据表：${name}`),
      ...(route.ruleSheets||[]).map(name=>`规则表：${name}`),
      ...(route.reportSheets||[]).map(name=>`报告表：${name}`)
    ];
    const evidenceRows=evidence.slice(0,8).map(item=>{
      if(item.sheet)return `${item.sheet} · 第 ${item.headerRow||'?'} 行表头：${(item.headers||[]).join('、')}`;
      return `${item.locator||'文档节点'} · 证据指纹 ${String(item.quoteSha256||'').slice(0,16)}`;
    });
    return `<div class="route-evidence"><div class="route-badges"><span class="pill">${label(route.role)}</span><span class="pill">${label(route.confidence)}置信度</span><span class="pill">${label(a.status)}</span></div><p><strong>判断：</strong>${e(route.reason||'未提供判断说明')}</p><p><strong>解析：</strong>${label(a.profile?.kind)} · ${e(a.extension||'')}</p>${sheets.length?`<p><strong>定位：</strong>${e(sheets.join('；'))}</p>`:''}<p><strong>证据：</strong>${evidence.length?`检测到 ${evidence.length} 项，展示前 ${Math.min(evidence.length,8)} 项`:'未检测到明确结构'}</p>${evidenceRows.length?`<ul>${evidenceRows.map(row=>`<li>${e(row)}</li>`).join('')}</ul>`:''}</div>`;
  }
  function showRouteInfo(a){
    modal('文件类型识别',`<p class="muted">文件：${e(a.name)}</p>${routeEvidence(a)}${a.status==='NEEDS_CONFIRMATION'?'<p class="notice-box">自动判断不够确定。请确认用途后再进行指标提取或分析。</p>':''}${a.status==='UNSUPPORTED_ROLE_FORMAT'?'<p class="notice-box">识别出的用途与当前格式不匹配，请另存为对应格式后重新导入。</p>':''}`,a.status==='NEEDS_CONFIRMATION'||a.status==='UNSUPPORTED_ROLE_FORMAT'?btn('route','确认用途',a.id):'');
  }
  function renderAttachment(a, session){
    const isDocument=a.profile.kind==='document';
    const canConfirm=['NEEDS_CONFIRMATION','UNSUPPORTED_ROLE_FORMAT'].includes(a.status)&&session.state==='DRAFT';
    return `<div class="file-row"><div class="file-icon ${isDocument?'word':''}">${isDocument?'W':'X'}</div><div class="file-text"><button class="file-name link-action" data-action="preview" data-id="${e(a.id)}">${e(a.name)}</button><div class="file-desc">识别为 ${label(a.route.role)} · ${label(a.route.confidence)}置信度 · ${label(a.status)}</div><div class="file-reason">${e(a.route.reason)}</div><div class="file-actions">${btn('routeInfo','查看依据',a.id)}${canConfirm?btn('route','确认用途',a.id):''}${session.state==='DRAFT'?btn('remove','移除',a.id):''}</div></div></div>`;
  }
  function renderAttachmentList(target, attachments, session, emptyText){
    $(target).innerHTML=attachments.length?attachments.map(a=>renderAttachment(a,session)).join(''):`<p class="muted file-empty">${e(emptyText)}</p>`;
  }
  function activityText(item){
    const data=item.data||{};
    if(item.type==='job.queued')return `已进入${data.kind==='import'?'文件导入':data.kind==='turn'?'对话分析':'后台处理'}队列`;
    if(item.type==='import.started')return `已收到 ${data.total||data.files?.length||0} 个文件，等待处理`;
    if(item.type==='job.terminal')return `处理任务${label(data.status)}`;
    if(item.type==='attachment.imported')return `文件已识别并纳入当前资料 · ${label(data.role)}`;
    if(item.type==='turn.accepted')return '已收到请求，正在理解目标并匹配当前资料';
    if(item.type==='turn.preparing')return '正在准备本次说明的指标规则，完成后自动继续处理请求';
    if(item.type==='turn.prepared')return '指标规则已就绪，正在继续本次分析';
    if(item.type==='run.created')return '已创建分析运行，准备冻结本次数据基线';
    if(item.type==='run.phase')return label(data.phase)||'正在处理';
    if(item.type==='run.item')return `取数与核验 ${data.completed}/${data.total} · ${data.name} · ${data.outcome==='VALUE'?'已取数':data.reason||'待补充'}`;
    if(item.type==='definition.progress'){
      const phase=({STARTED:'准备说明原文',CATALOG:'已整理待填位置',EXTRACTING:'识别指标与统计口径',BATCH_DONE:'当前批次已保存',RESUMED:'已恢复上次成功批次',REPAIRING:'正在纠正当前批次的取数意图',SPLITTING:'正在缩小解析批次',VALIDATING:'校验原文与指标规则',READY:'指标解析结果已保存'})[data.phase]||data.phase;
      const count=Number.isFinite(data.completed)&&Number.isFinite(data.total)?` · ${data.completed}/${data.total} ${data.unit==='metrics'?'项指标':data.unit==='all_slots'?'个待填位置':'个数字待填位置'}`:'';
      return `${phase}${count}${data.nodeCount!==undefined?` · 原文 ${data.nodeCount} 个节点`:''}${data.batchSize?` · 本批 ${data.batchSize} 项`:''}${data.names?.length?` · ${data.names.join('、')}`:''}`;
    }
    if(item.type==='metric_catalog.progress')return `${({CATALOG:'已整理指标目录',DETAILS:'补充指标口径',SPLITTING:'正在缩小解析批次',READY:'指标说明解析完成'})[data.phase]||data.phase} · ${data.completed}/${data.total}`;
    if(item.type==='run.committed')return `分析结果已提交 · ${label(data.status)}`;
    if(item.type==='definition.created')return '已读取指标说明，等待自动解析';
    if(['definition.published','session.metrics.ready'].includes(item.type))return '说明文档中的指标已解析就绪';
    if(item.type==='import.progress'){
      const prefix=data.total?`${data.index||1}/${data.total} · `:'';
      const name=data.name?`${data.name} · `:'';
      const extra=data.role?` · ${label(data.role)}`:data.code?` · ${data.code}`:'';
      return `${prefix}${name}${label(data.phase)||'处理文件'}${extra}`;
    }
    return item.type||'处理步骤';
  }
  function activityState(item){
    if(item.type==='job.terminal'&&['FAILED','CANCELLED'].includes(item.data?.status))return 'failed';
    if(item.type==='run.committed'||item.type==='attachment.imported'||['definition.published','session.metrics.ready','turn.prepared'].includes(item.type)||item.data?.phase==='COMPLETED')return 'done';
    return ['run.phase','import.progress','turn.preparing'].includes(item.type)?'current':'';
  }
  const importPhasePercent={READING:10,PARSING:45,ROUTING:75,COMPLETED:100,FAILED:100};
  function formatBytes(value){
    if(value===null||value===undefined||value==='')return '';
    const size=Number(value);if(!Number.isFinite(size)||size<0)return '';
    if(size<1024)return `${size} B`;
    if(size<1024*1024)return `${(size/1024).toFixed(1)} KB`;
    if(size<1024*1024*1024)return `${(size/1024/1024).toFixed(1)} MB`;
    return `${(size/1024/1024/1024).toFixed(1)} GB`;
  }
  function importRows(job, events){
    const started=events.find(item=>item.type==='import.started'&&item.data?.jobId===job.id);
    const rows=new Map((started?.data?.files||[]).map(file=>[Number(file.index),{index:Number(file.index),name:file.name||`文件 ${file.index}`,size:file.size,phase:'QUEUED'}]));
    events.filter(item=>item.type==='import.progress'&&item.data?.jobId===job.id).forEach(item=>{
      const data=item.data||{},index=Number(data.index);
      const row=rows.get(index)||{index,name:data.name||`文件 ${index}`};
      rows.set(index,{...row,...data,phase:data.phase||row.phase});
    });
    const result=job.result||{};
    (result.acceptedDetails||[]).forEach(item=>{
      const row=[...rows.values()].find(value=>value.name===item.name);
      if(row)Object.assign(row,item,{phase:'COMPLETED'});
    });
    (result.failures||[]).forEach(item=>{
      const row=[...rows.values()].find(value=>value.name===item.name);
      if(row)Object.assign(row,item,{phase:'FAILED'});
    });
    return [...rows.values()].sort((a,b)=>a.index-b.index);
  }
  function importProgress(job, events){
    const rows=importRows(job,events);
    if(!rows.length)return '';
    const completed=rows.filter(row=>['COMPLETED','FAILED'].includes(row.phase)).length;
    const failed=rows.filter(row=>row.phase==='FAILED').length;
    const percent=Math.round(rows.reduce((sum,row)=>sum+(importPhasePercent[row.phase]||0),0)/rows.length);
    const active=['QUEUED','RUNNING'].includes(job.status);
    const heading=active?'正在上传并识别文件':failed?`处理完成，${failed} 个文件失败`:'文件处理完成';
    const rowsHtml=rows.map(row=>{
      const state=row.phase==='FAILED'?'failed':row.phase==='COMPLETED'?'done':row.phase==='QUEUED'?'queued':'current';
      const details=row.phase==='FAILED'?(row.message||row.code||'处理失败'):row.role?`${label(row.role)}${row.confidence?` · ${label(row.confidence)}置信度`:''}`:label(row.phase);
      return `<div class="import-progress-file ${state}"><span class="import-file-state" aria-hidden="true"></span><div class="import-file-main"><strong title="${e(row.name)}">${e(row.name)}</strong><small>${e(details)}${formatBytes(row.size)?` · ${formatBytes(row.size)}`:''}</small></div><span class="import-file-percent">${importPhasePercent[row.phase]||0}%</span></div>`;
    }).join('');
    const timeline=events.filter(item=>item.data?.jobId===job.id&&['import.started','import.progress'].includes(item.type)).slice(-80).map(item=>`<li class="activity-item ${activityState(item)}"><span class="activity-dot"></span><span>${e(activityText(item))}</span><time>${e(formatTime(item.created))}</time></li>`).join('');
    return `<div class="import-progress-block"><div class="import-progress-heading"><div><strong>${heading}</strong><small>${completed}/${rows.length} 个文件已结束</small></div><span class="pill ${failed?'warm':active?'purple':'green'}">${percent}%</span></div><div class="import-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><span style="width:${percent}%"></span></div><div class="import-progress-files">${rowsHtml}</div>${timeline?`<details class="activity-detail"><summary>查看处理记录</summary><ul class="activity-timeline">${timeline}</ul></details>`:''}</div>`;
  }
  function renderActivities(activities){
    const jobs=activities?.jobs||[];
    const events=(activities?.events||[]).filter(item=>['job.queued','job.terminal','attachment.imported','turn.accepted','turn.preparing','turn.prepared','run.created','run.phase','run.item','run.committed','definition.created','definition.progress','definition.published','session.metrics.ready','import.started','import.progress'].includes(item.type));
    const active=jobs.some(job=>['QUEUED','RUNNING'].includes(job.status));
    if(!jobs.length&&!events.length)return '';
    const importJobs=jobs.filter(job=>job.kind==='import');
    const importCards=importJobs.map(job=>importProgress(job,events)).join('');
    const timeline=events.filter(item=>!['import.started','import.progress'].includes(item.type)).slice(-48).map(item=>`<li class="activity-item ${activityState(item)}"><span class="activity-dot"></span><span>${e(activityText(item))}</span><time>${e(formatTime(item.created))}</time></li>`).join('');
    const jobDetails=jobs.filter(job=>job.kind==='import'&&job.result).map(job=>`<details class="activity-detail" ${active?'open':''}><summary>文件导入结果 · ${label(job.status)} · ${e(job.id.slice(-10))}</summary>${importResult(job.result)}</details>`).join('');
    return `<section class="activity-card ${active?'active':''}"><div class="activity-head"><strong>${active?'正在处理':'处理过程'}</strong><span class="pill">${active?'进行中':'已记录'}</span><small>${jobs.length} 个处理任务</small></div>${importCards}${timeline?`<ul class="activity-timeline">${timeline}</ul>`:''}${jobs.map(jobFailure).join('')}${jobDetails}</section>`;
  }
  function latestConversationClearAt(detail){
    const eventAt=(detail.activities?.events||[]).filter(item=>item.type==='conversation.cleared').map(item=>item.created).filter(Boolean).sort().at(-1)||null;
    return [state.lastClearedAt,eventAt].filter(Boolean).sort().at(-1)||null;
  }
  function itemAfterConversationClear(item, clearAt){
    if(!clearAt)return true;
    const timestamp=item?.created||item?.updated||'';
    return !timestamp||timestamp>clearAt;
  }
  function splitConversationHistory(detail, clearAt){
    const activities=detail.activities||{jobs:[],events:[]};
    const split=(items,keep)=>items.filter(item=>itemAfterConversationClear(item,clearAt)===keep);
    return {
      clearAt,
      visibleMessages:(detail.messages||[]).filter(item=>itemAfterConversationClear(item,clearAt)),
      visibleActivities:{jobs:split(activities.jobs||[],true),events:split((activities.events||[]).filter(item=>item.type!=='conversation.cleared'),true)},
      visibleRuns:split(detail.runs||[],true),
      visibleComparisons:split(detail.comparisons||[],true),
      retainedActivities:{jobs:split(activities.jobs||[],false),events:split((activities.events||[]).filter(item=>item.type!=='conversation.cleared'),false)},
      retainedMessages:(detail.messages||[]).filter(item=>itemAfterConversationClear(item,clearAt)===false),
      retainedRuns:split(detail.runs||[],false),
      retainedComparisons:split(detail.comparisons||[],false)
    };
  }
  function renderMessage(message){
    return `<article class="msg ${message.role==='user'?'user':''}"><div class="avatar">${message.role==='user'?'我':'✳'}</div><div class="msg-body"><div class="msg-author">${message.role==='user'?'本机使用者':'Insight Agent'} <span class="message-time">${e(formatTime(message.created))}</span></div><div class="message-text">${e(message.text)}</div>${message.role==='assistant'?`<div class="message-actions">${message.uiAction==='SETTINGS'?btn('openSettings','打开设置'):''}${btn('copy','复制',message.id)}${btn('feedback',message.feedback===true?'取消赞':'有帮助',message.id,'data-value="true"')}${btn('feedback',message.feedback===false?'取消踩':'需改进',message.id,'data-value="false"')}${message.runId?btn('explain','解释',message.runId)+btn('regenerate','重新生成解释',message.runId):''}</div>`:''}</div></article>`;
  }
  function chronological(items){
    return items.map((item,index)=>({item,index})).sort((a,b)=>{
      const left=String(a.item?.created||a.item?.updated||'');
      const right=String(b.item?.created||b.item?.updated||'');
      return left.localeCompare(right)||a.index-b.index;
    }).map(entry=>entry.item);
  }
  function activityItemMarkup(item){
    return `<div class="conversation-step conversation-step-activity ${activityState(item)}"><span class="activity-dot"></span><span>${e(activityText(item))}</span><time>${e(formatTime(item.created))}</time></div>`;
  }
  function latestExtraction(definitionId, originalJobId=''){
    return (state.detail?.activities?.jobs||[]).filter(job=>job.kind==='extract'&&(job.definitionId===definitionId||job.id===originalJobId)).sort((a,b)=>String(b.created).localeCompare(String(a.created)))[0];
  }
  function latestDocumentParsing(definitionId){
    return (state.detail?.activities?.jobs||[]).filter(job=>['extract','metric_catalog'].includes(job.kind)&&job.definitionId===definitionId).sort((a,b)=>String(b.created).localeCompare(String(a.created)))[0];
  }
  function jobFailure(job){
    if(!['FAILED','CANCELLED'].includes(job.status)||!job.error)return '';
    const error=job.error,details=error.details||{},definitionId=job.definitionId||details.definitionId;
    if(['extract','metric_catalog'].includes(job.kind)){
      const current=latestDocumentParsing(definitionId);
      if(current&&current.id!==job.id&&String(current.created)>String(job.created)&&['QUEUED','RUNNING','SUCCEEDED'].includes(current.status))return '';
    }
    if(job.kind==='metric_catalog')return `<div class="job-failure" role="status"><strong>指标说明解析未完成</strong><p>${e(error.message||'处理失败')}</p><small>${e(error.code)} · 已保存的指标清单与口径可继续查看。</small>${btn('catalogView','查看解析结果')}${btn('parseMetricSpec','继续解析')}</div>`;
    const definition=state.detail?.metricDefinition?.id===definitionId?state.detail.metricDefinition:null;
    const latest=definitionId?latestExtraction(definitionId):null;
    const retry=definition?.status==='DRAFT'&&(!latest||latest.id===job.id||!['QUEUED','RUNNING','SUCCEEDED'].includes(latest.status));
    const advice=error.code==='DEFINITION_UNAVAILABLE'&&definition?.status==='PUBLISHED'?'当前说明已就绪，可重新开始分析。':{WAIT_EXTRACTION:'重新开始分析后，会等待指标规则就绪并自动继续。',RETRY_EXTRACTION:'检查本次解析错误后，可重试解析或重新开始分析。',VIEW_METRICS:'查看各项原因，完善说明文档后重新上传。',UPLOAD_DEFINITION:'请上传本次对话的指标说明。'}[details.nextAction];
    const actions=definitionId?`${definition?btn('definition','查看说明中的指标',definitionId):''}${retry?btn('retryExtract','重试解析',definitionId):''}`:'';
    const choose=error.code==='DEFINITION_UNAVAILABLE'&&!definition?btn('chooseDefinition','查看指标说明'):'';
    const settings=/^MODEL_|^NETWORK_|^HTTP_/.test(error.code||'')?btn('openSettings','检查模型设置'):'';
    return `<div class="job-failure" role="status" data-job-id="${e(job.id)}"><strong>${job.kind==='extract'?'指标提取未完成':'本次处理未完成'}</strong><p>${e(error.message||'处理失败，请重试')}</p><small>错误代码：${e(error.code||'UNKNOWN_ERROR')} · ${e(formatTime(job.updated||job.created))}</small>${advice?`<p>${e(advice)}</p>`:''}<div class="job-failure-actions">${actions}${choose}${settings}</div></div>`;
  }
  function unitHeader(title, time, detail, active=false){
    return `<div class="conversation-unit-head"><div><strong>${e(title)}</strong>${detail?`<small>${e(detail)}</small>`:''}</div><div class="conversation-unit-meta">${active?'<span class="pill purple">进行中</span>':''}<time>${e(formatDateTime(time))}</time></div></div>`;
  }
  function renderTurnUnit(unit){
    const entries=[
      ...unit.messages.map(item=>({time:item.created,priority:1,markup:renderMessage(item)})),
      ...chronological(unit.events).map(item=>({time:item.created,priority:2,markup:activityItemMarkup(item)})),
      ...unit.jobs.filter(job=>job.error).map(job=>({time:job.updated||job.created,priority:5,markup:jobFailure(job)})),
      ...unit.runs.map(item=>({time:item.created||item.updated,priority:3,markup:renderRun(item)})),
      ...unit.comparisons.map(item=>({time:item.created||item.updated,priority:4,markup:renderComparison(item)}))
    ];
    entries.sort((a,b)=>String(a.time||'').localeCompare(String(b.time||''))||a.priority-b.priority);
    const active=unit.jobs.some(job=>['QUEUED','RUNNING'].includes(job.status))||unit.runs.some(run=>['QUEUED','RUNNING'].includes(run.status));
    return `<section class="conversation-unit conversation-unit-turn" data-unit-type="turn" data-created="${e(unit.time)}">${unitHeader('对话轮次',unit.time,unit.turnId?`轮次 ${unit.turnId.slice(-10)}`:'',active)}<div class="conversation-unit-body">${entries.map(item=>item.markup).join('')||'<p class="muted">本轮暂无可显示内容</p>'}</div></section>`;
  }
  function renderUploadUnit(unit){
    const active=[unit.job,...unit.extractJobs].some(job=>['QUEUED','RUNNING'].includes(job.status));
    const eventMarkup=chronological(unit.events.filter(item=>!['import.started','import.progress'].includes(item.type))).map(activityItemMarkup).join('');
    const result=unit.job.result?`<details class="activity-detail" open><summary>文件导入结果 · ${label(unit.job.status)} · ${e(unit.job.id.slice(-10))}</summary>${importResult(unit.job.result)}</details>`:'';
    return `<section class="conversation-unit conversation-unit-upload" data-unit-type="upload" data-created="${e(unit.time)}">${unitHeader('上传文件',unit.time,`${unit.job.result?.acceptedDetails?.length||0} 个文件已接收`,active)}<div class="conversation-unit-body">${importProgress(unit.job,unit.events)}${eventMarkup?`<div class="conversation-processing">${eventMarkup}</div>`:''}${jobFailure(unit.job)}${result}</div></section>`;
  }
  function renderGeneralUnit(unit){
    const content=chronological(unit.events).map(activityItemMarkup).join('')+unit.jobs.map(job=>`<div class="conversation-step conversation-step-activity"><span class="activity-dot"></span><span>${e(`处理任务${label(job.status)} · ${label(job.kind)}`)}</span><time>${e(formatTime(job.created))}</time></div>${jobFailure(job)}${job.kind==='metric_catalog'&&!['FAILED','CANCELLED'].includes(job.status)?btn('catalogView','查看解析结果'):''}`).join('');
    return `<section class="conversation-unit conversation-unit-processing" data-unit-type="processing" data-created="${e(unit.time)}">${unitHeader('处理记录',unit.time,'未关联到具体上传或对话',false)}<div class="conversation-unit-body">${content||'<p class="muted">暂无处理记录</p>'}</div></section>`;
  }
  function conversationUnits(history){
    const activities=history.visibleActivities||{jobs:[],events:[]};
    const jobs=activities.jobs||[], events=activities.events||[], messages=history.visibleMessages||[], runs=history.visibleRuns||[], comparisons=history.visibleComparisons||[];
    const units=[];
    const turns=new Map(), runById=new Map(runs.map(run=>[run.id,run]));
    const turnByRun=new Map(runs.filter(run=>run.turnId).map(run=>[run.id,run.turnId]));
    const turnByJob=new Map();
    const assigned={events:new Set(),messages:new Set(),runs:new Set(),comparisons:new Set(),jobs:new Set()};
    const ensureTurn=(turnId,time)=>{
      if(!turnId)return null;
      let unit=turns.get(turnId);
      if(!unit){unit={type:'turn',turnId,time:time||'',messages:[],events:[],runs:[],comparisons:[],jobs:[]};turns.set(turnId,unit);units.push(unit);}
      if(time&&(!unit.time||time<unit.time))unit.time=time;
      return unit;
    };
    const ensureUpload=(job)=>{
      const unit={type:'upload',job,events:[],extractJobs:[],time:job.created||job.updated||''};units.push(unit);return unit;
    };
    jobs.filter(job=>job.kind==='import').forEach(job=>{
      const unit=ensureUpload(job);
      events.forEach(item=>{if(item.data?.jobId===job.id){unit.events.push(item);assigned.events.add(item);}});
      assigned.jobs.add(job);
    });
    jobs.filter(job=>job.kind==='extract').forEach(job=>{
      const unit=units.find(candidate=>candidate.type==='upload'&&(candidate.job.result?.acceptedDetails||[]).some(item=>item.definitionId===job.definitionId||item.extractJobId===job.id||item.catalogJobId===job.id));
      if(!unit)return;
      unit.extractJobs.push(job);assigned.jobs.add(job);
      events.forEach(item=>{if(item.data?.jobId===job.id||(!item.data?.jobId&&item.scope===job.definitionId)){unit.events.push(item);assigned.events.add(item);}});
    });
    events.filter(item=>item.type==='turn.accepted'&&item.data?.turnId).forEach(item=>{turnByJob.set(item.data.jobId,item.data.turnId);const unit=ensureTurn(item.data.turnId,item.created);unit.events.push(item);assigned.events.add(item);});
    jobs.filter(job=>job.kind==='turn'&&turnByJob.has(job.id)).forEach(job=>{
      const unit=ensureTurn(turnByJob.get(job.id),job.created||job.updated);
      unit.jobs.push(job);
      assigned.jobs.add(job);
    });
    messages.forEach(item=>{
      const turnId=item.turnId||turnByRun.get(item.runId);
      if(turnId){const unit=ensureTurn(turnId,item.created);unit.messages.push(item);assigned.messages.add(item);}
    });
    runs.forEach(item=>{
      const turnId=item.turnId||turnByRun.get(item.id);
      if(turnId){const unit=ensureTurn(turnId,item.created||item.updated);unit.runs.push(item);assigned.runs.add(item);}
    });
    events.forEach(item=>{
      if(assigned.events.has(item))return;
      const turnId=item.data?.turnId||turnByRun.get(item.data?.runId)||turnByJob.get(item.data?.jobId);
      if(turnId){const unit=ensureTurn(turnId,item.created);unit.events.push(item);assigned.events.add(item);}
    });
    comparisons.forEach(item=>{
      const unit=[...turns.values()].find(candidate=>candidate.runs.some(run=>item.runIds?.includes(run.id)));
      if(unit){unit.comparisons.push(item);assigned.comparisons.add(item);if(item.created&&item.created<unit.time)unit.time=item.created;}
    });
    messages.filter(item=>!assigned.messages.has(item)).forEach(item=>units.push({type:'turn',turnId:item.turnId||'',time:item.created||'',messages:[item],events:[],runs:[],comparisons:[],jobs:[]}));
    runs.filter(item=>!assigned.runs.has(item)).forEach(item=>units.push({type:'turn',turnId:item.turnId||'',time:item.created||item.updated||'',messages:[],events:[],runs:[item],comparisons:[],jobs:[]}));
    comparisons.filter(item=>!assigned.comparisons.has(item)).forEach(item=>units.push({type:'turn',turnId:'',time:item.created||item.updated||'',messages:[],events:[],runs:[],comparisons:[item],jobs:[]}));
    const unassignedEvents=events.filter(item=>!assigned.events.has(item));
    const unassignedJobs=jobs.filter(item=>!assigned.jobs.has(item));
    if(unassignedEvents.length||unassignedJobs.length){units.push({type:'processing',time:chronological([...unassignedEvents,...unassignedJobs])[0]?.created||unassignedJobs[0]?.created||'',events:unassignedEvents,jobs:unassignedJobs});}
    return units.sort((a,b)=>String(a.time||'').localeCompare(String(b.time||'')));
  }
  function renderConversation(history){
    return conversationUnits(history).map(unit=>unit.type==='upload'?renderUploadUnit(unit):unit.type==='processing'?renderGeneralUnit(unit):renderTurnUnit(unit)).join('');
  }
  function renderSession(){const d=state.detail,s=d.session;renderHistory();$('chatName').textContent=s.name;$('sessionCode').textContent='#'+s.id.slice(-10);$('chatTags').innerHTML=`<span class="pill ${s.state==='LOCKED'?'green':'purple'}">${label(s.state)}</span><span class="pill">${d.attachments.length} 份附件</span>${s.archived?'<span class="pill">已归档</span>':''}`;
    const groups={data:[],metric:[],pending:[]};
    d.attachments.forEach(a=>{if(a.route.role==='DATA_FILE')groups.data.push(a);else if(a.route.role==='METRIC_DEFINITION')groups.metric.push(a);else groups.pending.push(a);});
    const definition=d.metricDefinition;$('sideVersion').textContent=definition?.name||'尚未上传';$('sideVersionDesc').textContent=definition?definition.fillSlotCount?`${definition.fillSlotCount} 个待填位置 · ${label(definition.status)} · 分析时核验取数条件`:`${definition.metricCount} 项指标 · ${definition.clearCount} 项可计算 · ${label(definition.status)}`:'上传后自动解析并用于当前对话';$('lockText').textContent=s.state==='LOCKED'?'🔒 已冻结说明和数据':'使用本次上传的说明';$('dataFileCounter').textContent=groups.data.length+' 个';$('metricFileCounter').textContent=groups.metric.length+' / 1';$('pendingFileCounter').textContent=groups.pending.length+' 个';$('composeInfo').textContent=`${d.tables.reduce((a,t)=>a+t.rowCount,0)} 行 · ${definition?.name||'待上传指标说明'}`;
    renderAttachmentList('dataFiles',groups.data,s,'尚未导入数据文件');
    renderAttachmentList('metricFile',groups.metric,s,'尚未导入指标说明');
    renderAttachmentList('pendingFiles',groups.pending,s,'没有待处理文件');
    const catalogJob=latestCatalogJob(definition?.id),catalogReady=definition?.catalogSummary?.status==='READY';
    $('metricParse').disabled=!definition||s.archived;
    $('metricParse').textContent=catalogReady?'查看解析结果':['QUEUED','RUNNING'].includes(catalogJob?.status)?'查看解析进度':'解析指标说明';
    $('metricFileHint').textContent=definition?.catalogSummary?`已识别 ${definition.catalogSummary.count} 项指标，已整理 ${definition.catalogSummary.completed} 项口径。`:groups.metric.length?'可独立解析指标清单、公式和待确认项，无需上传数据文件。':'上传 Word 后可独立解析指标清单、公式和待确认项。';
    $('pendingFileHint').textContent=groups.pending.length?'请查看识别依据并确认用途，混合内容需要先拆分。':'用途不明确、混合内容或其他文件会显示在这里。';
    $('dataFoot').textContent=d.tables.length?`${d.tables.length} 个工作表 · 全量扫描`:'数据文件可添加多份，导入后自动识别类型';
    $('auditStatus').innerHTML=`<p>${definition?.status==='PUBLISHED'?'✓ 指标解析就绪':'○ 待上传或解析指标说明'}</p><p>${Object.keys(s.mappings).length?'✓ 已保存字段映射':'○ 计算时自动匹配同名字段，其他字段需确认'}</p><p>${s.state==='LOCKED'?'🔒 基线已冻结':'○ 首次执行型对话后锁定'}</p>`;
    const processing=state.boot?.processing||[];
    const active=processing.some(item=>item.scope===s.id)||((d.activities?.jobs||[]).some(item=>['QUEUED','RUNNING'].includes(item.status)));
    const analysisBusy=processing.some(item=>item.scope===s.id)||((d.activities?.jobs||[]).some(item=>['QUEUED','RUNNING'].includes(item.status)&&!['extract','metric_catalog'].includes(item.kind)));
    $('conversationGuidance').textContent=active?(analysisBusy?'正在处理当前请求，结果会显示在本次对话中':'指标说明正在解析；开始分析时会等待规则就绪后自动继续'):s.state==='LOCKED'?'数据基线已冻结，继续描述要查询、解释或比较的内容。':'上传指标说明和数据文件后，计算说明文档中的指标值。';
    $('sendBtn').disabled=active||s.archived;$('planBtn').disabled=analysisBusy||s.archived;$('analyzeBtn').disabled=analysisBusy||s.archived;$('mappingBtn').disabled=s.state!=='DRAFT';
    $('selectMetricVersion').disabled=s.state!=='DRAFT'||s.archived||active;
    if(definition&&!definition.sessionId){$('metricParse').disabled=true;$('metricFileHint').textContent=definition.sourceKind==='MANUAL'?'当前使用手工维护的指标版本，可在指标管理中创建新版本。':'当前使用指标管理中的已发布版本，点击“查看指标”核对说明与规则。';$('lockText').textContent=s.state==='LOCKED'?'🔒 已冻结指标和数据':'使用指标管理中的已发布版本';}
    const history=splitConversationHistory(d,latestConversationClearAt(d));
    const atBottom=$('chatFeed').scrollHeight-$('chatFeed').scrollTop-$('chatFeed').clientHeight<80;
    const currentMarkup=renderConversation(history);
    const retainedMarkup=renderConversation({visibleMessages:history.retainedMessages,visibleActivities:history.retainedActivities,visibleRuns:history.retainedRuns,visibleComparisons:history.retainedComparisons});
    const retainedHistory=history.clearAt&&retainedMarkup?`<details class="activity-detail retained-history"><summary>查看已保留的历史结果和处理记录</summary>${retainedMarkup}</details>`:'';
    const clearNotice=history.clearAt?`<div class="notice-box conversation-cleared-note"><strong>对话内容已清除</strong><span>用户问题、Agent 回复和输入框草稿已移除。附件、指标定义、分析结果和处理记录仍保留。</span>${retainedHistory}</div>`:'';
    const hasCurrentContent=conversationUnits(history).length>0;
    const empty=!hasCurrentContent?'<div class="empty-state"><div class="empty-symbol">✳</div><h2>准备好本次数据</h2><p>上传 Word 指标说明和 Excel 数据，等待解析后计算指标值。<br>同名字段自动匹配，其他字段可通过字段映射确认。</p></div>':'';
    $('chatFeed').innerHTML=clearNotice+currentMarkup+empty;
    if(atBottom)$('chatFeed').scrollTop=$('chatFeed').scrollHeight;
  }
  function renderPhaseHistory(run){
    const phases=run.phaseHistory||[];
    if(!phases.length)return '';
    return `<section class="analysis-flow" aria-label="分析执行流程"><div class="analysis-flow-head"><strong>分析执行流程</strong><span class="pill">${run.committed?'已完成':label(run.phase)}</span><small>按 LangGraph 阶段记录</small></div><ol class="analysis-flow-list">${phases.map((item,index)=>{const done=Boolean(item.endedAt)||item.phase==='COMPLETED';const current=!done&&!run.committed;const duration=item.durationMs==null?(current?'进行中':'—'):`${(Number(item.durationMs)/1000).toFixed(1)} 秒`;return `<li class="analysis-flow-step ${done?'done':current?'current':''}"><span class="analysis-flow-dot"></span><div><strong>${e(label(item.phase)||item.phase)}</strong><small>${e(formatDateTime(item.startedAt))}${item.endedAt?` · ${e(formatTime(item.endedAt))}`:''}</small></div><time>${e(duration)}</time></li>`;}).join('')}</ol></section>`;
  }
  function renderFilledReport(report){if(!report)return '';return `<section class="filled-report"><h3>按指标说明回填的内容</h3><p class="muted">${e(report.scopeNote)}</p>${report.paragraphs.map(p=>`<p class="filled-paragraph">${e(p.text)}</p>`).join('')}<details><summary>待补充清单（${report.gaps.length} 处）</summary>${report.gaps.map(g=>`<p><strong>${e(g.name)}</strong> · ${e(g.reason||g.code)}<small> · ${e(g.locator)}</small></p>`).join('')}</details></section>`;}
  function renderRun(run){let body=`<div class="run-heading"><strong>${run.mode==='PLAN'?'执行计划':'分析结果'}</strong><span class="pill">${label(run.status)}</span><small>${e(run.id.slice(-10))}</small></div>${renderPhaseHistory(run)}<div class="run-scope">统计范围：${run.contract?.parameters?.scope==='ALL_DATA'?'当前会话全部数据文件':`${e(run.contract?.parameters?.date_start||'')} 至 ${e(run.contract?.parameters?.date_end||'')}`}</div>${run.committed?renderFilledReport(run.filledReport):''}`;
    if(!run.committed){body+=`<p>${e(run.phase)} ${e(run.technicalFailure?.message||'')}</p>${run.technicalFailure?btn('retry','新建运行重试',run.id):''}`;}
    else{body+=run.results.map(r=>{const definition=r.definition||{};const basis=definition.expression?`<details class="metric-definition"><summary>指标口径与审计</summary><p>口径顺序 ${e(r.definitionOrder||definition.definitionOrder||'—')} · 实体 ${e(definition.entityType||'—')} · 分组 ${e((definition.groupBy||[]).join('、')||'总体')}</p><pre>${json({expression:definition.expression,filters:definition.filters,exclusions:definition.exclusions,requiredFields:definition.requiredFields,precision:definition.precision,missingPolicy:definition.missingPolicy,invalidPolicy:definition.invalidPolicy,sourceEvidence:definition.sourceEvidence})}</pre></details>`:'';return r.outcome==='VALUE'?`<div class="result-metric"><strong>${e(r.name)}</strong>${basis}${table(['分组','结果','单位'],r.groups.map(g=>[Object.values(g.group).join(' / ')||'总体',g.outcome==='VALUE'?g.displayValue:'跳过：'+g.reasonCode,r.unit]))}<small class="result-audit">输入 ${r.audit?.inputRows??'—'} 行 · 纳入 ${r.audit?.includedRows??'—'} 行 · 排除 ${r.audit?.excludedRows??'—'} 行 · 缺失 ${r.audit?.missingRows??'—'} 行 · 非法 ${r.audit?.invalidRows??'—'} 行</small>${btn('evidence','依据 / 处理记录',run.id,'data-metric="'+e(r.metricId)+'"')}</div>`:`<div class="skip-row"><strong>${e(r.name)}</strong> · ${r.outcome==='PLANNED'?'计划就绪':e(r.reason||r.reasonCode)}${basis}${btn('evidence','查看依据',run.id,'data-metric="'+e(r.metricId)+'"')}</div>`;}).join('');const isAllData=run.contract?.parameters?.scope==='ALL_DATA';body+=`<div class="run-actions">${btn('runDetail','运行合同 / 记录',run.id)}${run.mode!=='PLAN'?btn('query','筛选已有结果',run.id)+btn('formula','本地公式',run.id)+btn('followup','引用此运行分析',run.id):''}${run.mode!=='PLAN'&&!isAllData?btn('compare','同比 / 环比 / 趋势',run.id):''}${run.mode!=='PLAN'?btn('recalculate','离线冻结重算',run.id):''}${(run.filledReport||run.results.some(r=>r.outcome==='VALUE'))?['docx','xlsx','json','zip','csv','pdf'].map(f=>btn('export',f.toUpperCase(),run.id,'data-format="'+f+'"'+(f==='pdf'&&!state.boot.readiness.pdf?' disabled title="请先安装 LibreOffice"':''))).join(''):''}</div>`;}
    body+=(state.detail.artifacts||[]).filter(a=>a.runId===run.id).map(a=>`<div class="artifact"><span>${e(a.format.toUpperCase())} · ${a.status==='READY'?'可用':label(a.layoutQA)+'草稿'}</span>${btn('artifactSave','另存',a.id)}${a.renderHash?btn('artifactPreview','页面预览',a.id)+btn('artifactApprove','已检查全部页面',a.id):''}</div>`).join('');return `<section class="run-card">${body}</section>`;
  }
  async function newSession(definitionId=null){await flushDraft();const s=await api('session.create',definitionId?{definition_id:definitionId}:{});await selectSession(s.id);toast('已新建独立数据对话');}
  async function update(changes,mappingReview=null){const s=state.detail.session;await api('session.update',{session_id:s.id,revision:s.revision,changes,mapping_review:mappingReview});await refresh(true);}
  async function flushDraft(){clearTimeout(state.draftTimer);if(state.library||!state.id||!state.detail)return;const draft=$('prompt').value;if(draft===state.detail.session.draft)return;const s=state.detail.session;try{const saved=await api('session.update',{session_id:s.id,revision:s.revision,changes:{draft}});state.detail.session=saved;}catch(error){if(!error.message.includes('[REVISION_CONFLICT]'))throw error;await refresh(true);const current=state.detail.session;if(draft!==current.draft){const saved=await api('session.update',{session_id:current.id,revision:current.revision,changes:{draft}});state.detail.session=saved;}}}
  async function submit(action='AUTO',source=null,suppliedParameters=null){if(!state.id){await newSession();}await flushDraft();await refresh(true);const text=action==='AUTO'?$('prompt').value.trim():action==='EXPLAIN'?($('prompt').value.trim()||'说明本次结果的依据与限制'):action==='REGENERATE'?'重新表述有来源的解释':'';if(action==='AUTO'&&!text){toast('请先输入问题或分析目标');$('prompt').focus();return;}const parameters=suppliedParameters??(['PLAN','ANALYZE'].includes(action)?{scope:'ALL_DATA'}:{});const s=state.detail.session;await api('turn.submit',{session_id:s.id,revision:s.revision,request_id:crypto.randomUUID(),text,action,parameters,source_run_id:source});$('prompt').value='';await refresh(true);toast('正在处理对话，结果会显示在当前会话');}
  async function showDefinition(){
    if(!state.id)return modal('说明文档中的指标','<p>请先创建对话并上传 Word 指标说明。</p>');
    const d=await api('session.metrics',{session_id:state.id});
    if(!d)return chooseLibraryDefinition();
    if(!d.sessionId)return library.show(d.id);
    if(!d.candidates.length&&latestDocumentParsing(d.id)?.kind==='metric_catalog')return showMetricCatalog();
    const original=d.nodes.map(n=>`<div class="source-node"><code>${e(n.locator)}</code><p>${e(n.text)}</p></div>`).join('');
    const metrics=d.candidates.map(c=>`<div class="result-metric"><strong>${e(c.name)}</strong><p>${c.status==='REPORT'?'待填项已登记，分析时核验资料':c.status==='CLEAR'?'可计算':'无法计算'}${c.reason?' · '+e(c.reason):''}</p><small>原文位置：${e((c.locators||[]).join('、'))}</small>${(c.rule||c.plan)?`<details><summary>取数计划与证据</summary><pre>${json(c.rule||c.plan)}</pre></details>`:''}</div>`).join('');
    modal('说明文档中的指标',`<div class="notice-box">${e(d.name)} · ${label(d.status)}<br>解析完成后自动用于当前对话。无法确定的条款会说明原因。</div>${btn('catalogView','查看指标说明解析')}<h3>指标（${d.candidates.length} 项）</h3>${metrics||'<p>正在等待解析结果。</p>'}<details><summary>说明文档原文（${d.nodes.length} 个节点）</summary>${original}</details><details><summary>全文处理清单</summary><pre>${json(d.coverage)}</pre></details>`,d.status==='DRAFT'&&state.detail.session.state==='DRAFT'?btn('retryExtract','重试解析',d.id):'');
  }
  function chooseLibraryDefinition(){
    const versions=(state.boot?.definitions||[]).filter(d=>d.status==='PUBLISHED'&&!d.disabled);
    modal('选择指标版本',`<p>可以使用指标管理中已发布的版本，也可在当前对话上传 Word 指标说明。</p>${versions.length?`<label>指标版本<select id="bindLibraryVersion">${versions.map(d=>`<option value="${e(d.id)}" ${d.default?'selected':''}>${e(d.name)} · ${e(d.versionLabel)}</option>`).join('')}</select></label>`:'<p>暂无启用的已发布版本，请先到指标管理新增并发布。</p>'}`, (versions.length?btn('bindLibraryVersion',state.id?'用于当前对话':'新建对话使用'):'')+btn('openMetricLibrary','打开指标管理'));
  }
  function latestCatalogJob(definitionId){return (state.detail?.activities?.jobs||[]).find(j=>j.kind==='metric_catalog'&&j.definitionId===definitionId);}
  async function parseMetricSpec(){
    if(!state.id||!state.detail?.metricDefinition){toast('请先导入并确认 Word 指标说明');return;}
    await api('session.metrics.parse',{session_id:state.id});await refresh(true);await showMetricCatalog();
  }
  async function showMetricCatalog(open=true){
    const sessionId=state.id,definition=state.detail?.metricDefinition;
    if(!definition)return modal('指标说明解析','<p>请先导入并确认当前对话的 Word 指标说明。</p>');
    const parsed=await api('session.metricCatalog',{session_id:sessionId});
    if(state.id!==sessionId||(!open&&state.catalogDialog!==sessionId))return;
    const job=latestCatalogJob(definition.id),active=['QUEUED','RUNNING'].includes(job?.status);
    const stamp=JSON.stringify([parsed,job?.status,job?.error]);
    if(!open&&state.catalogStamp===stamp)return;
    const items=parsed?.items||[],completed=items.filter(i=>i.status!=='PENDING').length;
    const rows=items.map((i,index)=>`<article class="result-metric catalog-metric"><strong>${index+1}. ${e(i.name)}</strong><p>${e(i.category)} · ${i.status==='PENDING'?'待补充口径':i.status==='NEEDS_CLARIFICATION'?'存在待确认项':'口径已提取'}</p>${i.status==='PENDING'?'':`<dl><dt>单位</dt><dd>${e(i.unit||'待确认')}</dd><dt>维度</dt><dd>${e(i.dimensions.join('、')||'未明确')}</dd><dt>统计期间</dt><dd>${e(i.timeWindow||'待确认')}</dd><dt>公式</dt><dd>${e(i.formula||'原文未明确')}</dd></dl>${i.conditions.length?`<p><strong>条件：</strong>${e(i.conditions.join('；'))}</p>`:''}${i.exclusions.length?`<p><strong>排除：</strong>${e(i.exclusions.join('；'))}</p>`:''}${i.unresolved.length?`<p class="notice-box"><strong>待确认：</strong>${e(i.unresolved.join('；'))}</p>`:''}`}<small>原文位置：${e(i.locators.join('、'))}</small>${i.sourceEvidence?.length?`<details><summary>原文依据</summary>${i.sourceEvidence.map(ref=>`<div class="source-node"><code>${e(ref.locator)}</code><p>${e(ref.quote)}</p></div>`).join('')}</details>`:''}</article>`).join('');
    const body=`<div class="notice-box">${e(definition.name)}<br>${active?'正在解析':parsed?.status==='READY'?'解析完成':'解析待完成'} · 已识别 ${items.length} 项指标，已整理 ${completed} 项口径。<br>指标清单用于核对说明内容；可计算性将在数据与规则校验时确认。</div>${jobFailure(job||{})}${rows||'<p>点击“开始解析”，提取说明中的指标、公式和待确认项。无需上传数据文件。</p>'}`;
    const footer=active?'<span>正在分批整理口径，完成后自动更新。</span>':parsed?.status==='READY'?'':btn('parseMetricSpec',parsed?'继续解析':'开始解析');
    if(open)modal('指标说明解析',body,footer);else{$('dialogBody').innerHTML=body;$('dialogFooter').innerHTML=footer;}
    state.catalogDialog=sessionId;state.catalogStamp=stamp;
  }
  async function showMapping(){const s=state.detail.session;if(state.detail.metricDefinition?.status!=='PUBLISHED')throw Error('请先上传指标说明并等待解析完成');const def=await api('session.metrics',{session_id:s.id});const suggested=await api('mapping.suggest',{session_id:s.id});state.mappingDefinition=def;state.mappingDraft=structuredClone(Object.keys(s.mappings).length?s.mappings:suggested);let content='<p>选择来源表与字段。合并多表仅适用于相同实体粒度；关联模式请先明确唯一键与基数。</p>';
    for(const c of def.candidates.filter(c=>c.status==='CLEAR'&&!def.metricExclusions?.[c.id])){const m=state.mappingDraft[c.id]||{};content+=`<fieldset class="mapping-field" data-metric="${e(c.id)}"><legend>${e(c.name)}</legend><select class="table-select"><option value="">暂不映射（本项将跳过）</option>${state.detail.tables.map((t,i)=>`<option value="${i}" ${m.sources?.[0]?.attachmentId===t.attachmentId&&m.sources?.[0]?.sheet===t.sheet?'selected':''}>${e(t.fileName+' / '+t.sheet+' · '+t.rowCount+'行')}${t.executionBlocked?'（仅预览）':''}</option>`).join('')}</select><div class="field-selects"></div><label>全量完整性依据<input class="coverage-evidence" placeholder="例如：系统导出的全部事件，含非发生记录" value="${e(m.coverage?.evidence||'')}"></label><label><input class="coverage-complete" type="checkbox" ${m.coverage?.complete?'checked':''}>我已核对本来源覆盖当前会话全部数据</label></fieldset>`;}
    content+='<details><summary>高级映射（多表合并 / 关联）</summary><p>union 的每个来源需映射全部标准字段；join 恰好两个来源，合起来覆盖全部标准字段，左右共同映射关联键。1:1、1:n、n:1 均校验唯一性，拒绝多对多。</p><pre>{"mode":"join","relationship":{"keys":["surgery_id"],"cardinality":"1:1","unmatchedPolicy":"reject","evidence":"经核对两表使用同一手术编号"},"sources":[{"attachmentId":"左表附件ID","sheet":"事件","fields":{"surgery_id":"编号","surgery_date":"日期"}},{"attachmentId":"右表附件ID","sheet":"时间","fields":{"surgery_id":"编号","room_entry":"入室","anesthesia_start":"麻醉开始"}}],"coverage":{"complete":true,"evidence":"当前会话全部数据"}}</pre><textarea id="advancedMapping" class="code-editor" aria-label="高级映射 JSON"></textarea><label><input id="useAdvanced" type="checkbox">保存此 JSON，覆盖上方表单</label></details>';content+='<label>映射维护人<input id="mappingActor" value="本机使用者"></label><label>修改依据<input id="mappingReason" placeholder="说明字段含义、关联和全量范围核对依据"></label>';modal('字段映射与范围确认',content,btn('saveMapping','校验并保存映射'));$('advancedMapping').value=JSON.stringify(state.mappingDraft,null,2);document.querySelectorAll('.mapping-field').forEach(renderFieldSelects);
  }
  function renderFieldSelects(fieldset){const c=state.mappingDefinition.candidates.find(c=>c.id===fieldset.dataset.metric);const t=state.detail.tables[fieldset.querySelector('.table-select').value];const m=state.mappingDraft[c.id];fieldset.querySelector('.field-selects').innerHTML=t?c.rule.requiredFields.map(f=>`<label>${e(f.id)} · ${e(f.valueType)}<select data-field="${e(f.id)}"><option value="">请选择字段</option>${t.headers.map(h=>`<option value="${e(h)}" ${((m?.sources?.[0]?.fields||{})[f.id]||f.id)===h?'selected':''}>${e(h)}</option>`).join('')}</select></label>`).join(''):'';}
  async function saveMapping(){let mappings={};if($('useAdvanced').checked)mappings=JSON.parse($('advancedMapping').value);else document.querySelectorAll('.mapping-field').forEach(f=>{const index=f.querySelector('.table-select').value;if(index==='')return;const t=state.detail.tables[index];const fields=Object.fromEntries([...f.querySelectorAll('[data-field]')].map(s=>[s.dataset.field,s.value]));if(Object.values(fields).some(v=>!v))throw Error('请完成全部标准字段映射');mappings[f.dataset.metric]={mode:'union',sources:[{attachmentId:t.attachmentId,sheet:t.sheet,fields}],coverage:{evidence:f.querySelector('.coverage-evidence').value,complete:f.querySelector('.coverage-complete').checked}};});await update({mappings},{actor:$('mappingActor').value,reason:$('mappingReason').value,source:'USER_RULE'});close();toast('映射已保存；运行时会按当前会话全部数据检查实体键及数据有效性');}
  function renderComparison(c){const fraction=x=>x?`${e(x.numerator)} / ${e(x.denominator)}`:'—';return `<section class="run-card"><h3>${({year:'同比',period:'环比',trend:'趋势'})[c.kind]} · ${e(c.id.slice(-8))}</h3>${c.series.map(s=>`<div class="result-metric"><strong>${e(s.name)} · ${e(Object.values(s.group).join(' / ')||'总体')}</strong>${table(['统计窗口','结果','状态','来源运行'],s.points.map(p=>[p.window.join(' 至 '),p.displayValue??'—',p.reasonCode||p.outcome,p.runId]))}${c.kind==='trend'?trendChart(s):''}${s.difference?`<p>差值（精确）：${fraction(s.difference)} ${e(s.differenceUnit)}；变化率（%）：${fraction(s.relativeChangePercent)} ${e(s.rateReason||'')}</p>`:''}</div>`).join('')}${btn('comparisonExport','导出比较 JSON',c.id)}</section>`;}
  function trendChart(series) {
    const values=series.points.map(p=>p.outcome==='VALUE'&&p.displayValue!==null?Number(p.displayValue):null);
    const valid=values.filter(v=>v!==null&&Number.isFinite(v));
    if(!valid.length)return '<p class="muted">没有可绘制的数值点</p>';
    const lo=Math.min(...valid),hi=Math.max(...valid),range=hi-lo||1;
    const x=i=>50+i*500/Math.max(values.length-1,1),y=v=>150-(v-lo)*120/range;
    const paths=[];let segment=[];
    values.forEach((v,i)=>{if(v===null||!Number.isFinite(v)){if(segment.length)paths.push(segment);segment=[];}else segment.push(`${x(i)},${y(v)}`);});
    if(segment.length)paths.push(segment);
    return `<svg class="trend-chart" viewBox="0 0 600 190" role="img" aria-label="${e(series.name)}趋势图；精确数值见上表，缺失点不连接"><path d="M50 20V155H560" fill="none" stroke="#ccd0df"/>${paths.map(p=>`<polyline points="${p.join(' ')}" fill="none" stroke="#6858ed" stroke-width="2"/>`).join('')}${values.map((v,i)=>v!==null&&Number.isFinite(v)?`<circle cx="${x(i)}" cy="${y(v)}" r="4" fill="#6858ed"><title>${e(series.points[i].window.join(' 至 '))}：${e(series.points[i].displayValue)}</title></circle>`:'').join('')}<text x="50" y="180" font-size="13">${e(series.points[0].window[0])}</text><text x="560" y="180" text-anchor="end" font-size="13">${e(series.points.at(-1).window[0])}</text></svg>`;
  }
  async function showComparison(id){const runs=state.detail.runs.filter(r=>r.committed&&r.mode!=='PLAN'&&r.review?.status==='PASS');modal('比较已完成运行',`<p>先用同一冻结数据分别分析各窗口，再选择运行。同比需上年同窗，环比需相邻同粒度，趋势不插值；零基期和缺点单独标记。</p><select id="comparisonKind"><option value="period">环比</option><option value="year">同比</option><option value="trend">趋势</option></select>${runs.map(r=>`<label><input class="comparison-run" type="checkbox" value="${e(r.id)}" ${r.id===id?'checked':''}>${e(r.contract.parameters.date_start)} 至 ${e(r.contract.parameters.date_end)} · ${e(r.id.slice(-10))}</label>`).join('')}`,btn('comparisonSubmit','计算比较'));}
  function extractionResult(item){
    if(!item.definitionId)return '';
    const definition=state.detail?.metricDefinition?.id===item.definitionId?state.detail.metricDefinition:null;
    const job=latestDocumentParsing(item.definitionId);
    const isCatalog=job?.kind==='metric_catalog';
    let text='等待自动解析';
    if(job?.status==='QUEUED')text='等待自动提取';
    else if(job?.status==='RUNNING')text='正在自动提取';
    else if(job?.status==='SUCCEEDED')text=isCatalog?'指标目录与口径已提取，请查看待确认项':'解析已完成，请查看各项计算条件';
    else if(['FAILED','CANCELLED'].includes(job?.status))text='自动提取未完成';
    if(definition?.status==='PUBLISHED')text='解析就绪，已用于当前对话';
    if(!definition)return '<small>该说明已移除或更换，请查看当前指标说明。</small>';
    return `<div class="extraction-result"><small>指标说明 · ${e(text)}</small>${btn(isCatalog?'catalogView':'definition','查看说明中的指标',item.definitionId)}${jobFailure(job||{})}</div>`;
  }
  function importResult(result){
    const details=result?.acceptedDetails||[];
    const failures=result?.failures||[];
    if(!details.length&&!failures.length)return result?`<details><summary>处理结论</summary><pre>${json(result)}</pre></details>`:'';
    return `${details.length?`<details open><summary>文件识别结果（${details.length} 个）</summary><div class="import-results">${details.map(item=>`<div class="import-result"><strong>${e(item.name)}</strong><span>${label(item.role)} · ${label(item.confidence)}置信度 · ${label(item.status)}</span><small>${e(item.reason||'')} · ${e(label(item.kind))} · 证据 ${item.evidenceCount||0} 项</small>${extractionResult(item)}${item.dataSheets?.length?`<small>数据表：${e(item.dataSheets.join('、'))}</small>`:''}${item.ruleSheets?.length?`<small>规则表：${e(item.ruleSheets.join('、'))}</small>`:''}${item.reportSheets?.length?`<small>报告表：${e(item.reportSheets.join('、'))}</small>`:''}</div>`).join('')}</div></details>`:''}${failures.length?`<details open><summary>未导入文件（${failures.length} 个）</summary><div class="import-results">${failures.map(item=>`<div class="import-result import-failure"><strong>${e(item.name)}</strong><span>${e(item.code||'IMPORT_FAILED')}</span><small>${e(item.message||'导入失败')}</small></div>`).join('')}</div></details>`:''}`;
  }
  async function handle(action,id,el){
    if(action.startsWith('lib'))return library.handle(action,id,el);
    if(action==='openMetricLibrary'){await flushDraft();close();state.library=true;switchView();return refresh(true);}
    if(action==='bindLibraryVersion'){const selected=$('bindLibraryVersion').value;if(state.id)await update({definitionId:selected});else await newSession(selected);close();toast('已绑定指标版本，请核对字段映射');return;}
    if(action==='selectMetricVersion')return chooseLibraryDefinition();
    if(action==='pagePrevious'||action==='pageNext'){state.offset=Math.max(0,state.offset+(action==='pageNext'?30:-30));return refresh(true);}
    if(action==='compare')return showComparison(id);
    if(action==='comparisonSubmit'){const runIds=[...document.querySelectorAll('.comparison-run:checked')].map(x=>x.value);await api('job.queue',{kind:'comparison',scope:state.id,payload:{runIds,kind:$('comparisonKind').value}});close();toast('比较结果正在生成');return;}
    if(action==='comparisonExport'){state.bridge.saveComparison(id);return;}
    if(action==='deleteSession'){modal('永久删除数据对话',`<p>将删除本会话的消息、运行、检查点和导出记录。共享定义与原件由引用清理另行管理。</p><p>请输入“确认删除”继续：</p><input id="deleteConfirmation" aria-label="输入确认删除" placeholder="确认删除" autocomplete="off">`,btn('deleteSessionConfirm','永久删除'));return;}
    if(action==='deleteSessionConfirm'){await api('session.delete',{session_id:state.id,revision:state.detail.session.revision,confirmation:$('deleteConfirmation').value});state.id=null;state.detail=null;state.detailStamp='';state.bridge.setContext('');close();await refresh(true);if(state.boot.sessions[0])await selectSession(state.boot.sessions[0].id);else await newSession();return;}
    if(action==='clearConversation'){modal('清除对话内容','<p>只会删除本会话中的用户问题、Agent 回复和输入框草稿。已上传文件、指标定义、字段映射、分析运行结果和处理记录会保留。</p><label><input id="clearConversationAcknowledged" type="checkbox">我确认清除本会话的对话内容</label>',btn('clearConversationConfirm','清除对话内容'));return;}
    if(action==='clearConversationConfirm'){
      if(!$('clearConversationAcknowledged').checked)throw Error('请先确认清除对话内容');
      clearTimeout(state.draftTimer);
      await flushDraft();
      const cleared=await api('session.clear',{session_id:state.id,revision:state.detail.session.revision});
      state.lastClearedAt=new Date().toISOString();
      if(state.detail){state.detail.session=cleared;state.detail.messages=[];state.detailStamp='';renderSession();}
      close();
      $('prompt').value='';
      await refresh(true);
      toast('对话内容已清除');
      return;
    }
    if(action==='session')return selectSession(id);
    if(action==='definition')return showDefinition(id);
    if(action==='parseMetricSpec')return parseMetricSpec();
    if(action==='catalogView')return showMetricCatalog();
    if(action==='chooseDefinition')return $('viewVersion').onclick();
    if(action==='preview'){const p=await api('attachment.preview',{attachment_id:id,session_id:state.id});modal(p.attachment.name,routeEvidence(p.attachment)+(p.sheets?p.sheets.map(s=>`<h3>${e(s.name)} · ${s.rowCount} 行</h3>${table(s.headers,s.rows.map(r=>s.headers.map(h=>typeof r.values[h]==='object'?JSON.stringify(r.values[h]):r.values[h])))}`).join(''):p.nodes.map(n=>`<div class="source-node"><code>${e(n.locator)}</code><p>${e(n.text)}</p></div>`).join('')),btn('original','打开托管原件',id));return;}
    if(action==='routeInfo'){const attachment=state.detail?.attachments.find(a=>a.id===id);if(attachment)return showRouteInfo(attachment);return;}
    if(action==='original'){state.bridge.openOriginal(id);return;}
    if(action==='route'){const attachment=state.detail?.attachments.find(a=>a.id===id);const kind=attachment?.profile?.kind;const mixed=attachment?.route?.role==='MIXED';const options=[kind==='workbook'&&!mixed?'<option value="DATA_FILE">数据文件（Excel）</option>':'',kind==='document'&&!mixed?'<option value="METRIC_DEFINITION">指标说明（Word）</option>':'','<option value="OTHER">其他 / 排除</option>'].join('');const selected=attachment?.route?.role&&['DATA_FILE','METRIC_DEFINITION','OTHER'].includes(attachment.route.role)&&(!mixed||attachment.route.role==='OTHER')?attachment.route.role:'OTHER';modal('确认文件用途',`${attachment?routeEvidence(attachment):''}<p>自动识别仅提供建议。混合文件必须拆分，不能强行作为单一用途接入。</p><select id="routeRole">${options}</select>`,btn('routeSave','确认',id));$('routeRole').value=selected;return;}
    if(action==='routeSave'){await api('attachment.route',{attachment_id:id,role:$('routeRole').value});close();await refresh(true);return;}
    if(action==='remove'){await update({attachmentIds:state.detail.session.attachmentIds.filter(x=>x!==id),mappings:{}});return;}
    if(action==='retryExtract'){await api('session.metrics.retry',{session_id:state.id});close();await refresh(true);toast('已重试解析，进度将在当前对话更新');return;}
    if(action==='saveMapping')return saveMapping();
    if(action==='copy'){state.bridge.copyText(state.detail.messages.find(m=>m.id===id).text);toast('已复制');return;}
    if(action==='feedback'){const m=state.detail.messages.find(m=>m.id===id);const helpful=el.dataset.value==='true';await api('message.feedback',{message_id:id,helpful:m.feedback===helpful?null:helpful});return refresh(true);}
    if(action==='explain'||action==='regenerate'||action==='recalculate')return submit({explain:'EXPLAIN',regenerate:'REGENERATE',recalculate:'RECALCULATE_FROZEN'}[action],id);
    if(action==='retry'){const run=state.detail.runs.find(r=>r.id===id);return submit(run.mode,null,run.contract?.parameters?.scope==='ALL_DATA'?{scope:'ALL_DATA'}:{date_start:run.contract.parameters.date_start,date_end:run.contract.parameters.date_end});}
    if(action==='formula')return submit('FORMULA_INSPECT',id);
    if(action==='followup')return submit('ANALYZE',id);
    if(action==='query'){const r=state.detail.runs.find(r=>r.id===id);modal('筛选已有结果',`<p>引用 ${e(id)}。只筛选、排序已有分组；变更日期或分组维度请重新分析。</p><label>指标<select id="queryMetric">${r.results.map(m=>`<option value="${e(m.metricId)}">${e(m.name)}</option>`).join('')}</select></label><label>排序<select id="querySort"><option value="source">原结果顺序</option><option value="asc">数值从小到大</option><option value="desc">数值从大到小</option></select></label><label>跳过条数<input id="queryOffset" type="number" value="0" min="0"></label><label>本页条数<input id="queryLimit" type="number" value="20" min="1" max="100"></label><label>分组等值筛选 JSON（只允许已有维度）<textarea id="queryFilters" aria-label="分组筛选">{}</textarea></label>`,btn('querySubmit','查询',id));return;}
    if(action==='querySubmit'){const sort=$('querySort').value;const parameters={metricIds:[$('queryMetric').value],groupFilters:JSON.parse($('queryFilters').value),sortBy:sort==='source'?'source':'value',direction:sort==='desc'?'desc':'asc',offset:Number($('queryOffset').value),limit:Number($('queryLimit').value)};close();return submit('RESULT_QUERY',id,parameters);}
    if(action==='openSettings'){state.bridge.settings();return;}
    if(action==='runDetail'){const context=await api('run.context',{session_id:state.id,run_id:id});modal('运行合同与处理清单',`<pre>${json(context)}</pre>`);return;}
    if(action==='evidence'){const r=state.detail.runs.find(r=>r.id===id);const metric=r.results.find(m=>m.metricId===el.dataset.metric);modal(metric.name,`<pre>${json(metric)}</pre>`);return;}
    if(action==='export'){await api('job.queue',{kind:'export',scope:state.id,payload:{runId:id,format:el.dataset.format}});toast('导出文件正在生成，完成后可在结果下方另存');return;}
    if(action==='artifactSave'){state.bridge.saveArtifact(id);return;}
    if(action==='artifactPreview'){state.bridge.openArtifact(id,true);return;}
    if(action==='artifactApprove'){modal('页面核验','<p>请先打开页面预览，检查全部页面的中文字体、表格跨页、边界、页码与空白页。</p><label>检查人<input id="layoutActor"></label><label><input id="layoutReviewed" type="checkbox">我已逐页核对并确认排版合格</label>',btn('artifactConfirm','确认核验',id));return;}
    if(action==='artifactConfirm'){if(!$('layoutReviewed').checked)throw Error('请先完成页面检查');await api('artifact.confirm',{artifact_id:id,actor:$('layoutActor').value});close();return refresh(true);}
    if(action==='rename'){modal('重命名对话',`<label>对话标题<input id="newName" maxlength="120" value="${e(state.detail.session.name)}"></label>`,btn('renameSave','保存'));return;}
    if(action==='renameSave'){await update({name:$('newName').value});close();return;}
    if(action==='archive'){await update({archived:!state.detail.session.archived});close();return;}
  }
  document.addEventListener('click',event=>{const b=event.target.closest('[data-action]');if(b)guarded(()=>handle(b.dataset.action,b.dataset.id,b));});
  document.addEventListener('change',event=>{if(event.target.matches('.table-select'))renderFieldSelects(event.target.closest('.mapping-field'));});
  $('openNew').onclick=$('emptyNew').onclick=()=>guarded(()=>newSession());
  $('navChat').onclick=()=>{state.library=false;switchView();};
  $('navLibrary').onclick=()=>guarded(async()=>{await flushDraft();state.library=true;switchView();await refresh(true);});
  $('librarySearch').oninput=library.filter;
  $('libraryVersionFilter').onchange=library.filter;
  $('manualMetric').onclick=()=>library.form();
  $('uploadVersion').onclick=()=>{state.bridge.setContext('library');state.bridge.pickFiles();};
  $('showArchive').onclick=()=>{state.archived=!state.archived;state.offset=0;refresh(true);};$('search').oninput=()=>{clearTimeout(state.searchTimer);state.searchTimer=setTimeout(()=>{state.offset=0;refresh(true);},250);};
  $('workspaceInfo').onclick=()=>state.bridge?.settings();
  $('closeModal').onclick=close;$('overlay').onclick=ev=>{if(ev.target===$('overlay'))close();};
  const pickForSession=()=>guarded(async()=>{if(!state.id)await newSession();state.bridge.pickFiles();});
  $('attachBtn').onclick=$('dropzone').onclick=$('metricUpload').onclick=pickForSession;
  $('metricParse').onclick=()=>guarded(()=>state.detail?.metricDefinition?.catalogSummary?.status==='READY'||['QUEUED','RUNNING'].includes(latestCatalogJob(state.detail?.metricDefinition?.id)?.status)?showMetricCatalog():parseMetricSpec());
  $('sendBtn').onclick=()=>guarded(()=>submit());$('planBtn').onclick=()=>guarded(()=>submit('PLAN'));$('analyzeBtn').onclick=()=>guarded(()=>submit('ANALYZE'));
  document.addEventListener('click',event=>{const starter=event.target.closest('[data-prompt]');if(starter){$('prompt').value=starter.dataset.prompt||'';$('prompt').focus();$('prompt').dispatchEvent(new Event('input',{bubbles:true}));}});
  $('mappingBtn').onclick=()=>guarded(showMapping);
  $('viewVersion').onclick=()=>guarded(()=>showDefinition());
  $('moreBtn').onclick=()=>{if(state.id)modal('会话操作','<p>可重命名、归档、清除当前会话或永久删除数据对话。</p>',btn('rename','重命名')+btn('archive',state.detail.session.archived?'恢复归档':'归档对话')+(state.detail.session.archived?'':btn('clearConversation','清除对话内容'))+btn('deleteSession','永久删除数据对话'));};
  $('toggleContext').onclick=()=>{$('app').classList.toggle('context-collapsed');$('toggleContext').setAttribute('aria-expanded',String(!$('app').classList.contains('context-collapsed')));guarded(()=>api('settings.save',{values:{contextCollapsed:$('app').classList.contains('context-collapsed')}}));};
  $('infoBtn').onclick=()=>modal('使用工作台','<ol class="guide"><li>新建独立数据对话，导入 Excel 与 Word，支持文件选择、拖放及 Ctrl+V。</li><li>等待说明文档自动解析，查看指标及无法计算的原因。</li><li>也可进入左侧“指标管理”手工新增指标，无需 Word 或模型。填写口径并复核发布后，点击“新建对话使用”；已有准备中对话可在右侧选择已发布版本。上传 Excel 并确认字段映射后即可计算。</li><li>在输入框直接描述目标，例如“统计 2026-10-01 至 2026-11-01 的手术指标并汇总异常”。Agent 会识别资料查询、计划、分析、解释或结果筛选。</li><li>首次执行型对话会冻结数据基线；查看资料和帮助不会锁定。</li><li>查看结果、跳过原因和证据。需要新数据时请新建独立数据对话并重新选择资料。已发布口径通过创建新版本维护。</li></ol><p>Key 在原生设置中填写。未配置模型仍可查看历史和进行批准规则的本地计算。旧 DOC 可由内置 Python 解析为文本；缺少 LibreOffice 时仅禁用 PDF 页面渲染，DOCX 报告保留为未核验草稿。</p>');
  $('prompt').oncompositionstart=()=>state.composing=true;$('prompt').oncompositionend=()=>state.composing=false;
  $('prompt').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing&&!state.composing){event.preventDefault();if(!$('sendBtn').disabled)guarded(()=>submit());}};
  $('prompt').oninput=()=>{clearTimeout(state.draftTimer);state.draftTimer=setTimeout(()=>guarded(flushDraft),700);};
  // Native Qt handles file clipboard imports. Plain text stays on the browser's
  // native paste path so it is inserted exactly once.
  $('prompt').addEventListener('paste',event=>{
    const items=[...(event.clipboardData?.items||[])];
    if(items.some(item=>item.kind==='file'))event.preventDefault();
    if(event.defaultPrevented)return;
    const text=event.clipboardData?.getData('text/plain')||'';
    event.preventDefault();
    const now=Date.now();
    const previous=state.lastTextPaste;
    if(previous&&previous.text===text&&now-previous.at<300)return;
    state.lastTextPaste={text,at:now};
    if(document.activeElement===$('prompt'))document.execCommand('insertText',false,text);
    setTimeout(()=>{if(state.lastTextPaste?.at===now)state.lastTextPaste=null;},350);
  });
  document.addEventListener('focusin',event=>state.bridge?.allowFilePaste(event.target===$('prompt')||event.target===$('dropzone')||event.target===$('metricUpload')));
  document.addEventListener('keydown',event=>{if(event.key==='Escape')close();if(event.key==='Tab'&&$('overlay').classList.contains('show')){const items=[...$('overlay').querySelectorAll('button:not(:disabled),input,textarea,select,summary')].filter(x=>x.offsetParent);if(event.shiftKey&&document.activeElement===items[0]){event.preventDefault();items.at(-1)?.focus();}else if(!event.shiftKey&&document.activeElement===items.at(-1)){event.preventDefault();items[0]?.focus();}}});
  window.appActions={newSession:()=>guarded(newSession),refresh:()=>refresh(true),selectSession,
    noteFilePaste:()=>{},
    noteTextPasteShortcut:()=>{},
    pasteText:text=>{if(document.activeElement===$('prompt'))document.execCommand('insertText',false,text);}};
  if(typeof QWebChannel==='undefined'||typeof qt==='undefined'){toast('请通过 start.bat 启动 Python 桌面工作台');return;}
  new QWebChannel(qt.webChannelTransport,channel=>{state.bridge=channel.objects.desktop;state.bridge.changed.connect(()=>refresh());state.bridge.notice.connect(raw=>{const n=JSON.parse(raw);toast(n.message);if(n.code==='NEW_SESSION')guarded(()=>selectSession(n.sessionId));else refresh(true);});guarded(async()=>{await refresh(true);if(state.boot.sessions.length)await selectSession(state.boot.sessions[0].id);});});
})();
