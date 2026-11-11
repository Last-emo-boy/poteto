import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from execution import host
from publisher.controlflow import ObservationUnavailable
from publisher.executor import BuildOutput


class BuildOutputTests(unittest.TestCase):
    def test_subprocess_output_is_available_before_the_build_exits(self):
        with tempfile.TemporaryDirectory() as temp, ThreadPoolExecutor(max_workers=1) as pool:
            root = Path(temp)
            job = root / 'builds' / ('a' * 24); job.mkdir(parents=True)
            log = job / 'build.log'; release = root / 'continue'
            command = [sys.executable, '-u', '-c', '''import sys, time
from pathlib import Path
sys.stderr.write('compile started\\n')
while not Path(sys.argv[1]).exists(): time.sleep(0.01)
sys.stderr.write('finished\\n')
print('{"state":"succeeded"}')
''', str(release)]
            future = pool.submit(host.collect_build_output, command, log, 10)
            try:
                deadline = time.monotonic() + 5
                with patch.object(host, 'ROOT', root):
                    while time.monotonic() < deadline:
                        value = host.build_log_chunk('a' * 24, 0)
                        if value['data']:
                            break
                        time.sleep(0.01)
                    self.assertEqual(b'compile started\n', base64.b64decode(value['data']))
                    self.assertFalse(future.done())
                    self.assertEqual({'data': ''}, host.build_log_chunk('a' * 24, 16))
            finally:
                release.touch()
            self.assertEqual({'state': 'succeeded'}, future.result(timeout=5))
            self.assertEqual(b'compile started\nfinished\n', log.read_bytes())

    def test_log_cap_does_not_block_receipt(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(host, 'BUILD_LOG_LIMIT', 128):
            log = Path(temp) / 'build.log'
            command = [sys.executable, '-c', 'import sys; sys.stderr.write("x" * 100000); print(\'{"state":"succeeded"}\')']
            self.assertEqual({'state': 'succeeded'}, host.collect_build_output(command, log, 5))
            self.assertEqual(128, log.stat().st_size)

    def test_utf8_and_final_partial_line_survive_transport_boundaries(self):
        client = Mock(); messages = []
        client.build_log_chunk.side_effect = [b'first\n\xe4\xbd', ObservationUnavailable('disconnected'), b'\xa0\r\nlast', b'']
        reader = BuildOutput(client, 'fixture', messages.append)
        reader.read(); self.assertEqual(['first'], messages)
        reader.read(); self.assertEqual(['first'], messages)
        reader.read(final=True)
        self.assertEqual(['first', '你', 'last'], messages)
        self.assertEqual([0, 8, 8, 15], [call.args[1] for call in client.build_log_chunk.call_args_list])


if __name__ == '__main__': unittest.main()
