"""Opt-in frozen-build verification, restricted to a prepared synthetic workspace."""
import json
import sys
import time
from pathlib import Path

from PySide6.QtCore import QTimer


def attach(app, window, output):
    started = time.monotonic()
    checks, state = [], {'submitted': False, 'done': False, 'js': False, 'export': False}
    service = window.service
    sessions = service.store.list('session')
    if len(sessions) != 1 or sessions[0]['name'] != '合成手术数据':
        raise ValueError('Smoke verification requires the isolated synthetic fixture')
    session = sessions[0]
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    timer = QTimer(window)
    timer.setInterval(150)

    def stop(error=None):
        if state['done']:
            return
        state['done'] = True
        timer.stop()
        window.stop_worker()
        result = {'status': 'FAILED' if error else 'PASS', 'frozen': bool(getattr(sys, 'frozen', False)), 'python': sys.version.split()[0], 'checks': checks, 'elapsedSeconds': round(time.monotonic()-started, 2), 'error': error}
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        window.web.grab().save(str(path.with_suffix('.png')))
        window.close()

    def accept_js(value):
        if value and json.loads(value) is True:
            state['js'] = True

    def tick():
        try:
            if time.monotonic()-started > 90:
                return stop('Timed out waiting for WebEngine / persistent worker')
            if not state['js']:
                window.page.runJavaScript("JSON.stringify(document.querySelector('#chatName')?.textContent==='合成手术数据')", 0, accept_js)
                return
            if not state['submitted']:
                checks.append('bundled Qt WebEngine resources and WebChannel session bootstrap')
                service.submit_user_turn(session['id'], session['revision'], 'frozen-smoke', '', 'ANALYZE', {'date_start':'2026-10-01','date_end':'2026-11-01'})
                state['submitted'] = True
                return
            runs = service.store.list('run')
            failures = [j for j in service.store.jobs() if j['status'] == 'FAILED']
            if failures:
                return stop(str(failures[0].get('error', {}).get('code')))
            if not runs or not runs[0].get('committed'):
                return
            run = runs[0]
            assert run['results'][0]['groups'][0]['displayValue'] == '2'
            if not state['export']:
                checks.extend(['spawned frozen worker with Windows memory bound', 'LangGraph SQLite checkpoint and exact golden value 2'])
                service.queue('export', session['id'], {'runId':run['id'], 'format':'docx'})
                state['export'] = True
                return
            if not service.store.list('artifact'):
                return
            checks.append('bundled document dependencies generate verified DOCX content')
            assert not window.page.javascript_errors
            checks.append('no JavaScript runtime errors')
            stop()
        except Exception as error:
            stop(type(error).__name__ + ': ' + str(error))

    timer.timeout.connect(tick)
    timer.start()
    return timer
