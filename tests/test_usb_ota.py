import base64
import hashlib
import json
import struct
import zlib
import io
import zipfile
from unittest.mock import Mock
import pytest
from watchdogs.usb_ota import UsbOtaRunner
from watchdogs import usb_ota
from watchdogs.config import FLASH_BOARDS, FIRMWARE_REPO
from test_firmware_ota import release, info


class Device:
    def __init__(self, image):
        self.image = image
        self.sha = hashlib.sha256(image).hexdigest()
        self.offset = 0
        self.queue = []
        self.commands = []
        self.now = 0
        self.active = False
        self.applied = False
        self.once = False
        self.drop_at = None
        self.reboot_on_drop = False
        self.fail_finish = False
        self.crc_seen = []
        self.version = '1.7.7'
        self.fast = False
        self.features = dict(block_size=4096, encoding='base64', line_size=8192)

    def send(self, cmd):
        if cmd and set(cmd) == {'\b'}:
            self.queue = []
            return
        cmd = cmd.removeprefix('\x15')
        self.commands.append(cmd)
        args = cmd.split()
        if cmd == 'stop':
            self.queue = ['USB OTA is active; finish or abort it first.' if self.active else 'All operations stopped.']
            return
        if cmd == 'version':
            self.queue = ['JanOS version: ' + self.version]
            return
        if cmd == 'ota_info':
            self.queue = info('ota_1' if self.applied else 'ota_0')
            return
        kind = 'status'
        if args[0] == 'uota_begin':
            assert args[1:] == [str(len(self.image)), self.sha]
            self.active = True
            kind = 'ready'
        elif args[0] in ('uota_chunk', 'uota_block'):
            at = int(args[2])
            data = base64.b64decode(args[4], validate=True) if args[0] == 'uota_block' else bytes.fromhex(args[4])
            if args[0] == 'uota_block':
                assert self.fast
                assert len(data) == min(4096-at%4096, len(self.image)-at)
            assert args[1] == self.sha and int(args[3]) == zlib.crc32(data)
            assert data == self.image[at:at+len(data)]
            self.crc_seen.append(at)
            assert at == self.offset
            self.offset += len(data)
            kind = 'ack'
            if self.drop_at == at and not self.once:
                self.once = True
                if self.reboot_on_drop:
                    self.offset = self.offset // 4096 * 4096
                self.queue = []
                raise OSError('simulated unplug')
        elif args[0] == 'uota_finish':
            assert self.offset == len(self.image)
            if self.fail_finish:
                kind = 'error'
            else:
                self.applied = True
                self.active = False
                kind = 'applied'
        self.queue = ['UOTA:' + json.dumps(dict(v=1, kind=kind, board='xiao', sha256=self.sha,
                      size=len(self.image), offset=self.offset, active=self.active, slot='ota_1', error='sha256', **(self.features if self.fast else {})))]

    def read(self):
        result, self.queue = self.queue, []
        return result

    def sleep(self, n):
        self.now += n

    def reconnect(self):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    image = bytes(range(256)) * 32 + b'final partial chunk'
    (tmp_path / 'projectZerobyLOCOSP-xiao.bin').write_bytes(image)
    downloader = Mock(return_value=('v1.7.7', tmp_path))
    monkeypatch.setattr(usb_ota, 'prepare_firmware', downloader)
    dev = Device(image)
    runner = UsbOtaRunner(dev, Mock(), tmp_path, clock=lambda: dev.now, sleep=dev.sleep)
    return dev, runner, downloader


def candidate_bundle(image, *, board='xiao', manifest_board=None, version='1.8.6',
                    source_commit='a'*40, source_dirty=False, changed_hash=False,
                    bad_geometry=False):
    profile = FLASH_BOARDS[board]
    if board == 'waveshare-zero':
        raise ValueError('test helper only builds USB OTA boards')
    parts = [
        (0, 0x10, 0, 0x20000, 0x3F0000, 'ota_0'),
        (0, 0x11, 0, 0x410000, 0x3F0000, 'ota_1'),
        (1, 2, 0, 0x9000, 0x6000, 'nvs'),
    ]
    partition = b''.join(struct.pack('<HBBII16sI', 0x50AA, kind, subtype,
                      offset, size, name.encode().ljust(16, b'\0'), flags)
                      for kind, subtype, flags, offset, size, name in parts) + b'\xff'*32
    files = {
        'bootloader.bin': b'bootloader',
        'partition-table.bin': partition,
        'ota_data_initial.bin': b'ota-data',
        profile['bin_name']: image,
    }
    partitions = [dict(name=name, type=kind, subtype=subtype, offset=offset,
                       size=size, flags=flags)
                  for kind, subtype, flags, offset, size, name in parts]
    manifest = dict(
        format=2, repository=FIRMWARE_REPO, board=manifest_board or board, chip='esp32c5',
        version=version, flash_bytes=profile['flash_bytes'],
        application_limit=profile['app_limit'] - (1 if bad_geometry else 0),
        application_offset=0x20000, application_size=0x3F0000,
        console='uart' if board == 'wroom' else 'usb-serial-jtag',
        updates=['usb-ota', 'rom'], psram=True, partitions=partitions,
        source_commit=source_commit, source_dirty=source_dirty,
        files={name:dict(offset=profile['offsets'][name], size=len(data),
                         sha256=hashlib.sha256(data).hexdigest())
               for name, data in files.items()},
    )
    if changed_hash:
        files[profile['bin_name']] = b'changed image'
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr('manifest.json', json.dumps(manifest, separators=(',', ':')))
    return output.getvalue()


