/* Three-source conversation and manually authored metric views. */
window.createV2UI = ({state, api, e, toast, refresh, modal, close}) => {
  const $ = id => document.getElementById(id);
  const sourceName = {FIRST_SURGERY:'首台手术信息', CANCELLATION:'手术室退单信息', SURGERY_DETAIL:'手术明细查询'};
  const unitName = {OR_1:'一号手术室',OR_2:'二号手术室',OR_3:'三号手术室'};
  let metrics = [];
  let metricJobs = [];
  const button = (action, label, id='', extra='') => `<button class="soft-action" data-v2-action="${action}" data-v2-id="${e(id)}" ${extra}>${e(label)}</button>`;
  const capture = async fn => {try {await fn(); await refresh(true);} catch(error) {toast(error.message);} };
  function renderSession(detail) {
    const session = detail.session;
    $('chatName').textContent = session.name;
    $('sessionCode').textContent = '#' + session.id.slice(-10);
    const sources = detail.sources.sources;
    const revision = detail.sources.revision;
    const importJobs = (detail.activities?.jobs||[]).filter(job=>job.kind==='import_v2').slice(-5).reverse();
    $('chatTags').innerHTML = `<span class="pill">三类固定数据</span><span class="pill">${sources.filter(x=>x.current).length} 份已入库</span>${revision?`<span class="pill">数据版本 ${revision.revision_no}</span>`:''}`;
    $('conversationGuidance').textContent = sources.length ? '当前对话数据已就绪' : '等待上传数据';
    $('composeInfo').textContent = `${sources.filter(x=>x.current).reduce((n,x)=>n+x.row_count,0)} 行来源记录`;
    $('prompt').placeholder = '例如：按日期统计退单来源记录行数';
    $('sendBtn').disabled = session.archived;
    $('chatFeed').innerHTML = detail.messages.map(message=>`<article class="msg ${message.role==='user'?'user':''}"><div class="avatar">${message.role==='user'?'我':'✳'}</div><div class="msg-body"><div class="msg-author">${message.role==='user'?'本机使用者':'Insight Agent'}</div><div class="message-text">${e(message.text)}</div></div></article>`).join('') + detail.runs.map(run=>{
      if(run.status!=='COMPLETED') {const candidates=run.error?.details?.candidates||[];return `<section class="run-card"><strong>${e(run.status==='QUEUED'?'查询排队中':run.status==='WAITING_INPUT'?'需要补充信息':'查询未完成')}</strong>${run.error?`<p>${e(run.error.message)}</p>`:''}${candidates.map(id=>{const metric=metrics.find(item=>item.id===id);return button('resolveMetric',metric?.version?.name||id,run.id,`data-metric="${e(id)}"`)}).join('')}</section>`;}
      const rows = run.result.rows;
      const columns = rows.length ? Object.keys(rows[0]) : [];
      return `<section class="run-card"><div class="run-heading"><strong>${e(run.result.metricName||run.result.label)}</strong><span class="pill">${e(run.result.unit)}</span></div><div class="table-scroll"><table class="real-table"><thead><tr>${columns.map(c=>`<th>${e(c==='source_row_count'?'来源记录行数':c)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${columns.map(c=>`<td>${e(row[c])}</td>`).join('')}</tr>`).join('')}</tbody></table></div><details><summary>查询依据</summary><p>数据版本 ${e(run.dataRevisionId)} · 知识库 ${e(run.result.releaseId)}</p><pre>${e(run.result.sql)}</pre></details></section>`;
    }).join('') || '<div class="empty-state"><h2>当前对话</h2><p>上传三类手术数据后，在下方直接查询。</p></div>';
    const list = sources.map(source=>`<div class="v2-source"><strong>${e(source.file_name)}</strong><small>${e(sourceName[source.source_type])} · ${e(source.row_count)} 行 · ${e(source.state==='READY'?source.current?'已入库':'历史版本':'待选择手术室')}${source.origin_operating_unit?' · '+e(unitName[source.origin_operating_unit]):''}</small>${source.state==='NEEDS_UNIT_CONFIRMATION'?`<div class="v2-unit-actions">${Object.entries(unitName).map(([code,name])=>button('confirmUnit',name,source.id,`data-unit="${code}"`)).join('')}</div>`:source.source_type==='CANCELLATION'&&source.current?button('correctUnit','更正手术室',source.id):''}</div>`).join('');
    $('v2Inspector').innerHTML = `<div class="inspector-head"><div class="inspector-title">当前对话的数据</div><div class="inspector-sub">已入库文件仅供本对话查询</div></div><div class="side-panel"><div class="panel-title">来源文件</div>${list||'<p class="muted">尚未上传工作簿</p>'}<button class="dropzone compact-drop" data-v2-action="upload">＋ 上传 XLSX</button>${importJobs.map(job=>`<p class="v2-job">导入：${e(job.status)}${job.error?` · ${e(job.error.message)}`:''}${(job.result?.failures||[]).map(item=>`<br>${e(item.name)}：${e(item.message)}`).join('')}</p>`).join('')}</div>`;
  }
  async function renderLibrary() {
    metrics = await api('metricV2.list');
    metricJobs = (await api('job.list')).filter(job=>job.kind==='metric_sql_v2');
    $('v2Metrics').innerHTML = `<div class="lib-top"><div><h1>指标管理</h1><p class="lib-intro">名称与自然语言口径由人工维护，SQL 经本地知识库校验后发布。</p></div><button class="purple-action" data-v2-action="newMetric">新增指标</button></div><div class="v2-metric-list">${metrics.map(metric=>{
      const v=metric.version;
      const latestJob=metricJobs.find(job=>job.scope===metric.id);
      return `<article class="v2-metric"><div><strong>${e(v.name)}</strong><span class="pill">${e(v.status)}</span><p>${e(v.description)}</p><small>版本 ${e(v.version_no)} · ${metric.activeVersionId?'已有发布版':'未发布'}</small>${latestJob?`<small class="v2-job">SQL 生成：${e(latestJob.status)}${latestJob.error?' · '+e(latestJob.error.message):''}</small>`:''}</div><div>${button('viewMetric','查看',metric.id)}${button('editMetric','编辑',metric.id)}${button('deleteMetric','删除',metric.id)}</div></article>`;
    }).join('')||'<p class="muted">还没有手动指标</p>'}</div>`;
  }
  function editForm(metric=null) {
    const v=metric?.version||{};
    modal(metric?'编辑指标':'新增指标',`<label class="v2-field">名称<input id="v2Name" maxlength="120" value="${e(v.name||'')}"></label><label class="v2-field">自然语言口径<textarea id="v2Description" rows="7">${e(v.description||'')}</textarea></label>`,button(metric?'saveMetric':'createMetric','保存',metric?.id||''));
  }
  async function viewMetric(id) {
    const metric=await api('metricV2.get',{metric_id:id}); const v=metric.version;
    const history=(metric.versions||[]).map(version=>`<div class="v2-version"><strong>v${e(version.version_no)} · ${e(version.status)}${version.id===metric.activeVersionId?' · 当前执行版':''}</strong><p>${e(version.description)}</p>${version.sql_template?`<details><summary>执行 SQL</summary><pre>${e(version.sql_template)}</pre></details>`:''}</div>`).join('');
    modal(v.name,`<p>${e(v.description)}</p><p>状态：${e(v.status)} · 版本 ${e(v.version_no)} · 草稿修订 ${e(v.draft_revision)}</p>${v.validation?.reason?`<p>校验缺口：${e(v.validation.reason)}</p>`:''}${v.ai_sql_draft?`<details><summary>AI 提议 SQL（仅供审阅）</summary><pre>${e(v.ai_sql_draft)}</pre></details>`:''}${v.sql_template?`<details open><summary>本地编译 SQL（实际执行）</summary><pre>${e(v.sql_template)}</pre></details>`:''}${v.plan?`<details><summary>闭合取数计划</summary><pre>${e(JSON.stringify(v.plan,null,2))}</pre></details>`:''}<details><summary>版本记录（${metric.versions.length}）</summary>${history}</details>`,`${v.status!=='PUBLISHED'?button('generateSql','AI 生成 SQL',id,`data-revision="${v.draft_revision}"`):''}${v.status==='VALIDATED'&&state.detail?.session?.schemaVersion===2?button('previewMetric','预览当前对话结果',id):''}${v.status==='VALIDATED'?button('publishMetric','审核发布',id,`data-revision="${v.draft_revision}"`):''}`);
  }
  document.addEventListener('click', event=>{
    const target=event.target.closest('[data-v2-action]'); if(!target)return;
    const action=target.dataset.v2Action,id=target.dataset.v2Id;
    if(action==='upload')return $('attachBtn').click();
    if(action==='newMetric')return editForm();
    if(action==='editMetric')return capture(async()=>editForm(await api('metricV2.get',{metric_id:id})));
    if(action==='viewMetric')return capture(async()=>viewMetric(id));
    if(action==='createMetric'||action==='saveMetric')return capture(async()=>{
      const name=$('v2Name').value.trim(),description=$('v2Description').value.trim();
      if(action==='createMetric')await api('metricV2.create',{name,description});
      else {const metric=await api('metricV2.get',{metric_id:id});await api('metricV2.edit',{metric_id:id,name,description,expected_revision:metric.version.draft_revision});}
      close();toast('指标草稿已保存');
    });
    if(action==='deleteMetric')return capture(async()=>{if(!confirm('删除此指标？历史版本仍保留。'))return;await api('metricV2.delete',{metric_id:id});toast('指标已删除');});
    if(action==='generateSql')return capture(async()=>{await api('metricV2.generateSql',{metric_id:id,expected_revision:Number(target.dataset.revision)});close();toast('SQL 生成任务已进入队列');});
    if(action==='previewMetric')return capture(async()=>{const result=await api('metricV2.preview',{session_id:state.id,metric_id:id});modal('当前对话预览',`<p>数据版本 ${e(result.revisionId)} · ${e(result.unit)}</p><pre>${e(JSON.stringify(result.rows,null,2))}</pre>`);});
    if(action==='publishMetric')return capture(async()=>{const actor=prompt('复核人姓名');if(!actor)return;await api('metricV2.publish',{metric_id:id,expected_revision:Number(target.dataset.revision),actor,reviewed:true});await viewMetric(id);});
    if(action==='confirmUnit')return capture(async()=>{await api('source.confirmUnit',{session_id:state.id,source_id:id,unit_code:target.dataset.unit});toast('退单来源手术室已确认');});
    if(action==='correctUnit')return capture(async()=>{const chosen=prompt('输入手术室编号：1、2 或 3');if(!chosen)return;const unit_code={1:'OR_1',2:'OR_2',3:'OR_3'}[chosen];if(!unit_code)throw Error('请输入 1、2 或 3');await api('source.correctUnit',{session_id:state.id,source_id:id,unit_code});toast('已创建更正后的数据版本');});
    if(action==='resolveMetric')return capture(async()=>{await api('queryV2.resolveMetric',{session_id:state.id,run_id:id,metric_id:target.dataset.metric});toast('已按所选指标继续查询');});
  });
  return {renderSession,renderLibrary};
};
