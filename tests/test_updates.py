import hashlib
import io
import json
from pathlib import Path
import subprocess
import zipfile
import pytest
from watchdogs import updates

def bundle(board='xiao', version='1.7.1', corrupt=False, extra=False):
    files = {name:b'firmware-'+name.encode() for name in updates.FLASH_BOARDS[board]['offsets']}
    manifest = dict(format=1, repository=updates.FIRMWARE_REPO, board=board, chip='esp32c5', version=version,
        files={name:dict(offset=updates.FLASH_BOARDS[board]['offsets'][name], size=len(data),
                        sha256=hashlib.sha256(data).hexdigest()) for name,data in files.items()})
    if corrupt:
        files['bootloader.bin'] = b'changed'
    out = io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        for name,data in files.items():
            z.writestr(name,data)
        z.writestr('manifest.json',json.dumps(manifest))
        if extra:
            z.writestr('../escaped.bin',b'bad')
    return out.getvalue(), files

def test_bundle_accepts_expected_board_and_checks_files():
    blob, files = bundle()
    assert updates.validate_bundle(blob,'xiao','1.7.1') == files
    for bad in (bundle(corrupt=True)[0], bundle(extra=True)[0], bundle(board='wroom')[0]):
        with pytest.raises(ValueError):
            updates.validate_bundle(bad,'xiao','1.7.1')
    with pytest.raises(ValueError):
        updates.validate_bundle(blob,'xiao','1.7.2')

def test_download_no_upstream_fallback_or_stale_cache(tmp_path, monkeypatch):
    name = 'projectZerobyLOCOSP-xiao-1.7.1.zip'
    base = 'https://github.com/Smethan/projectZero/releases/download/v1.7.1/'
    release = dict(tag_name='v1.7.1',assets=[dict(name=n,browser_download_url=base+n) for n in (name,'SHA256SUMS')])
    blob, files = bundle()
    replies = {base+name:blob, base+'SHA256SUMS':(hashlib.sha256(blob).hexdigest()+'  '+name+'\n').encode()}
    monkeypatch.setattr(updates,'download',lambda url,limit:replies[url])
    tag, first = updates.prepare_firmware(tmp_path,'xiao',release)
    assert tag == 'v1.7.1' and (first/'bootloader.bin').read_bytes() == files['bootloader.bin']
    _, second = updates.prepare_firmware(tmp_path,'xiao',release)
    assert first != second
    replies[base+name] = b'corrupt archive'
    with pytest.raises(ValueError,match='checksum'):
        updates.prepare_firmware(tmp_path,'xiao',release)
    release['assets'] = []
    with pytest.raises(ValueError,match='missing'):
        updates.prepare_firmware(tmp_path,'xiao',release)
    release['assets'] = [dict(name=name,browser_download_url=base.replace('Smethan','LOCOSP')+name)]
    with pytest.raises(ValueError,match='configured fork'):
        updates.asset_url(release,name)

def repo_pair(tmp_path, monkeypatch):
    real_run = subprocess.run
    def git(path,*args):
        r = real_run(['git','-C',str(path),*args],capture_output=True,text=True,check=True)
        return r.stdout.strip()
    seed, remote, client = [tmp_path/n for n in ('seed','remote.git','client')]
    seed.mkdir()
    git(seed,'init','-b','main')
    git(seed,'config','user.email','test@example.invalid')
    git(seed,'config','user.name','Test')
    (seed/'file').write_text('old')
    git(seed,'add','file'); git(seed,'commit','-m','initial')
    real_run(['git','clone','--bare',str(seed),str(remote)],check=True,capture_output=True)
    real_run(['git','clone',str(remote),str(client)],check=True,capture_output=True)
    git(client,'remote','set-url','origin','https://github.com/Smethan/WatchDogsGo.git')
    (seed/'file').write_text('new')
    git(seed,'commit','-am','new')
    git(seed,'push',str(remote),'main')
    def local_fetch(args,**kwargs):
        if args[-3:] == ['fetch','origin','main']:
            args = args[:-3]+['fetch',str(remote),'main:refs/remotes/origin/main']
        return real_run(args,**kwargs)
    monkeypatch.setattr(updates.subprocess,'run',local_fetch)
    return client, git

def test_app_update_migrates_feature_and_preserves_untracked(tmp_path,monkeypatch):
    client,git = repo_pair(tmp_path,monkeypatch)
    git(client,'switch','-c','feature/all-wardrive')
    (client/'my-notes').write_text('keep')
    assert updates.update_app(client,lambda s:None)
    assert git(client,'branch','--show-current') == 'main'
    assert (client/'file').read_text() == 'new'
    assert (client/'my-notes').read_text() == 'keep'
    assert not updates.update_app(client,lambda s:None)

def test_app_update_refuses_dirty_diverged_and_upstream(tmp_path,monkeypatch):
    client,git = repo_pair(tmp_path,monkeypatch)
    (client/'file').write_text('user edit')
    with pytest.raises(RuntimeError,match='Local tracked'):
        updates.update_app(client)
    assert (client/'file').read_text() == 'user edit'
    git(client,'config','user.email','test@example.invalid')
    git(client,'config','user.name','Test')
    git(client,'commit','-am','local commit')
    before = git(client,'rev-parse','HEAD')
    with pytest.raises(RuntimeError):
        updates.update_app(client)
    assert git(client,'rev-parse','HEAD') == before
    git(client,'remote','set-url','origin','https://github.com/LOCOSP/WatchDogsGo.git')
    with pytest.raises(RuntimeError,match='Origin must'):
        updates.update_app(client)
