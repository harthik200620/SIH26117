"""Native Linux regression, also runnable directly with stdlib unittest in staging."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform.startswith("linux"), "resource meter requires Linux")
class ProcessMeterTest(unittest.TestCase):
    def test_meter_excludes_other_children_and_hides_report_descriptor(self) -> None:
        import importlib

        resource = importlib.import_module("resource")
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import time; start=time.process_time()\nwhile time.process_time()-start < 0.5: pass",
            ],
            check=True,
        )
        previous = resource.getrusage(resource.RUSAGE_CHILDREN)
        prior_cpu = previous.ru_utime + previous.ru_stime
        meter = Path(__file__).resolve().parents[2] / "yantra_server/sandbox/_meter.py"
        limits = json.dumps({"cpu_s": 5, "max_rss_mb": 256, "max_pids": 256})
        with tempfile.TemporaryFile() as report:
            code = f"import os\ntry: os.fstat({report.fileno()}); print('LEAK')\nexcept OSError: print('closed')"
            child = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(meter),
                    str(report.fileno()),
                    limits,
                    sys.executable,
                    "-I",
                    "-c",
                    code,
                ],
                pass_fds=(report.fileno(),),
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(child.stdout.strip(), "closed")
            report.seek(0)
            measured = json.load(report)
        self.assertGreaterEqual(prior_cpu, 0.5)
        self.assertGreater(measured["cpu_s"], 0)
        self.assertLess(measured["cpu_s"], prior_cpu / 2)
        self.assertGreater(measured["peak_rss_mb"], 0)


if __name__ == "__main__":
    unittest.main()
