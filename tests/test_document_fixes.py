import io
import json
import unittest
import zipfile
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from yida_archive import ai_classify as ai, document_text


class DocumentFixTests(unittest.TestCase):
    def test_zip_container_is_not_pending_or_classified(self):
        from web import projects, server
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'OUTPUT_ROOT', tmp):
            base = Path(tmp) / '单位' / 'P1'
            pending = base / '未归类'
            pending.mkdir(parents=True)
            (pending / '原包.zip').write_bytes(b'zip')
            path = '单位/P1/未归类/原包.zip'
            (base / '编号记录.json').write_text(json.dumps({'units': {'zip': {
                'path': path, 'type': '', 'file': {'name': '原包.zip',
                'ai_result': {'status': 'archive_container'}}}}}), encoding='utf8')
            result = projects.inventory({'purchaser': '单位', 'project_no': 'P1'})
            self.assertEqual(result[0]['folder'], '原始压缩包')
            self.assertFalse(result[0]['needs_review'])
            self.assertTrue(result[0]['archive_container'])
            self.assertEqual(result[0]['type'], '')

    def test_pdf_stream_and_separate_timeout(self):
        result = {'status': 'unknown', 'reason': 'test'}
        response = Mock(status_code=200)
        response.iter_lines.return_value = [
            b'data: ' + json.dumps({'choices': [{'delta': {'content': json.dumps(result)}, 'finish_reason': None}]}).encode(),
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            b'data: {"choices":[],"usage":{"total_tokens":10}}', b'data: [DONE]']
        with patch.object(ai, 'credentials', return_value=('key', 'https://example.invalid', 'model')), \
             patch.object(ai.requests, 'post', return_value=response) as post:
            output, usage = ai.Client({'timeout': 45}).classify({}, pdf_data=b'pdf')
        self.assertEqual(output, result)
        self.assertEqual(usage['total_tokens'], 10)
        self.assertEqual(post.call_args.kwargs['timeout'], (10, 300))
        self.assertTrue(post.call_args.kwargs['json']['stream'])
        response.close.assert_called_once()

    def test_truncated_stream_not_accepted(self):
        response = Mock(status_code=200)
        response.iter_lines.return_value = [b'data: {"choices":[{"delta":{"content":"{}"}}]}']
        with patch.object(ai, 'credentials', return_value=('key', 'https://example.invalid', 'model')), \
             patch.object(ai.requests, 'post', return_value=response), self.assertRaises(ai.AIError):
            ai.Client({}).classify({}, pdf_data=b'pdf')
        response.close.assert_called_once()

    def test_bad_fill_repaired_in_memory(self):
        from openpyxl import Workbook
        book = Workbook()
        book.active.append(['采购项目台账货物名称', '数量', '合计'])
        book.active.append(['设备', 10, '=B2*100'])
        buf = io.BytesIO(); book.save(buf)
        bad = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as source, zipfile.ZipFile(bad, 'w') as target:
            for item in source.infolist():
                body = source.read(item)
                if item.filename == 'xl/styles.xml':
                    body = body.replace(b'<patternFill/>', b'<invalidFill/>')
                target.writestr(item, body)
        data = bad.getvalue()
        text = str(document_text.extract(data, '.xlsx'))
        self.assertIn('采购项目台账货物名称', text)
        self.assertIn('=B2*100', text)
        self.assertEqual(data, bad.getvalue())
