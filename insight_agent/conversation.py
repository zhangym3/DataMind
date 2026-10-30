"""Local conversation actions and explicit, same-session history references."""
from fractions import Fraction

from .core import digest, require
from .data import catalog


LOCAL_COMMANDS = {
    '帮助': 'HELP', '/help': 'HELP', '查看附件': 'ATTACHMENT_STATUS',
    '/files': 'ATTACHMENT_STATUS', '查看数据': 'DATASET_INSPECT', '/data': 'DATASET_INSPECT',
    '查看口径': 'DEFINITION_INSPECT', '/definition': 'DEFINITION_INSPECT',
    '解析指标说明': 'METRIC_SPEC_PARSE', '提取指标': 'METRIC_SPEC_PARSE', '/parse-metrics': 'METRIC_SPEC_PARSE',
    '查看设置': 'SETTINGS_ACTION', '/settings': 'SETTINGS_ACTION',
}
LOCAL_ACTIONS = set(LOCAL_COMMANDS.values())

# Natural-language preparation requests that are deterministic and do not need
# an online model. More specific analysis/result words are checked first so a
# request such as "分析数据" is never mistaken for a data catalogue query.
def infer_local_action(text):
    normalized = ' '.join((text or '').strip().split())
    if not normalized:
        return None
    if normalized in ('请解析指标说明', '解析说明文档', '提取指标说明中的指标', '请提取指标'):
        return 'METRIC_SPEC_PARSE'
    if any(word in normalized for word in ('分析', '统计', '汇总', '计算', '查询结果', '最高', '最低', '排序', '总结')):
        return None
    if normalized in ('帮助', '怎么用', '如何使用', '能做什么') or any(word in normalized for word in ('使用说明', '功能介绍')):
        return 'HELP'
    if any(word in normalized for word in ('模型设置', '模型配置', '设置', '凭据')):
        return 'SETTINGS_ACTION'
    if any(word in normalized for word in ('指标定义', '指标口径', '口径', '定义库', '公式')):
        return 'DEFINITION_INSPECT'
    if any(word in normalized for word in ('数据表', '数据集', '字段', '表头', '行数')):
        return 'DATASET_INSPECT'
    if any(word in normalized for word in ('附件', '文件', '资料', '导入了什么', '有哪些数据')):
        return 'ATTACHMENT_STATUS'
    return None


def latest_committed_run(store, session):
    """Return the newest committed analysis run in this session, if any."""
    runs = [r for r in store.list('run') if r.get('sessionId') == session['id'] and r.get('committed') and r.get('mode') != 'PLAN']
    return runs[0] if runs else None


def resolve_history(store, session, turn, action, parameters):
    """Inherit only from a user-selected run; never guess 'last month' or a run."""
    source = None
    source_id = turn.get('sourceRunId')
    explicit_source_id = source_id
    referenced = action in ('EXPLAIN', 'SUMMARY', 'REGENERATE', 'RECALCULATE_FROZEN', 'RESULT_QUERY', 'FORMULA_INSPECT')
    if referenced and not source_id and action in ('EXPLAIN', 'SUMMARY', 'RESULT_QUERY'):
        # A plain-language follow-up such as "总结本次分析" has no run id in
        # the prompt. Resolve it to the newest committed run in this session;
        # an explicit source id still takes precedence and is scope checked.
        source = latest_committed_run(store, session)
        source_id = source['id'] if source else None
    require(not referenced or source_id, 'AMBIGUOUS_RESULT_REFERENCE', '请在分析结果上选择要引用的运行')
    if source_id:
        source = source or store.get('run', source_id)
        require(source['sessionId'] == session['id'] and source.get('committed'), 'SCOPE_FORBIDDEN', '不能引用其他会话或未提交的运行')
    inherited, omitted = {}, {}
    if source and action in ('PLAN', 'ANALYZE'):
        require(source['contract']['definitionId'] == session['definitionId'], 'CONTRACT_MISMATCH', '不能从不同口径版本继承参数')
        allowed = {p['name'] for c in source['contract']['candidates'] if c['status'] == 'CLEAR' for p in c['rule']['allowedQueryParameters']}
        for key, value in source['contract']['parameters'].items():
            if parameters.get('scope') == 'ALL_DATA' and key in ('date_start', 'date_end'):
                omitted[key] = 'ALL_DATA_SCOPE'
                continue
            if key in parameters:
                omitted[key] = 'EXPLICIT_PARAMETER'
            elif key in allowed:
                inherited[key] = value
            else:
                omitted[key] = 'NOT_ALLOWED'
    history = {
        'schemaVersion': 'history-context/1.0', 'sessionId': session['id'], 'turnId': turn['id'],
        'sourceRunId': source_id, 'explicitReference': bool(explicit_source_id),
        'automaticReference': bool(source_id and not explicit_source_id),
        'sourceContractHash': source['contractHash'] if source else None,
        'sourceResultDigest': source.get('resultDigest') if source else None,
        'inheritedParameters': inherited, 'explicitParameters': parameters, 'notInherited': omitted,
        'messageRefs': [], 'historyTokens': 0, 'outboundPolicy': 'NO_RAW_HISTORY',
    }
    history['sha256'] = digest(history)
    return source, inherited | parameters, history


