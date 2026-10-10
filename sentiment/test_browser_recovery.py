import unittest
from unittest.mock import patch

from sentiment import sources


class BrowserRecoveryTest(unittest.TestCase):
    def test_launch_chrome_without_url(self):
        with patch.object(sources, "_chrome_exe", return_value="chrome.exe"), \
             patch.object(sources.subprocess, "Popen") as popen:
            self.assertTrue(sources._launch_chrome())
        self.assertEqual(popen.call_args.args[0], ["chrome.exe"])

    def test_no_healthy_profile_reports_browser_failure(self):
        with patch.object(sources, "_ensure_opencli_browser", return_value=([], [])), \
             patch.object(sources, "_opencli_default_profile", return_value="retired-edge"):
            with self.assertRaisesRegex(RuntimeError, "no healthy whitelisted"):
                sources._run_opencli(["opencli", "twitter", "search", "$INTC"], timeout=1)


if __name__ == "__main__":
    unittest.main()
