"""Traceable local onedir build and dependency/license inventory."""
import hashlib
import importlib.metadata as metadata
import json
import platform
import subprocess
import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def source_hashes():
    sources = [ROOT / name for name in ('main.py', 'InsightAgent.spec', 'requirements.txt', 'requirements-lock.txt', 'requirements-dev.txt')]
    for folder in ('insight_agent', 'scripts', 'skills', 'tests'):
        sources += [p for p in (ROOT / folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(sources)}


def copy_documents(release):
    documents = [ROOT / 'README.md'] + [p for p in (ROOT / 'docs').rglob('*') if p.is_file() and p.suffix in ('.md', '.json')]
    for source in documents:
        target = release / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


def file_inventory(release):
    return [{'path': p.relative_to(release).as_posix(), 'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(release.rglob('*')) if p.is_file() and p.name != 'RELEASE-MANIFEST.json']


def main():
    if sys.version_info[:2] != (3, 11) or platform.architecture()[0] != '64bit':
        raise SystemExit('Build requires Python 3.11 x64')
    output = ROOT / 'artifacts/build-metadata'
    output.mkdir(parents=True, exist_ok=True)
    pending = [Requirement(line) for line in (ROOT / 'requirements.txt').read_text().splitlines() if line and not line.startswith('#')]
    seen, dependencies = set(), {}
    while pending:
        req = pending.pop()
        key = (canonicalize_name(req.name), tuple(sorted(req.extras)))
        if key in seen:
            continue
        seen.add(key)
        dist = metadata.distribution(req.name)
        assert dist.version in req.specifier, str(req)
        dependencies[key[0]] = dist
        for raw in dist.requires or []:
            child = Requirement(raw)
            if child.marker is None or any(child.marker.evaluate({'extra': extra}) for extra in ('', *req.extras)):
                pending.append(child)
    lock = '# Validated Windows x64 / Python 3.11 runtime closure.\n' + ''.join(f'{name}=={dist.version}\n' for name, dist in sorted(dependencies.items()))
    (ROOT / 'requirements-lock.txt').write_text(lock, encoding='utf-8')
    (output / 'requirements-lock.txt').write_text(lock, encoding='utf-8')
    packages = []
    for name, dist in sorted(dependencies.items()):
        licenses = []
        for entry in dist.files or []:
            if any(token in Path(str(entry)).name.lower() for token in ('license', 'copying', 'notice')) and str(entry).endswith(('.txt', '.md', '.rst', 'LICENSE', 'COPYING', 'NOTICE')):
                source = Path(dist.locate_file(entry))
                if source.is_file() and source.stat().st_size < 2 * 1024**2:
                    dest = output / 'licenses' / name / str(entry).replace('..', '_').replace('\\', '_').replace('/', '_')
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(source.read_bytes())
                    licenses.append(dest.relative_to(output).as_posix())
        packages.append({'name': name, 'version': dist.version, 'license': dist.metadata.get('License-Expression') or dist.metadata.get('License'), 'licenseFiles': licenses})
    (output / 'dependencies.json').write_text(json.dumps(packages, ensure_ascii=False, indent=2), encoding='utf-8')
    with (output / 'pyinstaller.log').open('w', encoding='utf-8') as log:
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', str(ROOT / 'InsightAgent.spec')], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    release = ROOT / 'dist/InsightAgent'
    copy_documents(release)
    files = file_inventory(release)
    sys.path.insert(0, str(ROOT))
    from insight_agent.core import VERSION, now
    manifest = {'appVersion': VERSION, 'builtAt': now(), 'python': platform.python_version(), 'platform': platform.platform(), 'sourceHashes': source_hashes(), 'totalBytes': sum(f['bytes'] for f in files), 'fileCount': len(files), 'files': files}
    (ROOT / 'artifacts/acceptance').mkdir(parents=True, exist_ok=True)
    (ROOT / 'artifacts/acceptance/release-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Built {release}: {len(files)} files, {manifest["totalBytes"] / 1024**2:.1f} MiB', flush=True)


if __name__ == '__main__':
    main()
