"""使用合成文字测试模型连通性，不上传业务文档、不输出密钥。"""
import argparse
import contextlib
import copy
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yida_archive.config import Config, load
from yida_archive import pipeline, fetch_local


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.yaml')
    args = parser.parse_args()
    cfg = load(args.config)
    try:
        from docx import Document
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / '输入'
            source = root / '测试上传' / '附件'
            source.mkdir(parents=True)
            doc = Document()
            doc.add_paragraph('PACKING LIST 装箱单。正式出货文件。货物为实验仪器配件，共10箱，净重100kg，毛重120kg。')
            original = source / '测试文档.docx'
            doc.save(original)
            raw = copy.deepcopy(cfg.raw)
            raw['scope'] = {'project_no': 'AI-SMOKE', 'purchaser': '测试'}
            raw['storage'] = {'work_dir': str(Path(tmp) / 'cache'), 'out_dir': str(Path(tmp) / 'output')}
            test_cfg = Config(raw)
            instances = fetch_local.load(root)
            with contextlib.redirect_stdout(io.StringIO()):
                preview = pipeline.run(test_cfg, instances, dry_run=True, verbose=False)
                written = pipeline.run(test_cfg, instances, client=fetch_local.LocalClient([root]),
                                       dry_run=False, verbose=False)
            f = written.files[0]
            success = (preview.files[0].type_name == '箱单' and f.type_name == '箱单'
                       and len(written.done) == 1 and f.ai_result.get('cached')
                       and Path(written.done[0][1]).read_bytes() == original.read_bytes())
            print('模型：%s；状态：%s；类型：%s；调用Token：%s；预览/缓存/实际归档：%s' %
                  (f.ai_result.get('model', cfg.raw.get('ai', {}).get('model', '')),
                   f.ai_result.get('status', ''), f.type_name,
                   f.ai_result.get('usage', {}).get('total_tokens', '未知'), '通过' if success else '失败'))
            if not success:
                print(f.review_note)
            return 0 if success else 1
    except Exception:
        print('模型或归档验证失败，请检查配置与运行依赖')
        return 1


if __name__ == '__main__':
    sys.exit(main())
