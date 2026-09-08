"""Local printed Chinese discharge-table adapter, never a patient-fact writer."""
from __future__ import annotations

import base64
import io
import re
import threading
import time
from importlib.metadata import version

from .product import ProductError, FIELDS, digest
from .memory import utc_now

MAX_BYTES = 6 * 1024 * 1024
MAX_PAGES = 3
MAX_PIXELS = 16_000_000
_ENGINE = None
_OCR_LOCK = threading.Lock()


def render_document(raw, mime):
    try:
        from PIL import Image, ImageOps
        pages = []
        if mime == 'application/pdf' and raw.startswith(b'%PDF-'):
            import pymupdf
            with pymupdf.open(stream=raw, filetype='pdf') as doc:
                if doc.is_encrypted or not 1 <= len(doc) <= MAX_PAGES:
                    raise ProductError('PDF 必须未加密且包含 1–3 页')
                for number, page in enumerate(doc):
                    if page.rect.width * page.rect.height * 4 > MAX_PIXELS:
                        raise ProductError('PDF 页面尺寸过大')
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                    pages.append({'page': number + 1, 'width': pix.width, 'height': pix.height, 'rotation': page.rotation,
                        'coordinate_system': 'rendered_pixels_top_left', 'pdf_scale': 2, 'png_base64': base64.b64encode(pix.tobytes('png')).decode()})
        elif mime in ('image/png', 'image/jpeg'):
            if not (raw.startswith(b'\x89PNG\r\n\x1a\n') if mime == 'image/png' else raw.startswith(b'\xff\xd8\xff')):
                raise ProductError('文件内容与声明格式不一致')
            with Image.open(io.BytesIO(raw)) as source:
                if source.width * source.height > MAX_PIXELS or getattr(source, 'n_frames', 1) != 1:
                    raise ProductError('图片尺寸过大或包含多帧')
                source.load()
                im = ImageOps.exif_transpose(source).convert('RGB')
                buf = io.BytesIO()
                im.save(buf, format='PNG')
                pages.append({'page': 1, 'width': im.width, 'height': im.height, 'rotation': 0,
                    'coordinate_system': 'rendered_pixels_top_left', 'exif_orientation_applied': True,
                    'png_base64': base64.b64encode(buf.getvalue()).decode()})
        else:
            raise ProductError('仅支持 PNG、JPEG 和 PDF；文件内容必须匹配格式')
        return pages
    except ImportError as exc:
        raise ProductError('本地 OCR 依赖未安装；仍可使用 CSV 导入', 503) from exc
    except ProductError:
        raise
    except Exception as exc:
        raise ProductError('材料无法打开或已损坏') from exc


def parse_pages(pages):
    global _ENGINE
    try:
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise ProductError('请安装 requirements-product-ocr.txt 后重试；CSV 不受影响', 503) from exc
    started = time.perf_counter()
    blocks = []
    with _OCR_LOCK:
        if _ENGINE is None:
            _ENGINE = RapidOCR()
        for page in pages:
            result = _ENGINE(base64.b64decode(page['png_base64']))
            if result.txts is None:
                continue
            for text, box, score in zip(result.txts, result.boxes, result.scores):
                polygon = box.tolist()
                xs, ys = [p[0] for p in polygon], [p[1] for p in polygon]
                blocks.append({'text': text, 'score': float(score), 'bbox': [min(xs), min(ys), max(xs), max(ys)],
                               'polygon': polygon, 'page': page['page'], 'coordinate_system': page['coordinate_system']})
    candidates = table_candidates(blocks)
    import rapidocr
    from pathlib import Path
    from .evidence_quality import file_fingerprint
    return {'parser_version': f'discharge-table-v1/rapidocr-{version("rapidocr")}',
            'models': ['PP-OCRv6_det_small', 'ch_ppocr_mobile_v2.0_cls_mobile', 'PP-OCRv6_rec_small'],
            'model_sha256': {p.name: file_fingerprint(p) for p in (Path(rapidocr.__file__).parent / 'models').glob('*.onnx')},
            'blocks': blocks, 'candidates': candidates, 'elapsed_ms': (time.perf_counter() - started) * 1000,
            'score_meaning': 'OCR recognition score, not probability of medical correctness'}


