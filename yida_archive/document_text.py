"""正文提取；扫描版 PDF 使用本地 OCR，不执行宏或外部链接。"""
import io
from functools import lru_cache
import zipfile
import shutil
import subprocess
import tempfile
from pathlib import Path


SUPPORTED = {'.pdf', '.docx', '.xlsx', '.xls'}
IMAGES = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tif', '.tiff', '.webp', '.heic', '.svg'}
OCR_IMAGES = {'.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff', '.webp'}
SUPPORTED |= OCR_IMAGES | {'.doc'}


class TextError(ValueError):
    pass


def extract(data, extension, max_chars=48000, max_units=120):
    """返回带定位信息的正文；超过界限整体拒绝，避免截断后误归档。"""
    ext = extension.lower()
    if ext not in SUPPORTED:
        raise TextError('首期不识别此格式的正文')
    if len(data) > 64 * 1024 * 1024:
        raise TextError('文档超过64MB识别上限')
    if ext in {'.docx', '.xlsx'}:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if sum(i.file_size for i in z.infolist()) > 128 * 1024 * 1024:
                    raise TextError('文档解压内容过大')
        except zipfile.BadZipFile:
            raise TextError('文档格式损坏或已加密') from None
    sections = []
    ocr_document = None
    total = 0

    def add(location, text):
        nonlocal total
        text = str(text or '').strip()
        if not text:
            return
        total += len(text)
        if total > max_chars:
            raise TextError('正文超过识别长度上限，未截断归档')
        sections.append({'location': location, 'text': text})

    try:
        if ext in OCR_IMAGES:
            import pymupdf as fitz
            from PIL import Image
            with Image.open(io.BytesIO(data)) as picture:
                if picture.width * picture.height > 30_000_000:
                    raise TextError('图片像素超过识别上限')
                if getattr(picture, 'n_frames', 1) != 1:
                    raise TextError('多帧图片需先转换为PDF，避免遗漏内容')
                converted = io.BytesIO()
                picture.convert('RGB').save(converted, format='PNG')
            ocr_document = fitz.open(stream=converted.getvalue(), filetype='png')
            add('第1页', _ocr_page(ocr_document[0]))
        elif ext == '.doc':
            reader = shutil.which('antiword')
            if not reader:
                raise TextError('缺少旧版Word读取器antiword，请安装或转为DOCX')
            with tempfile.TemporaryDirectory(prefix='archive-doc-') as directory:
                source = Path(directory) / 'document.doc'
                source.write_bytes(data)
                completed = subprocess.run([reader, '-m', 'UTF-8.txt', str(source)],
                                           capture_output=True, timeout=30, check=False)
                if completed.returncode:
                    raise TextError('旧版Word正文读取失败，请转为DOCX')
                add('正文1', completed.stdout.decode('utf-8', errors='strict'))
        elif ext == '.pdf':
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            # 部分PDF仅限制编辑/打印，打开密码为空，仍可正常读取正文。
            if reader.is_encrypted and not reader.decrypt(''):
                raise TextError('PDF需要打开密码，未能读取正文')
            if len(reader.pages) > max_units:
                raise TextError('PDF页数超过识别上限')
            for n, page in enumerate(reader.pages, 1):
                contents = page.get_contents()
                if contents and len(contents.get_data()) > 10 * 1024 * 1024:
                    raise TextError('PDF页面内容流过大')
                text = page.extract_text() or ''
                # 不让混合扫描文档凭少量文字被误认成已全文核验。
                if not text.strip():
                    if ocr_document is None:
                        import pymupdf as fitz
                        ocr_document = fitz.open(stream=data, filetype='pdf')
                    text = _ocr_page(ocr_document[n - 1])
                    if not text.strip():
                        raise TextError('扫描页未识别到文字，请人工确认')
                add('第%d页' % n, text)
        elif ext == '.docx':
            from docx import Document
            doc = Document(io.BytesIO(data))
            for n, p in enumerate(doc.paragraphs, 1):
                add('段落%d' % n, p.text)
            for n, table in enumerate(doc.tables, 1):
                for r, row in enumerate(table.rows, 1):
                    add('表格%d行%d' % (n, r), ' | '.join(c.text for c in row.cells))
            for n, section in enumerate(doc.sections, 1):
                add('节%d页眉' % n, '\n'.join(p.text for p in section.header.paragraphs))
                add('节%d页脚' % n, '\n'.join(p.text for p in section.footer.paragraphs))
        elif ext == '.xlsx':
            from openpyxl import load_workbook
            try:
                book = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
            except TypeError as exc:
                if 'openpyxl.styles.fills.Fill' not in str(exc):
                    raise
                # 仅修复内存副本的填充样式，保留单元格、公式和数字/日期格式。
                import xml.etree.ElementTree as ET
                buf = io.BytesIO()
                with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(buf, 'w') as target:
                    for item in source.infolist():
                        body = source.read(item)
                        if item.filename == 'xl/styles.xml':
                            tree = ET.fromstring(body)
                            ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
                            fills = tree.find(ns + 'fills')
                            if fills is None:
                                raise TextError('工作簿缺少填充样式定义')
                            for fill in fills:
                                fill.clear()
                                ET.SubElement(fill, ns + 'patternFill', {'patternType': 'none'})
                            body = ET.tostring(tree, encoding='utf-8', xml_declaration=True)
                        target.writestr(item, body)
                book = load_workbook(io.BytesIO(buf.getvalue()), read_only=True, data_only=False, keep_links=False)
            try:
                if len(book.worksheets) > max_units:
                    raise TextError('工作表数量超过识别上限')
                for sheet in book.worksheets:
                    if sheet.max_row > 10000 or sheet.max_column > 200:
                        raise TextError('工作表范围超过识别上限')
                    for n, row in enumerate(sheet.iter_rows(values_only=True), 1):
                        if any(v is not None for v in row):
                            add('工作表%s第%d行' % (sheet.title, n),
                                ' | '.join('' if v is None else str(v) for v in row))
            finally:
                book.close()
        elif ext == '.xls':
            import xlrd
            book = xlrd.open_workbook(file_contents=data, on_demand=True)
            try:
                if book.nsheets > max_units:
                    raise TextError('工作表数量超过识别上限')
                for sheet in book.sheets():
                    if sheet.nrows > 10000 or sheet.ncols > 200:
                        raise TextError('工作表范围超过识别上限')
                    for n in range(sheet.nrows):
                        add('工作表%s第%d行' % (sheet.name, n + 1),
                            ' | '.join(str(v) for v in sheet.row_values(n)))
            finally:
                book.release_resources()
    except ImportError:
        raise TextError('缺少正文提取依赖，请安装requirements.txt') from None
    except TextError:
        raise
    except Exception:
        raise TextError('文档损坏、加密或正文无法解析') from None
    finally:
        if ocr_document is not None:
            ocr_document.close()
    if sum(len(s['text']) for s in sections) < 20:
        raise TextError('未提取到足够正文，请人工确认')
    return sections


@lru_cache(maxsize=1)
def _ocr_engine():
    from rapidocr import RapidOCR
    return RapidOCR()


def _ocr_page(page):
    """Render a bounded page and recognize Chinese and English locally."""
    import numpy as np
    import pymupdf as fitz
    rect = page.rect
    scale = min(2.0, 2400 / max(rect.width, rect.height))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
    image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    result = _ocr_engine()(image)
    return '\n'.join(result.txts or [])
