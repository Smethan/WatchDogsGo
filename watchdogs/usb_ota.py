"""Verified app-only USB OTA with acknowledged chunks and durable resume."""
import hashlib
import json
from pathlib import Path
import time
import zlib

from .firmware_ota import OtaRunner, OtaResult, OtaError, validate_release
from .updates import prepare_firmware, release_version


class UsbOtaRunner(OtaRunner):
    def __init__(self, transport, report, directory, **kwargs):
        super().__init__(transport, report, **kwargs)
        self.directory = directory
        self.identity = None
        self.poll_interval = 0.005
        self._resync = True

    def send(self, command):
        self.pending.clear()
        paced = getattr(self.transport, 'send_paced', self.transport.send)
        if self._resync:
            # IDF dumb mode ignores Ctrl+U. Backspace works in both console
            # modes and clears the full maximum line without executing it.
            paced('\b' * 1024)
            self._resync = False
        paced(command)

    def version(self):
        self._resync = True
        return super().version()

    def exchange(self, command, timeout=8):
        self.send(command)
        def parse(line):
            if not line.startswith('UOTA:'):
                return None
            try:
                record = json.loads(line[5:])
                if not isinstance(record, dict) or record.get('v') != 1:
                    return None
                if record.get('kind') not in ('status', 'ready', 'ack', 'error', 'applied', 'aborted'):
                    return None
                if record.get('board') not in ('xiao', 'wroom'):
                    return None
                if self.identity and (record.get('sha256'), record.get('size'), record.get('board')) != self.identity:
                    if record.get('kind') != 'aborted':
                        return None
                for key in ('offset', 'size'):
                    if type(record.get(key)) is not int or not 0 <= record[key] <= 8*1024*1024:
                        return None
                if type(record.get('active')) is not bool:
                    return None
                return record
            except (ValueError, TypeError):
                return None
        return self.wait(parse, timeout, 'USB OTA reply timed out')

    @staticmethod
    def check(record, kind):
        if record['kind'] == 'error':
            reasons = {'different_image_abort_first': 'Another image is pending; select it or explicitly discard the old transfer.',
                       'running_slot_not_valid': 'Running firmware is not marked valid. Reboot normally first.',
                       'sha256': 'Image checksum failed. Discard this transfer and try again.',
                       'image_validation': 'Firmware image validation failed; current boot slot is preserved.',
                       'radio_or_ota_busy': 'Could not stop the radios, or another OTA is running.'}
            raise OtaError(reasons.get(record.get('error'), 'ESP32 rejected the USB OTA operation; check status before retrying.'))
        if record['kind'] != kind:
            raise OtaError('Unexpected USB OTA response')
        return record

    def run(self, release, ssid='', password='', *, discard=False):
        try:
            tag = validate_release(release)
            self.report('Checking application-mode USB OTA support...', None)
            self.send('stop')
            self.wait(lambda line: line in ('All operations stopped.',
                      'USB OTA is active; finish or abort it first.'), 30,
                      'Could not stop scans or confirm the pending USB transfer.')
            current = self.version()
            if release_version(current) < (1, 7, 7):
                return OtaResult('failed', 'USB OTA requires firmware 1.7.7+. Install it once using Wi-Fi OTA.', current)
            status = self.check(self.exchange('uota_status'), 'status')
            board = status['board']
            before = self.info()
            if before['boot'] != before['running'] or before['state'] != 2:
                raise OtaError('Current OTA slot is not stable/valid. Reboot normally first.')
            self.report('Downloading and verifying the selected firmware on the uConsole...', None)
            selected, directory = prepare_firmware(self.directory, board, release=release)
            if selected != tag:
                raise OtaError('Unexpected firmware release')
            filename = 'projectZerobyLOCOSP' + ('-xiao' if board == 'xiao' else '') + '.bin'
            image = (Path(directory) / filename).read_bytes()
            digest = hashlib.sha256(image).hexdigest()
            if discard and status.get('sha256'):
                self.check(self.exchange('uota_abort ' + status['sha256']), 'aborted')
            elif status.get('sha256') and status['sha256'] != digest:
                raise OtaError('A different image is pending. Select that version or use Ctrl+D to discard it.')
            self.identity = digest, len(image), board
            self.requested = True
            ready = self.check(self.exchange(f'uota_begin {len(image)} {digest}', 60), 'ready')
            position = ready['offset']
            retries = 0
            deadline = self.clock() + 1800
            while position < len(image):
                if self.clock() >= deadline:
                    raise OtaError("USB transfer paused after 30 minutes. Select this version again to resume.")
                if position % 256:
                    raise OtaError('Invalid USB OTA resume offset')
                data = image[position:position+256]
                self.report(f'USB firmware transfer: {position*100//len(image)}% (resumes after disconnects)', position*100//len(image))
                try:
                    reply = self.exchange(f'uota_chunk {digest} {position} {zlib.crc32(data)} {data.hex()}')
                    if reply['kind'] == 'error' and reply.get('error') in ('chunk_crc', 'chunk_arguments', 'offset'):
                        raise OSError('Retry block')
                    reply = self.check(reply, 'ack')
                    if reply['offset'] != position+len(data):
                        raise OSError('Resynchronize offset')
                    position = reply['offset']
                    retries = 0
                except (OSError, OtaError, RuntimeError):
                    retries += 1
                    if retries > 8:
                        raise OtaError('USB interrupted. Transfer is saved; reopen USB OTA with this same version to resume.')
                    self.report('USB interrupted; reconnecting to the same ESP32 and checking saved progress...', None)
                    self.sleep(1)
                    try:
                        self.transport.reconnect()
                        self._resync = True
                        ready = self.check(self.exchange(f'uota_begin {len(image)} {digest}', 60), 'ready')
                        position = ready['offset']
                    except (OSError, OtaError, RuntimeError):
                        continue
            if position != len(image):
                raise OtaError('Invalid final USB OTA offset')
            self.report('Verifying the complete image on ESP32 before selecting it for boot...', 100)
            try:
                reply = self.exchange('uota_finish ' + digest, 90)
                if reply['kind'] == 'error':
                    return OtaResult('failed', 'ESP32 rejected the complete image. Boot slot preserved; discard this transfer before retrying.')
                self.check(reply, 'applied')
            except (OSError, OtaError, RuntimeError):
                # A missing final ACK may be the successful reboot itself.
                pass
            return self.verify(tag, before['next'])
        except OtaError as exc:
            return OtaResult('unconfirmed' if self.requested else 'failed', str(exc))
        except Exception:
            return OtaResult('unconfirmed' if self.requested else 'failed',
                             'USB OTA could not finish. Check the connection; use the same version to resume.')