def table_candidates(blocks):
    candidates = []
    headers = {'药名': 'name', '药品名称': 'name', '剂量': 'dose', '单位': 'unit', '频次': 'schedule', '途径': 'route'}
    for page in sorted({b['page'] for b in blocks}):
        rows = [b for b in blocks if b['page'] == page]
        columns = sorted([b for b in rows if b['text'].strip() in headers], key=lambda b: b['bbox'][0])
        if not {'name', 'dose', 'unit', 'schedule'}.issubset({headers[c['text'].strip()] for c in columns}):
            continue
        subject_block = next((b for b in rows if re.search(r'患者[：:]', b['text'])), None)
        date_block = next((b for b in rows if re.search(r'\d{4}-\d{2}-\d{2}', b['text'])), None)
        subject_match = re.search(r'患者[：:]\s*([^\s]+?)(?:\s*日期|$)', subject_block['text']) if subject_block else None
        day = re.search(r'\d{4}-\d{2}-\d{2}', date_block['text']).group() if date_block else None
        top = max(c['bbox'][3] for c in columns)
        anchors = []
        # Anchor on any aligned cell, so a blank/illegible drug-name cell is
        # retained as missing input instead of silently dropping the row.
        for block in sorted(rows, key=lambda b: b['bbox'][1]):
            if block['bbox'][1] <= top or len(block['text']) > 30 or min(abs(block['bbox'][0] - c['bbox'][0]) for c in columns) >= 100:
                continue
            center = (block['bbox'][1] + block['bbox'][3]) / 2
            if not any(abs(center - (a['bbox'][1] + a['bbox'][3]) / 2) < 30 for a in anchors):
                anchors.append(block)
        for anchor in anchors:
            center_y = (anchor['bbox'][1] + anchor['bbox'][3]) / 2
            band = [b for b in rows if abs((b['bbox'][1] + b['bbox'][3]) / 2 - center_y) < max(25, (anchor['bbox'][3] - anchor['bbox'][1]) * .8)]
            # Footer prose is not a medication row: require a second aligned cell.
            if len(band) < 2:
                continue
            fields = {f: None for f in FIELDS}
            locations, scores = {}, {}
            for block in band:
                col = min(columns, key=lambda c: abs(c['bbox'][0] - block['bbox'][0]))
                field = headers[col['text'].strip()]
                if field in locations:
                    fields[field] = None  # ambiguous split/merged cell requires correction
                    continue
                fields[field] = block['text'].strip()
                locations[field] = {k: block[k] for k in ('page', 'bbox', 'polygon', 'coordinate_system')}
                scores[field] = block['score']
            fields.update(subject=subject_match.group(1).strip() if subject_match else None, date=day)
            for field, block in [('subject', subject_block), ('date', date_block)]:
                if block:
                    locations[field] = {k: block[k] for k in ('page', 'bbox', 'polygon', 'coordinate_system')}
                    locations[field]['granularity'] = 'text_block'
            candidates.append({'fields': fields, 'original_fields': dict(fields), 'locations': locations,
                'ocr_scores': scores, 'ocr_reviewed': False, 'corrections': []})
    return candidates


class DocumentImports:
    def __init__(self, product):
        self.p = product

    def upload(self, key, encoded, mime):
        if not isinstance(encoded, str) or len(encoded) > MAX_BYTES * 4 // 3 + 4:
            raise ProductError('上传材料不得超过 6 MB')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ProductError('材料编码无效') from exc
        if not raw or len(raw) > MAX_BYTES:
            raise ProductError('上传材料为空或超过 6 MB')
        pages = render_document(raw, mime)
        def execute():
            import uuid
            document_id = f'document:{digest(raw)}'
            self.p.save('document', {'id': document_id, 'scope_id': 'local-demo', 'sha256': digest(raw), 'raw_base64': encoded,
                'mime': mime, 'pages': pages, 'created_at': utc_now()})
            job = {'id': f'parse:{uuid.uuid4().hex}', 'document_id': document_id, 'status': 'ready', 'attempts': 0, 'created_at': utc_now(), 'case_id': None}
            self.p.save('parse_job', job)
            return job
        return self.p.command(key, {'type': 'document_upload', 'sha256': digest(raw), 'mime': mime}, execute)

    def parse(self, job_id):
        with self.p.transaction():
            job = self.p.get(job_id, 'parse_job')
            if job['status'] == 'completed':
                return job
            if job['status'] == 'running' and time.time() < job.get('lease_until', 0):
                raise ProductError('材料正在解析，请稍后刷新', 409)
            if job['attempts'] >= 3:
                raise ProductError('已达到 3 次解析上限，请检查文件后重新导入', 409)
            job.update(status='running', attempts=job['attempts'] + 1, lease_until=time.time() + 180)
            self.p.save('parse_job', job)
            doc = self.p.get(job['document_id'], 'document')
        try:
            parsed = parse_pages(doc['pages'])  # No patient writer lock held during OCR.
            job['parser_output'] = parsed
            if not parsed['candidates']:
                raise ProductError('没有识别到支持的用药表：请使用清晰、含药名/剂量/单位/频次表头的打印表格')
            with self.p.transaction():
                current = self.p.get(job_id, 'parse_job')
                if current['attempts'] != job['attempts'] or current['status'] != 'running':
                    raise ProductError('解析任务已由另一次尝试接管', 409)
                case = self.p.stage_candidates(base64.b64decode(doc['raw_base64']), parsed['candidates'], parsed['parser_version'], doc['mime'])
                job.update(status='completed', case_id=case['id'], parser_output=parsed, completed_at=utc_now())
                self.p.save('parse_job', job)
                return job
        except Exception as exc:
            with self.p.transaction():
                current = self.p.get(job_id, 'parse_job')
                if current['attempts'] == job['attempts']:
                    job.update(status='failed', error=str(exc))
                    self.p.save('parse_job', job)
            if isinstance(exc, ProductError):
                raise
            raise ProductError('本地解析失败，可在材料列表重试', 503) from exc


def register_document_routes(app, product, access, invoke):
    from fastapi import Request
    globals()['Request'] = Request
    imports = DocumentImports(product)

    @app.get('/v1/materials/parse-jobs')
    def jobs(request: Request):
        access(request)
        return {'items': [{k: v for k, v in j.items() if k != 'parser_output'} for j in product.objects('parse_job')]}

    @app.post('/v1/materials/document')
    def upload(request: Request, body: dict):
        access(request, True)
        return invoke(lambda: imports.upload(body.get('key'), body.get('base64'), body.get('mime')))

    @app.post('/v1/materials/parse-jobs/{job_id}/parse')
    def parse(job_id: str, request: Request):
        access(request, True)
        return invoke(lambda: imports.parse(job_id))

    @app.get('/v1/materials/documents/{document_id}')
    def document(document_id: str, request: Request):
        access(request)
        doc = invoke(lambda: product.get(document_id, 'document'))
        if digest(base64.b64decode(doc['raw_base64'])) != doc['sha256']:
            return invoke(lambda: (_ for _ in ()).throw(ProductError('原件完整性校验失败', 409)))
        return {k: v for k, v in doc.items() if k != 'raw_base64'}
