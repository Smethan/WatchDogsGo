"""Fork-only application updates and verified firmware release downloads."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.request import Request, urlopen
import zipfile

from .config import APP_UPDATE_REPO, APP_UPDATE_BRANCH, FIRMWARE_REPO, FIRMWARE_RELEASE_URL, FLASH_BOARDS

def release_version(tag):
    if not isinstance(tag, str) or not re.fullmatch(r'v?\d+\.\d+\.\d+', tag):
        raise ValueError('Unsupported release version')
    return tuple(int(p) for p in tag.removeprefix('v').split('.'))

def download(url, limit):
    req = Request(url, headers={'User-Agent':'Smethan-WatchDogsGo'})
    with urlopen(req, timeout=30) as response:
        data = response.read(limit+1)
    if len(data) > limit:
        raise ValueError('Release download exceeds size limit')
    return data

def latest_release():
    data = json.loads(download(FIRMWARE_RELEASE_URL, 256*1024))
    release_version(data['tag_name'])
    if data.get('draft') or data.get('prerelease'):
        raise ValueError('Expected a published stable firmware release')
    return data

def firmware_releases():
    """Published fork releases with the checksummed bundles used by this app."""
    url = f'https://api.github.com/repos/{FIRMWARE_REPO}/releases?per_page=100'
    data = json.loads(download(url, 2*1024*1024))
    if not isinstance(data, list):
        raise ValueError('Invalid firmware release list')
    releases = []
    seen = set()
    for release in data:
        try:
            tag = release['tag_name']
            release_version(tag)
            if release.get('draft') or release.get('prerelease') or tag in seen:
                continue
            version = tag.removeprefix('v')
            for name in ('SHA256SUMS', f'projectZerobyLOCOSP-{version}.zip',
                         f'projectZerobyLOCOSP-xiao-{version}.zip'):
                asset_url(release, name)
            releases.append(release)
            seen.add(tag)
        except (KeyError, TypeError, ValueError):
            continue  # Old/unrelated releases are not verified flash bundles.
    return sorted(releases, key=lambda r:release_version(r['tag_name']), reverse=True)

def asset_url(release, name):
    matches = [a for a in release.get('assets', []) if a.get('name') == name]
    if len(matches) != 1:
        raise ValueError('Release is missing a unique '+name)
    url = matches[0].get('browser_download_url', '')
    prefix = f'https://github.com/{FIRMWARE_REPO}/releases/download/{release["tag_name"]}/'
    if not url.startswith(prefix) or url[len(prefix):] != name:
        raise ValueError('Release asset is not from the configured fork')
    return url

def validate_bundle(blob, board, version):
    """Read only allowlisted files; never extract arbitrary ZIP paths."""
    offsets = FLASH_BOARDS[board]['offsets']
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        expected = set(offsets) | {'manifest.json'}
        if len(z.infolist()) != len(expected) or set(z.namelist()) != expected:
            raise ValueError('Unexpected or missing firmware ZIP entries')
        if any(i.file_size > 8*1024*1024 for i in z.infolist()) or sum(i.file_size for i in z.infolist()) > 12*1024*1024:
            raise ValueError('Oversized firmware ZIP entries')
        if z.getinfo('manifest.json').file_size > 16384:
            raise ValueError('Oversized firmware manifest')
        manifest = json.loads(z.read('manifest.json'))
        if (manifest.get('format') != 1 or manifest.get('repository') != FIRMWARE_REPO
                or manifest.get('board') != board or manifest.get('chip') != 'esp32c5'
                or manifest.get('version') != version or set(manifest.get('files', {})) != set(offsets)):
            raise ValueError('Firmware manifest does not match the board/release')
        files = {}
        for name, offset in offsets.items():
            payload = z.read(name)
            info = manifest['files'][name]
            if (info.get('offset') != offset or info.get('size') != len(payload)
                    or info.get('sha256') != hashlib.sha256(payload).hexdigest()):
                raise ValueError('Firmware file verification failed: '+name)
            files[name] = payload
    return files

def prepare_firmware(directory, board, release=None):
    if board not in FLASH_BOARDS:
        raise ValueError('Unknown board')
    release = latest_release() if release is None else release
    if release.get('draft') or release.get('prerelease'):
        raise ValueError('Expected a published stable firmware release')
    tag = release['tag_name']
    release_version(tag)
    version = tag.removeprefix('v')
    suffix = '-xiao' if board == 'xiao' else ''
    name = f'projectZerobyLOCOSP{suffix}-{version}.zip'
    sums = download(asset_url(release, 'SHA256SUMS'), 65536).decode('ascii')
    hashes = [line.split()[0] for line in sums.splitlines()
              if len(line.split()) == 2 and line.split()[1] == name]
    if len(hashes) != 1 or not re.fullmatch('[0-9a-f]{64}', hashes[0]):
        raise ValueError('Missing or invalid release checksum')
    blob = download(asset_url(release, name), 12*1024*1024)
    if hashlib.sha256(blob).hexdigest() != hashes[0]:
        raise ValueError('Firmware archive checksum mismatch')
    files = validate_bundle(blob, board, version)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix=f'{board}-{version}-', dir=directory))
    for filename, data in files.items():
        (target/filename).write_bytes(data)
    (target/'release.json').write_text(json.dumps(dict(repository=FIRMWARE_REPO, tag=tag, board=board)))
    return tag, target

def update_app(directory, report=print):
    """Fast-forward only, with an explicit migration from the old feature branch."""
    directory = Path(directory).resolve()
    prefix = []
    if hasattr(os, 'geteuid') and os.geteuid() == 0 and directory.stat().st_uid != 0:
        import pwd
        prefix = ['sudo', '-n', '-u', pwd.getpwuid(directory.stat().st_uid).pw_name, '--']
    def git(*args):
        result = subprocess.run(prefix+['git','-C',str(directory),*args],
            capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip() or 'Git update failed')
        return result.stdout.strip()
    remote = git('remote','get-url','origin').lower().removesuffix('.git')
    allowed = {f'https://github.com/{APP_UPDATE_REPO}'.lower(), f'git@github.com:{APP_UPDATE_REPO}'.lower()}
    if remote not in allowed:
        raise RuntimeError('Origin must point to '+APP_UPDATE_REPO+' before updating')
    if git('status','--porcelain','--untracked-files=no'):
        raise RuntimeError('Local tracked files have changes; commit or stash them before updating')
    branch = git('branch','--show-current')
    if branch not in (APP_UPDATE_BRANCH, 'feature/all-wardrive'):
        raise RuntimeError('Switch to main before updating this checkout')
    report('[UPDATE] Fetching '+APP_UPDATE_REPO+' / '+APP_UPDATE_BRANCH)
    git('fetch','origin',APP_UPDATE_BRANCH)
    before = git('rev-parse','HEAD')
    target = git('rev-parse','FETCH_HEAD')
    git('merge-base','--is-ancestor','HEAD',target)
    if branch != APP_UPDATE_BRANCH:
        if git('branch','--list',APP_UPDATE_BRANCH):
            git('merge-base','--is-ancestor',APP_UPDATE_BRANCH,target)
            git('switch',APP_UPDATE_BRANCH)
        else:
            git('switch','-c',APP_UPDATE_BRANCH,'--track','origin/'+APP_UPDATE_BRANCH)
    git('merge','--ff-only',target)
    git('branch','--set-upstream-to=origin/'+APP_UPDATE_BRANCH,APP_UPDATE_BRANCH)
    changed = before != git('rev-parse','HEAD')
    version_file = directory/'watchdogs/__init__.py'
    if version_file.exists():
        match = re.search(r'__version__\s*=\s*"([^"]+)"', version_file.read_text())
        if match:
            report('[UPDATE] Smethan fork v'+match.group(1)+' / main')
    report('[UPDATE] '+('Updated; restart WDG to load the changes.' if changed else 'Already up to date.'))
    return changed