def local_reply(store, session, action):
    if action == 'HELP':
        return ('在当前对话导入 Word 指标说明和 Excel 数据，等待自动解析后计算说明中的指标值。字段同名时自动匹配，无法计算的指标会列出原因；字段不一致时可确认字段映射。'
                '\n仅需提取指标时，上传Word后点击“解析指标说明”或输入“解析指标说明”，使用在线模型整理清单，无需Excel。'
                '\n本地命令：查看附件 / 查看数据 / 查看口径 / 查看设置 / 帮助。无需在线模型，不锁定会话。'
                '\n在已完成分析上可筛选结果、查看公式、解释或按原合同重算。自然语言分析和解释需要在线模型。')
    if action == 'ATTACHMENT_STATUS':
        items = [store.get('attachment', key) for key in session['attachmentIds']]
        return '\n'.join(f"{a['name']}：{a['route']['role']} / {a['status']}；{a['route']['reason']}" for a in items) or '当前会话尚无附件。请通过选择、拖放或 Ctrl+V 导入。'
    if action == 'DATASET_INSPECT':
        tables = catalog(store, session)
        return '\n'.join(f"{t['fileName']} / {t['sheet']}：{t['rowCount']} 行；字段：{'、'.join(t['headers'])}" for t in tables) or '当前没有用途已确认的数据表；请先导入并确认文件用途。'
    if action == 'DEFINITION_INSPECT':
        if not session['definitionId']:
            return '请上传当前对话的 Word 指标说明，系统会自动解析其中的指标。'
        definition = store.get('definition', session['definitionId'])
        status = '解析就绪' if definition['status'] == 'PUBLISHED' else '解析待完成'
        return f"{definition['name']} / {status}\n" + '\n'.join(f"{c['name']}：{'可计算' if c['status'] == 'CLEAR' else '无法计算'} {c.get('reason', '')}" for c in definition['candidates'])
    return '点击左下角“设置”查看模型服务、预算、界面与存储设置。请在原生 Key 输入框配置凭据，不要把 Key 输入对话。'


def material_context(store, session):
    """Build a bounded, non-row material catalogue for the online model."""
    attachments = [store.get('attachment', key) for key in session['attachmentIds']]
    definition = store.get('definition', session['definitionId']) if session.get('definitionId') else None
    nodes = []
    if definition:
        for node in definition.get('nodes', [])[:80]:
            nodes.append({
                'locator': node.get('locator', ''),
                'text': (node.get('text') or '')[:4000],
            })
    return {
        'attachments': [
            {'id': a['id'], 'name': a['name'], 'kind': a['profile']['kind'],
             'role': a['route']['role'], 'status': a['status']}
            for a in attachments
        ],
        'definition': {
            'id': definition['id'], 'name': definition['name'], 'status': definition['status'],
            'nodes': nodes,
            'metrics': [{'id': c['id'], 'name': c['name'], 'status': c['status']} for c in definition.get('candidates', [])],
        } if definition else None,
        'tables': [
            {'fileName': table['fileName'], 'sheet': table['sheet'], 'rowCount': table['rowCount'],
             'headers': table['headers'][:120]}
            for table in catalog(store, session)[:80]
        ],
    }


