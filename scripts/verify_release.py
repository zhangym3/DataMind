"""Launch the real EXE without a Python PATH and verify the frozen worker."""
import json
import hashlib
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    output=ROOT/'artifacts/acceptance/frozen-smoke.json'
    output.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='DataMind-frozen-中文-') as folder:
        fixture=runpy.run_path(str(ROOT/'tests/test_integration.py'))
        service,_,_,_=fixture['prepared'].__wrapped__(Path(folder))
        env=dict(os.environ)
        env['PATH']=str(Path(os.environ['SystemRoot'])/'System32')
        for key in ('PYTHONHOME','PYTHONPATH','VIRTUAL_ENV','QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH','QML2_IMPORT_PATH'):
            env.pop(key,None)
        exe=ROOT/'dist/InsightAgent/InsightAgent.exe'
        process=subprocess.Popen([str(exe),'--data-dir',str(service.store.home),'--smoke-output',str(output)],env=env,cwd=folder,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
            raise
        assert process.returncode==0, process.returncode
        assert output.is_file(), 'No smoke result from frozen executable'
        result=json.loads(output.read_text(encoding='utf-8'))
        assert result['status']=='PASS' and result['frozen'],result
        assert not (service.store.home/'instance.lock').exists(), 'Frozen shutdown leaked workspace lock'
        result['checks'].append('frozen app exits cleanly and releases workspace lock')
        result['checks'].append('PATH contains only Windows System32; no Python or Office command used')
        result['exeSha256'] = hashlib.sha256(exe.read_bytes()).hexdigest()
        output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=True,indent=2),flush=True)


if __name__=='__main__':
    main()
