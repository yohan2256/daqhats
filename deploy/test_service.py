"""Check the generated unit with systemd's parser; no service is installed."""
import importlib.util
from pathlib import Path
import pwd
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('render_service', Path(__file__).with_name('render_service.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ServiceUnitTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd parser unavailable')
    def test_service_in_checkout_with_spaces_and_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'Pi 100% measurement'
            server = root / 'pi_server'
            server.mkdir(parents=True)
            (server / 'pislm.py').write_text('')
            config = server / 'config.ini'
            config.write_text('[test]\noriginal=true\n')
            venv = root / 'venv'
            (venv / 'bin').mkdir(parents=True)
            (venv / 'bin/python').symlink_to(sys.executable)
            unit = Path(tmp) / 'pislm-portable.service'
            unit.write_text(module.render(root, pwd.getpwuid(os.getuid()).pw_name, venv, config))
            result = subprocess.run(['systemd-analyze', 'verify', str(unit)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config.read_text(), '[test]\noriginal=true\n')


if __name__ == '__main__':
    unittest.main()