def material_references(context):
    references = set()
    for attachment in context['attachments']:
        references.update((attachment['id'], attachment['name']))
    definition = context.get('definition')
    if definition:
        references.update(node['locator'] for node in definition['nodes'] if node.get('locator'))
        references.update(metric['id'] for metric in definition['metrics'])
        references.add(definition['id'])
    references.update(f"{table['fileName']} / {table['sheet']}" for table in context['tables'])
    return references


def query_result(source, parameters):
    """Filter/sort existing groups only. No new aggregates or inferred dimensions."""
    require(source['mode'] != 'PLAN', 'RUN_NOT_ANALYSIS', '请选择已完成的分析结果')
    require(isinstance(parameters, dict) and set(parameters) <= {'metricIds', 'groupFilters', 'sortBy', 'direction', 'offset', 'limit'}, 'INVALID_QUERY', '结果查询只允许筛选、排序和分页；新日期或维度需重新分析')
    metrics = {r['metricId']: r for r in source['results']}
    selected = parameters.get('metricIds', list(metrics))
    require(isinstance(selected, list) and selected and all(isinstance(x, str) for x in selected) and set(selected) <= set(metrics) and len(selected) == len(set(selected)), 'INVALID_QUERY', '请选择源运行中存在的指标')
    filters = parameters.get('groupFilters', {})
    require(isinstance(filters, dict), 'INVALID_QUERY', '分组筛选必须为对象')
    sort, direction = parameters.get('sortBy', 'source'), parameters.get('direction', 'asc')
    require(sort in ('source', 'value') and direction in ('asc', 'desc'), 'INVALID_QUERY', '排序参数无效')
    require(sort != 'value' or len(selected) == 1, 'INVALID_QUERY', '数值排序需选择单一指标，不能跨单位排序')
    offset, limit = parameters.get('offset', 0), parameters.get('limit', 20)
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100, 'INVALID_QUERY', '分页范围无效，每页最多 100 条')
    rows, available = [], set()
    for metric_id in selected:
        result = metrics[metric_id]
        for index, group in enumerate(result.get('groups', [])):
            dimensions = group.get('group', {})
            available.update(dimensions)
            if all(dimensions.get(k) == v for k, v in filters.items()):
                rows.append({'metricId': metric_id, 'name': result['name'], 'unit': result.get('unit', ''),
                             'groupIndex': index, 'group': dimensions, 'outcome': group['outcome'],
                             'displayValue': group.get('displayValue'), 'reasonCode': group.get('reasonCode'),
                             'exactNumerator': group.get('exactNumerator'), 'exactDenominator': group.get('exactDenominator')})
    require(set(filters) <= available, 'INVALID_QUERY', '筛选字段不是源结果中已有的维度')
    if sort == 'value':
        values = [r for r in rows if r['outcome'] == 'VALUE']
        values.sort(key=lambda r: Fraction(int(r['exactNumerator']), int(r['exactDenominator'])), reverse=direction == 'desc')
        rows = values + [r for r in rows if r['outcome'] != 'VALUE']
    elif direction == 'desc':
        rows.reverse()
    result = {'sourceRunId': source['id'], 'sourceResultDigest': source['resultDigest'], 'parameters': parameters,
              'total': len(rows), 'offset': offset, 'limit': limit, 'rows': rows[offset:offset + limit]}
    lines = [f"引用运行 {source['id']}；匹配 {len(rows)} 组，本页 {len(result['rows'])} 组。"]
    lines += [f"{r['name']} / {r['group']}：{r['displayValue'] + ' ' + r['unit'] if r['outcome'] == 'VALUE' else '跳过 / ' + str(r['reasonCode'])}" for r in result['rows']]
    return result, '\n'.join(lines)


def formula_reply(source):
    import json
    require(source['mode'] != 'PLAN', 'RUN_NOT_ANALYSIS', '请选择已完成的分析结果')
    return '引用运行 ' + source['id'] + '\n以下为已冻结口径，数值及行数审计见该运行的处理清单。\n' + '\n'.join(
        c['name'] + '\n' + json.dumps(c['rule'], ensure_ascii=False, indent=2)
        for c in source['contract']['candidates'] if c['status'] == 'CLEAR')
