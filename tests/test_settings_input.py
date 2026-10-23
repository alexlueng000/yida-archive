"""执行真实前端脚本的输入回归，防止仅 HTTP 接口通过但页面无法编辑。"""
import shutil
import subprocess
import unittest
from pathlib import Path


class TestSettingsInput(unittest.TestCase):
    def test_project_edit_survives_refresh_and_stale_response(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('需要 Node.js 执行页面脚本')
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([node, str(root / 'tests/settings_input.cjs'),
                                 str(root / 'web/static/index.html')],
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
