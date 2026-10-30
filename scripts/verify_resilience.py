"""Windows process kill/recovery and real credential-vault checks; synthetic only."""
import json
import multiprocessing
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def blocked():
    time.sleep(180)


def bounded(pipe):
    from insight_agent.resources import bound_worker_memory
    handle = bound_worker_memory(128 * 1024**2)
    pipe.send('bounded')
    try:
        value = bytearray(256 * 1024**2)
        pipe.send('unbounded')
    except MemoryError:
        pipe.send('MemoryError')


def main():
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from insight_agent.desktop import InsightWindow
    from insight_agent.models import Credentials
    checks = []
    vault = Credentials()
    ref, secret = 'acceptance-' + uuid.uuid4().hex, 'synthetic-not-a-provider-key'
    try:
        vault.set(ref, secret)
        assert vault.get(ref) == secret
        vault.set(ref, secret + '-rotated')
        assert vault.get(ref) == secret + '-rotated'
    finally:
        vault.delete(ref)
    assert vault.get(ref) is None
    checks.append('real Windows credential vault set/get/rotate/delete; synthetic key cleaned')
    parent, child = multiprocessing.Pipe()
    process = multiprocessing.Process(target=bounded, args=(child,))
    process.start()
    assert parent.poll(20) and parent.recv() == 'bounded'
    assert parent.poll(20) and parent.recv() == 'MemoryError'
    process.join(5)
    assert not process.is_alive() and process.exitcode == 0
    checks.append('Windows JobObject rejects allocation above configured process-tree memory limit')
    app = QApplication([])
    with tempfile.TemporaryDirectory(prefix='DataMind-watchdog-') as folder:
        window = InsightWindow(folder, start_worker=False)
        window.show()
        store = window.service.store
        try:
            queued = window.service.queue('diagnostics', 'local', {})
            job = store.claim()
            assert job['id'] == queued['jobId']
            window.worker = multiprocessing.Process(target=blocked)
            window.worker.start()
            hung_process = window.worker
            window.enable_worker = True
            # Advance only the watchdog observation clock. The actual worker
            # is blocked, terminated by production code, then replaced.
            window.observed_jobs[(job['id'],job['lease'])] = time.monotonic()-1000
            window.tick()
            assert not hung_process.is_alive()
            failed = next(j for j in store.jobs() if j['id']==job['id'])
            assert failed['status']=='FAILED' and failed['error']['code']=='RUN_TIMEOUT'
            assert failed['lease'] > job['lease']
            checks.append('GUI watchdog terminates a blocked worker and fences expired lease')
            followup = window.service.queue('diagnostics','local',{})
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                latest=next(j for j in store.jobs() if j['id']==followup['jobId'])
                if latest['status'] not in ('QUEUED','RUNNING'):
                    break
                QTest.qWait(100)
            assert latest['status']=='SUCCEEDED', latest
            checks.append('replacement worker completes next durable queued job')
            from insight_agent.native_settings import SettingsDialog
            settings=SettingsDialog(window)
            settings.show()
            settings.scale.setCurrentText('150')
            settings.title.setText('temporary title')
            settings.reset_general()
            assert settings.scale.currentText()=='100' and settings.title.text()!='temporary title'
            assert window.web.zoomFactor()==1
            settings.accept()
            checks.append('native settings reset updates saved state and live controls')
        finally:
            window.stop_worker()
            window.close()
            deadline=time.monotonic()+12
            while window.isVisible() and time.monotonic()<deadline:
                QTest.qWait(50)
            assert not (store.home/'instance.lock').exists()
            checks.append('shutdown releases workspace lock')
    output=ROOT/'artifacts/acceptance/windows-resilience.json'
    output.write_text(json.dumps({'status':'PASS','checks':checks},ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'PASS: {len(checks)} Windows resilience checks',flush=True)


if __name__=='__main__':
    multiprocessing.freeze_support()
    main()
