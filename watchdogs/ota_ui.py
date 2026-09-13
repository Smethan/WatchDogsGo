"""Wi-Fi OTA menu. Serial ownership is transferred only on explicit start."""
from queue import Queue, Empty
import threading
import os

import pyxel

from .firmware_ota import OtaRunner, SerialOtaTransport, validate_release, wifi_command
from .updates import release_version


class OtaMixin:
    def _open_ota(self):
        if getattr(self, '_ota_screen', False):
            return
        if getattr(self, '_serial_busy', False) or getattr(self, '_flash_io_active', False):
            self.msg('[OTA] Finish the current serial/flash operation first', 10)
            return
        self._ota_screen = True
        self.menu_open = False
        self._ota_running = False
        self._ota_reserved = False
        self._ota_fields = ['', '']
        self._ota_field = 0
        self._ota_selection = 0
        self._ota_releases = []
        self._ota_loading = True
        self._ota_status = 'Loading published firmware releases...'
        self._ota_percent = None
        self._ota_result = None
        self._ota_events = Queue()
        events = self._ota_events
        def load():
            from .updates import firmware_releases
            try:
                releases = []
                for release in firmware_releases():
                    try:
                        validate_release(release)
                        releases.append(release)
                    except (ValueError, KeyError, TypeError):
                        continue
                events.put(('releases', releases))
            except Exception:
                events.put(('load_error', None))
        threading.Thread(target=load, daemon=True).start()

    def _start_ota(self):
        if self._ota_running or self._ota_result or not self._ota_releases:
            return
        try:
            wifi_command(*self._ota_fields)
        except ValueError as exc:
            self._ota_status = str(exc)
            return
        if not self.serial or not self.serial.is_open:
            self._ota_status = 'No firmware connection. Release BOOT, tap RESET, then reconnect WDG.'
            return
        from .firmware_flash import FlashTarget, select_target
        try:
            identity = self.serial.usb_port_info
            target = FlashTarget.from_port(identity) if identity else select_target(self.serial.device)
            if os.path.realpath(target.resolve()) != os.path.realpath(self.serial.device):
                raise RuntimeError('Port changed')
        except (RuntimeError, AttributeError):
            self._ota_status = 'Cannot identify connected ESP32. Reconnect WDG to the intended board.'
            return
        # GUI thread finishes its last poll before transferring ownership. Keep
        # the reservation even on failure until the user closes the result.
        self._flash_io_active = self._ota_reserved = True
        self._reconnect_flash_target = target
        self._pending_cmd = None
        self.wardrive.on_stop()
        self.wardrive.close_passive()
        self.wardrive.capture.finish('disconnected')
        self.sniffing = self.capturing_hs = False
        transport = SerialOtaTransport(self.serial, target)
        self.serial = None
        self.state.connected = self._esp32 = False
        self._ota_running = True
        ssid, password = self._ota_fields
        self._ota_fields[1] = ''
        release = self._ota_releases[self._ota_selection]
        events = self._ota_events
        def worker():
            try:
                runner = OtaRunner(transport, lambda text, pct: events.put(('status', (text, pct))))
                result = runner.run(release, ssid, password)
            finally:
                # Discard firmware command echoes before normal logging resumes.
                try:
                    transport.close()
                except Exception:
                    pass
            events.put(('done', result))
        threading.Thread(target=worker, daemon=True).start()

    def _update_ota_screen(self):
        while True:
            try:
                kind, value = self._ota_events.get_nowait()
            except Empty:
                break
            if kind == 'releases':
                self._ota_releases = value
                self._ota_loading = False
                self._ota_status = ('Enter ESP32 Wi-Fi details, select a release, then start.' if value
                                    else 'No compatible OTA releases found. ESC closes; reopen to retry.')
            elif kind == 'load_error':
                self._ota_loading = False
                self._ota_status = 'Cannot load GitHub releases. Check internet; ESC closes, reopen to retry.'
            elif kind == 'status':
                self._ota_status, self._ota_percent = value
            elif kind == 'done':
                self._ota_running = False
                self._ota_result = value
                self._ota_status = value.message
                self._fw_version = value.version  # Unknown stays unknown after a failed/unconfirmed update.
                try:
                    self._fw_update_available = release_version(value.version) < release_version(self._fw_remote_version)
                except (ValueError, AttributeError):
                    self._fw_update_available = False
                self._term_add('[OTA] ' + value.message, raw=True)
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            if self._ota_running:
                self._ota_status = 'Update is active. Keep ESP32 power on and wait for the result.'
                return
            self._ota_fields[1] = ''
            self._ota_screen = False
            if self._ota_reserved:
                self._flash_io_active = self._ota_reserved = False
                self._try_reconnect_esp32()
            return
        if self._ota_running or self._ota_result:
            return
        if pyxel.btnp(pyxel.KEY_TAB) or pyxel.btnp(pyxel.KEY_DOWN):
            self._ota_field = (self._ota_field + 1) % 4
        elif pyxel.btnp(pyxel.KEY_UP):
            self._ota_field = (self._ota_field - 1) % 4
        elif pyxel.btnp(pyxel.KEY_RETURN):
            if self._ota_field == 3:
                self._start_ota()
            else:
                self._ota_field += 1
        elif self._ota_field == 2 and self._ota_releases:
            if pyxel.btnp(pyxel.KEY_LEFT):
                self._ota_selection = (self._ota_selection - 1) % len(self._ota_releases)
            elif pyxel.btnp(pyxel.KEY_RIGHT):
                self._ota_selection = (self._ota_selection + 1) % len(self._ota_releases)
        elif self._ota_field < 2:
            if pyxel.btnp(pyxel.KEY_BACKSPACE, 10, 2):
                self._ota_fields[self._ota_field] = self._ota_fields[self._ota_field][:-1]
            else:
                char = self._get_char_input()
                if char and len(self._ota_fields[self._ota_field]) < 64:
                    self._ota_fields[self._ota_field] += char

    def _draw_ota_screen(self):
        import textwrap
        x, y, width, height = 36, 36, 568, 288
        pyxel.rect(x, y, width, height, 0)
        pyxel.rectb(x, y, width, height, 12)
        pyxel.text(x + 10, y + 9, 'ESP32 WI-FI OTA / Smethan projectZero', 12)
        pyxel.text(x + 10, y + 26, 'Firmware downloads through ESP32 Wi-Fi. USB carries setup/status only.', 13)
        pyxel.text(x + 10, y + 39, 'Requires the running Smethan fork v1.7.2+ with two OTA slots; BOOT must be released.', 10)
        values = [self._ota_fields[0] or '(blank: use ESP32 current Wi-Fi)',
                  '*' * len(self._ota_fields[1]) or '(blank: open network)',
                  self._ota_releases[self._ota_selection]['tag_name'] if self._ota_releases else '(loading/unavailable)',
                  'START WI-FI UPDATE']
        for i, label in enumerate(('SSID', 'PASSWORD', 'VERSION', '')):
            yy = y + 65 + i * 25
            selected = self._ota_field == i and not self._ota_running and not self._ota_result
            if selected:
                pyxel.rect(x + 8, yy - 4, width - 16, 18, 1)
            pyxel.text(x + 14, yy, label, 12 if selected else 13)
            pyxel.text(x + 76, yy, values[i][:115], 7)
        if self._ota_running:
            pct = self._ota_percent
            pyxel.rectb(x + 12, y + 168, width - 24, 9, 5)
            if pct is not None:
                pyxel.rect(x + 14, y + 170, int((width - 28) * pct / 100), 5, 12)
        color = 11 if self._ota_result and self._ota_result.state in ('success', 'current') else 10
        for i, line in enumerate(textwrap.wrap(self._ota_status, 132)[:3]):
            pyxel.text(x + 12, y + 185 + i * 11, line, color)
        pyxel.text(x + 12, y + 225, 'Password is masked and not logged by WDG; firmware may save it on its SD card.', 13)
        pyxel.text(x + 12, y + 238, 'Keep power connected. Scans stop for the update; no SD card is required.', 13)
        hint = ('Updating / verifying - please wait' if self._ota_running else
                'ESC returns (check ESP32 before retrying if result is unconfirmed)' if self._ota_result else
                'TAB/UP/DOWN field   LEFT/RIGHT version   ENTER on START   ESC cancel')
        pyxel.text(x + 12, y + 265, hint, 7)