def _run_candidate(setup, tmp_path, **bundle_options):
    dev, runner, downloader = setup
    dev.version = '1.8.6'
    path = tmp_path / 'candidate.zip'
    path.write_bytes(candidate_bundle(dev.image, **bundle_options))
    result = runner.run_candidate(path, 'a'*40)
    return dev, result, downloader


@pytest.mark.parametrize('reboot', [False, True])
def test_interrupted_transfer_queries_board_and_resumes_without_wrong_bytes(setup, reboot):
    dev, runner, download = setup
    dev.drop_at = 4352
    dev.reboot_on_drop = reboot
    result = runner.run(release('v1.7.7'))
    assert result.state == 'success' and dev.applied
    assert dev.crc_seen.count(4096) == (2 if reboot else 1)
    assert sum(c.startswith('uota_begin') for c in dev.commands) == 2
    assert sum(c.startswith('uota_finish') for c in dev.commands) == 1
    assert download.call_args.args[1] == 'xiao'


@pytest.mark.parametrize('version', ['1.7.5', '1.7.6'])
def test_bootstrap_old_firmware_explains_wifi_requirement_without_download(setup, version):
    dev, runner, download = setup
    dev.version = version
    result = runner.run(release('v1.7.7'))
    assert result.state == 'failed' and 'Wi-Fi' in result.message
    download.assert_not_called()
    assert dev.commands == ['stop', 'version']


def test_device_sha_failure_is_not_success_or_reboot(setup):
    dev, runner, _ = setup
    dev.fail_finish = True
    result = runner.run(release('v1.7.7'))
    assert result.state == 'failed' and not dev.applied
    assert 'Boot slot preserved' in result.message


def test_other_pending_image_requires_explicit_discard(setup):
    dev, runner, _ = setup
    original = dev.send
    def send(cmd):
        original(cmd)
        if cmd.endswith('uota_status'):
            data = json.loads(dev.queue[0][5:]); data['sha256'] = 'a' * 64
            dev.queue = ['UOTA:' + json.dumps(data)]
    dev.send = send
    result = runner.run(release('v1.7.7'))
    assert result.state == 'failed'
    assert not any(c.startswith('uota_begin') for c in dev.commands)


def test_bounded_retry_never_sends_finish_after_failed_transfer(setup):
    dev, runner, _ = setup
    original = dev.send
    def send(cmd):
        if 'uota_chunk' in cmd:
            raise OSError('offline')
        original(cmd)
    dev.send = send
    result = runner.run(release('v1.7.7'))
    assert result.state == 'unconfirmed' and 'saved' in result.message
    assert not dev.applied and not any(c.startswith('uota_finish') for c in dev.commands)


def test_preexisting_active_transfer_resumes_without_stopping_or_rewriting_it(setup):
    dev, runner, _ = setup
    dev.active = True
    dev.offset = 4096
    result = runner.run(release('v1.7.7'))
    assert result.state == 'success'
    assert dev.commands[:2] == ['stop', 'version']
    assert min(dev.crc_seen) == 4096


@pytest.mark.parametrize('reboot', [False, True])
def test_fast_blocks_resume_after_lost_ack_and_keep_final_partial_bytes(setup, reboot):
    dev, runner, _ = setup
    dev.fast = True
    dev.drop_at = 4096
    dev.reboot_on_drop = reboot
    result = runner.run(release('v1.7.7'))
    assert result.state == 'success' and dev.applied
    assert runner.fast and dev.crc_seen == [0, 4096, 8192]
    assert not any(c.startswith('uota_chunk') for c in dev.commands)


def test_fast_upgrade_of_live_legacy_transfer_finishes_sector_first(setup):
    dev, runner, _ = setup
    dev.fast = dev.active = True
    dev.offset = 256
    assert runner.run(release('v1.7.7')).state == 'success'
    commands = [c.split() for c in dev.commands if c.startswith('uota_block')]
    assert [len(base64.b64decode(c[4])) for c in commands] == [3840,4096,19]
    assert len(commands) == 3  # legacy uses 32 further full chunks + tail


