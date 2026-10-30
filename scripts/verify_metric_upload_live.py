"""Verify the original upload using the configured model in an isolated store."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from insight_agent.service import AppService
from insight_agent.worker import run_once


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('document', type=Path)
    parser.add_argument('--max-output-tokens', type=int, help='Override output ceiling only in the isolated verification store')
    args = parser.parse_args()
    live = Path(os.environ['LOCALAPPDATA']) / 'DataMind' / 'datamind.sqlite3'
    with sqlite3.connect(live.as_uri() + '?mode=ro', uri=True) as db:
        profiles = [json.loads(row[0]) | {'id': row[1]} for row in db.execute(
            "SELECT data,id FROM objects WHERE kind='model'")]
    profile = next(p for p in profiles if p.get('default') and p.get('enabled'))
    if args.max_output_tokens is not None:
        assert 256 <= args.max_output_tokens <= 1000000
        profile['maxOutputTokens'] = args.max_output_tokens
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='DataMind-live-catalog-') as folder:
        service = AppService(Path(folder) / 'workspace')
        # Copy the credential reference; the secret stays in Windows Vault.
        with service.store.transaction() as db:
            service.store.put('model', profile, db)
        session = service.create_session('指标说明上传回归验证')
        imported = service.import_paths([str(args.document)], session['id'])
        assert run_once(service)
        job = next(j for j in service.store.jobs() if j['id'] == imported['jobId'])
        assert job['status'] == 'SUCCEEDED', job.get('error')
        queued = [j for j in service.store.jobs() if j['status'] == 'QUEUED']
        assert len(queued) == 1 and queued[0]['kind'] == 'metric_catalog'
        print(json.dumps({'phase': 'online_catalog_started', 'model': profile['model'],
                          'nodes': len(service.session_metrics(session['id'])['nodes'])}, ensure_ascii=False), flush=True)
        assert run_once(service)
        result = next(j for j in service.store.jobs() if j['kind'] == 'metric_catalog')
        parsed = service.session_metric_catalog(session['id'])
        report = {'status': result['status'], 'error': result['error'], 'model': profile['model'],
                  'configuredOutputTokens': profile['maxOutputTokens'],
                  'elapsedSeconds': round(time.monotonic() - started, 2),
                  'catalogStatus': parsed['status'] if parsed else None,
                  'metricCount': len(parsed['items']) if parsed else 0,
                  'completed': sum(i['status'] != 'PENDING' for i in parsed['items']) if parsed else 0,
                  'directoryNodes': len(parsed.get('directoryLocators', [])) if parsed else 0,
                  'calls': [{k: c.get(k) for k in ('stage', 'status', 'errorCode', 'elapsedSeconds', 'finishReason', 'usage',
                                                 'completion_tokens_details', 'requestOptions',
                                                 'configuredOutputTokens', 'effectiveOutputTokens')}
                            for c in reversed(service.store.list('model_call'))]}
        output = ROOT / 'artifacts' / 'acceptance'
        output.mkdir(parents=True, exist_ok=True)
        (output / 'metric-upload-live-verification.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if parsed:
            (output / 'metric-upload-live-catalog.json').write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)
        assert result['status'] == 'SUCCEEDED', result['error']


if __name__ == '__main__':
    main()
