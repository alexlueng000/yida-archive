import io
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import test_ai_classification as helpers
decision, SECTIONS = helpers.decision, helpers.SECTIONS
from yida_archive import ai_classify as ai, document_text, classify


class ToleranceTests(unittest.TestCase):
    setUp = helpers.AIClassificationTest.setUp
    tearDown = helpers.AIClassificationTest.tearDown
    apply = helpers.AIClassificationTest.apply
    def test_underlines_quotes_and_page_aliases(self):
        sections = [{'location': '第10页', 'text': '须保证 _PRECISE 公司满足“招投标”文件约定'}]
        result = decision()
        result['evidence'] = [{'location': 'Page 10', 'quote': '须保证 PRECISE 公司满足"招投标"文件约定'}]
        actual = ai.validate(result, sections, self.cfg.rules, 0.9)
        self.assertEqual(actual['status'], 'classified')
        self.assertEqual(actual['evidence'][0]['location'], '第10页')

    def test_one_bad_extra_quote_does_not_discard_verified_evidence(self):
        result = decision()
        result['evidence'].append({'location': '段落1', 'quote': '金额为999万元'})
        actual = ai.validate(result, SECTIONS, self.cfg.rules, 0.9)
        self.assertEqual(actual['status'], 'classified')
        self.assertEqual(len(actual['evidence']), 1)
        self.assertEqual(actual['evidence_rejected'], 1)

    def test_numbers_and_negation_still_must_match(self):
        for quote in ('货物数量100件', '货物数量不是10件'):
            result = dict(decision(), evidence=[{'location': '段落1', 'quote': quote}])
            with self.assertRaises(ai.AIError):
                ai.validate(result, SECTIONS, self.cfg.rules, 0.9)

    def test_verified_medium_score_is_saved_as_suggested(self):
        f, _ = self.apply(result=decision(score=0.8))
        self.assertEqual((f.type_name, f.confidence), ('箱单', classify.SUGGESTED))
        self.assertEqual(f.ai_result['status'], 'suggested')
        self.assertIn('待复核', f.review_note)

    def test_suggestion_floor_configurable(self):
        self.cfg.raw['ai']['suggestion_threshold'] = 0.85
        f, _ = self.apply(result=decision(score=0.8))
        self.assertEqual(f.ai_result['status'], 'unknown')

    def test_unknown_png_uses_extracted_text(self):
        f, count = self.apply(name='扫描件.png')
        self.assertEqual(f.type_name, '箱单')
        self.assertEqual(count, 1)

    def test_old_doc_reader_and_missing_dependency(self):
        result = SimpleNamespace(returncode=0, stdout='正式装箱单，货物数量10件，净重100千克，毛重120千克。'.encode())
        with patch.object(document_text.shutil, 'which', return_value='/usr/bin/antiword'), patch.object(document_text.subprocess, 'run', return_value=result) as run:
            sections = document_text.extract(b'legacy doc', '.doc')
            self.assertEqual(sections[0]['location'], '正文1')
            self.assertIn('装箱单', sections[0]['text'])
            self.assertFalse(run.call_args.kwargs.get('shell', False))
        with patch.object(document_text.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(document_text.TextError, 'antiword'):
                document_text.extract(b'legacy doc', '.doc')

    def test_actual_png_decode_to_local_ocr(self):
        from PIL import Image
        image = io.BytesIO()
        Image.new('RGB', (40, 40), 'white').save(image, format='PNG')
        with patch.object(document_text, '_ocr_page', return_value='正式装箱单，货物数量10件，净重100千克，毛重120千克。'):
            sections = document_text.extract(image.getvalue(), '.png')
        self.assertEqual(sections[0]['location'], '第1页')
        self.assertIn('装箱单', sections[0]['text'])
