from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH)
datas = [(str(root / 'insight_agent/web'), 'insight_agent/web'),
         (str(root / 'insight_agent/resources'), 'insight_agent/resources'),
         (str(root / 'artifacts/build-metadata/licenses'), 'build-metadata/licenses'),
         (str(root / 'artifacts/build-metadata/dependencies.json'), 'build-metadata'),
         (str(root / 'artifacts/build-metadata/requirements-lock.txt'), 'build-metadata')]
hidden = ['keyring.backends.Windows', 'win32ctypes.pywin32', 'tzdata',
          'langgraph.checkpoint.sqlite', 'langgraph.checkpoint.serde.jsonplus']
for package in ('langgraph', 'langchain_core', 'jsonschema_specifications', 'py7zr'):
    hidden += collect_submodules(package)
for package in ('tzdata', 'jsonschema_specifications'):
    datas += collect_data_files(package)
datas += collect_data_files('doc2txt')
for package in ('langgraph', 'langgraph-checkpoint-sqlite', 'langchain-core', 'keyring'):
    datas += copy_metadata(package)
a = Analysis([str(root / 'main.py')], pathex=[str(root)], binaries=[], datas=datas,
             hiddenimports=hidden, hookspath=[], runtime_hooks=[],
             excludes=['pytest', 'IPython', 'tkinter'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='InsightAgent',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='InsightAgent')