@pytest.mark.parametrize('feature', [dict(block_size=8192), dict(encoding='hex'), dict(line_size=1024)])
def test_unknown_fast_parameters_fall_back_to_legacy(setup, feature):
    dev, runner, _ = setup
    dev.fast = True
    dev.features.update(feature)
    assert runner.run(release('v1.7.7')).state == 'success'
    assert not runner.fast
    assert all(not c.startswith('uota_block') for c in dev.commands)


def test_fast_large_wire_write_uses_one_bounded_write_without_flush():
    from watchdogs.firmware_ota import SerialOtaTransport
    from watchdogs.serial_manager import SerialManager

    conn = Mock()
    conn.write.side_effect = len
    conn.write_timeout = 11
    manager = SerialManager("/dev/fake")
    manager.serial_conn = conn
    transport = SerialOtaTransport(manager, None)
    command = (
        "uota_block " + "a" * 64 + " 0 123 " + base64.b64encode(bytes(4096)).decode()
    )
    transport.send_fast(command)
    conn.write.assert_called_once_with((command+'\r').encode())
    conn.flush.assert_not_called()
    assert conn.write_timeout == 11
    conn.write.side_effect = None
    conn.write.return_value = 0
    with pytest.raises(OSError, match="Incomplete serial write"):
        transport.send_fast(command)
    conn.reset_output_buffer.assert_called_once()


def test_repeated_large_block_failure_falls_back_without_discard_or_restart(setup):
    dev, runner, _ = setup
    dev.fast = True
    original = dev.send
    failures = []
    def send(command):
        if command.startswith('uota_block') and int(command.split()[2]) >= 4096:
            failures.append(command)
            raise OSError('large USB block interrupted')
        original(command)
    dev.send = send
    assert runner.run(release('v1.7.7')).state == 'success'
    assert len(failures) == 2 and runner.fast_disabled
    assert dev.crc_seen.count(0) == 1
    assert min(int(c.split()[2]) for c in dev.commands if c.startswith('uota_chunk')) == 4096
    assert not any(c.startswith('uota_abort') for c in dev.commands)


@pytest.mark.parametrize('offset,active', [(1,True),(99999,True),(0,False)])
def test_invalid_ready_state_cannot_send_data_or_finish(setup, offset, active):
    dev, runner, _ = setup
    original = dev.send
    def send(cmd):
        original(cmd)
        if cmd.startswith('uota_begin'):
            data = json.loads(dev.queue[0][5:]); data.update(offset=offset,active=active)
            dev.queue = ['UOTA:' + json.dumps(data)]
    dev.send = send
    assert runner.run(release('v1.7.7')).state == 'unconfirmed'
    assert not dev.crc_seen and not dev.applied


def test_local_candidate_bundle_uses_guarded_usb_ota_without_download(setup, tmp_path):
    dev, result, downloader = _run_candidate(setup, tmp_path)
    assert result.state == 'success' and result.version == '1.8.6'
    assert dev.applied and downloader.call_count == 0


def test_local_candidate_requires_full_expected_source_commit_before_io(setup, tmp_path):
    dev, runner, downloader = setup
    path = tmp_path / 'candidate.zip'
    path.write_bytes(candidate_bundle(dev.image))
    result = runner.run_candidate(path, 'a'*39)
    assert result.state == 'failed'
    assert dev.commands == [] and downloader.call_count == 0


@pytest.mark.parametrize('options,expected_commit', [
    ({'manifest_board':'invalid'}, 'a'*40),
    ({}, 'b'*40),
    ({'source_dirty':True}, 'a'*40),
    ({'changed_hash':True}, 'a'*40),
    ({'bad_geometry':True}, 'a'*40),
    ({'version':'candidate'}, 'a'*40),
])
def test_local_candidate_rejections_happen_before_ota_write(
        setup, tmp_path, options, expected_commit):
    dev, runner, downloader = setup
    dev.version = '1.8.6'
    path = tmp_path / 'candidate.zip'
    path.write_bytes(candidate_bundle(dev.image, **options))
    result = runner.run_candidate(path, expected_commit)
    assert result.state == 'failed'
    assert not dev.applied and downloader.call_count == 0
    assert dev.commands == []


def test_valid_candidate_for_other_connected_board_is_rejected_before_stop(setup, tmp_path):
    dev, runner, downloader = setup
    dev.version = '1.8.6'
    path = tmp_path / 'candidate.zip'
    path.write_bytes(candidate_bundle(dev.image, board='wroom'))
    result = runner.run_candidate(path, 'a'*40)
    assert result.state == 'failed'
    assert dev.commands == ['version', 'uota_status']
    assert downloader.call_count == 0 and not dev.applied
