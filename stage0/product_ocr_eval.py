"""Measured local parser dev set; generated variants are not held-out data."""
import argparse
import json
from pathlib import Path
from .document_parser import render_document, parse_pages


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).parent.parent / 'docs/product-upgrade/p5/samples'
    expected = [('氨氯地平', '5', 'mg', '每日一次', '2026-09-08', 'local-demo'), ('阿司匹林', '100', 'mg', '每日一次', '2026-09-08', 'local-demo')]
    rows = []
    for filename, mime, repeat in [('clear-table.png', 'image/png', 1), ('tilted-blurred.png', 'image/png', 1), ('scanned-table.pdf', 'application/pdf', 1), ('two-pages.pdf', 'application/pdf', 2)]:
        raw = (root / filename).read_bytes()
        result = parse_pages(render_document(raw, mime))
        candidates = result['candidates']
        correct = sum(str(candidates[i]['fields'].get(field)) == wanted[j] for i, wanted in enumerate(expected * repeat) if i < len(candidates)
                      for j, field in enumerate(('name', 'dose', 'unit', 'schedule', 'date', 'subject')))
        denominator = len(expected) * repeat * 6
        rows.append({'file': filename, 'group': 'synthetic-printed-table-v1', 'fields_total': denominator, 'fields_correct': correct,
            'critical_field_errors': denominator - correct, 'rows_requiring_human_review': len(candidates),
            'elapsed_ms': result['elapsed_ms'], 'parser_version': result['parser_version'], 'models': result['models']})
    # Actual text-layer baseline on the scanned PDF, without calling another OCR.
    import pymupdf
    with pymupdf.open(root / 'scanned-table.pdf') as pdf:
        baseline_chars = sum(len(p.get_text().strip()) for p in pdf)
    result = {'status': 'pass' if all(r['critical_field_errors'] == 0 for r in rows if r['file'] == 'clear-table.png') else 'fail',
        'track': 'synthetic_development_only', 'dataset_version': 'printed-table-v1', 'rows': rows,
        'parser_comparison': {'pymupdf_native_text_chars_on_scan': baseline_chars, 'rapidocr_fields_correct_on_clear_image': rows[0]['fields_correct']},
        'selection': 'RapidOCR CPU for scans; PyMuPDF for bounded rendering',
        'held_out': 'unavailable', 'unsupported': ['handwriting', 'medicine_boxes', 'complex_reports'],
        'note': 'Blur/rotation/multipage share a generated template, so no independent accuracy claim.'}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    print(result['status'])
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
