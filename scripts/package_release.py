"""Bundle verified binaries, current documents and synthetic acceptance evidence."""
import hashlib
import json
import zipfile
from xml.etree import ElementTree

from build_release import ROOT, copy_documents, file_inventory, source_hashes


def main():
    release = ROOT / 'dist/InsightAgent'
    evidence = ROOT / 'artifacts/acceptance'
    manifest_path = evidence / 'release-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    assert manifest['sourceHashes'] == source_hashes(), 'Sources changed after build; rebuild first'
    for item in manifest['files']:
        if item['path'].startswith('docs/') or item['path'] == 'README.md':
            continue
        assert hashlib.sha256((release / item['path']).read_bytes()).hexdigest() == item['sha256'], item['path']
    smoke = json.loads((evidence / 'frozen-smoke.json').read_text(encoding='utf-8'))
    assert smoke['status'] == 'PASS' and smoke['frozen']
    assert smoke['exeSha256'] == hashlib.sha256((release / 'InsightAgent.exe').read_bytes()).hexdigest(), 'Re-run EXE verification'
    ui = json.loads((evidence / 'ui-verification.json').read_text(encoding='utf-8'))
    assert ui['count'] == len(ui['passed']) and ui['count'] >= 25
    assert json.loads((evidence / 'windows-resilience.json').read_text(encoding='utf-8'))['status'] == 'PASS'
    suites = ElementTree.parse(evidence / 'pytest.xml').getroot().findall('testsuite')
    assert suites and sum(int(s.get('tests', 0)) for s in suites) > 0
    assert all(int(s.get(key, 0)) == 0 for s in suites for key in ('failures', 'errors', 'skipped'))
    copy_documents(release)
    names = ['pytest.xml', 'ui-verification.json', 'windows-resilience.json', 'capacity.json', 'frozen-smoke.json',
             '01-analysis.png', '02-definition.png', '03-compact.png', '04-zoom-150.png', 'frozen-smoke.png']
    for name in names:
        target = release / 'artifacts/acceptance' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((evidence / name).read_bytes())
    files = file_inventory(release)
    manifest.update(files=files, fileCount=len(files), totalBytes=sum(f['bytes'] for f in files), manifestSelfExcluded=True)
    raw = json.dumps(manifest, ensure_ascii=False, indent=2)
    # Keep manifest bytes stable on Windows so the ZIP byte check matches raw.
    manifest_path.write_text(raw, encoding='utf-8', newline='')
    (release / 'RELEASE-MANIFEST.json').write_text(raw, encoding='utf-8', newline='')
    archive_path = ROOT / 'dist' / f'InsightAgent-{manifest["appVersion"]}-windows-x64.zip'
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(release.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(release.parent).as_posix())
    with zipfile.ZipFile(archive_path) as archive:
        assert len(archive.namelist()) == len(files) + 1
        for item in files:
            # Reading every member also checks ZIP CRC; hashes bind it to the manifest.
            assert hashlib.sha256(archive.read('InsightAgent/' + item['path'])).hexdigest() == item['sha256'], item['path']
        assert archive.read('InsightAgent/RELEASE-MANIFEST.json') == raw.encode('utf-8')
    sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    archive_path.with_suffix('.zip.sha256').write_text(f'{sha}  {archive_path.name}\n', encoding='ascii')
    result = {'status': 'PASS', 'archive': str(archive_path), 'bytes': archive_path.stat().st_size,
              'sha256': sha, 'verifiedFiles': len(files) + 1, 'appVersion': manifest['appVersion']}
    (evidence / 'package-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
