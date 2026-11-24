"""Isolated PDF text extraction (CP11 S11.1; decision 0061 §2).

Run as a separate process (``python -I extract_worker.py --max-pages N --max-chars M``) with
the PDF's bytes on stdin; writes one JSON object on stdout. PDFium parses hostile input in C:
a crash or a hang ends this process, never the hub or a run worker, and the caller reports the
file as refused with a reason. Imports only the standard library and pypdfium2 (never
PrometheOS), reads nothing but stdin and writes nothing but stdout.

The reply is ``{"ok": true, "engine", "page_count", "info", "pages": [{"raw": text} or
{"raw": null, "state": "too_long"}]}`` or ``{"ok": false, "reason", "detail"}`` with
``reason`` one of ``encrypted``, ``malformed``, ``too_many_pages``, ``no_pages`` or
``text_too_large`` (more characters than ``--max-total-chars`` in all: the child stops at the
page that crosses it, before extracting it).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

INFO_KEYS = {
    "Title": "title",
    "Author": "author",
    "Subject": "subject",
    "Keywords": "keywords",
    "Creator": "creator",
    "Producer": "producer",
}
PASSWORD_ERROR = 4  # FPDF_ERR_PASSWORD
SECURITY_ERROR = 5  # FPDF_ERR_SECURITY


def extract(data: bytes, max_pages: int, max_chars: int, max_total: int) -> dict[str, Any]:
    import pypdfium2
    import pypdfium2.version as pdfium_version

    engine = {
        "name": "pypdfium2",
        "version": str(pdfium_version.PYPDFIUM_INFO),
        "pdfium": str(pdfium_version.PDFIUM_INFO),
    }
    try:
        document = pypdfium2.PdfDocument(data)
    except pypdfium2.PdfiumError as exc:
        code = getattr(exc, "err_code", None)
        if code in (PASSWORD_ERROR, SECURITY_ERROR):
            return {"ok": False, "reason": "encrypted", "detail": "the PDF needs a password"}
        return {"ok": False, "reason": "malformed", "detail": "PDFium could not read the file"}
    try:
        count = len(document)
        if count < 1:
            return {"ok": False, "reason": "no_pages", "detail": "the PDF has no pages"}
        if count > max_pages:
            return {
                "ok": False,
                "reason": "too_many_pages",
                "detail": f"{count} pages; the limit is {max_pages}",
            }
        info: dict[str, str | None] = dict.fromkeys(INFO_KEYS.values())
        try:
            for key, value in document.get_metadata_dict(skip_empty=True).items():
                if key in INFO_KEYS and isinstance(value, str):
                    info[INFO_KEYS[key]] = value[:1000]
        except pypdfium2.PdfiumError:
            pass
        pages: list[dict[str, Any]] = []
        total = 0  # characters counted so far: the child stops before the reply grows large
        for index in range(count):
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    chars = textpage.count_chars()
                    if chars > max_chars * 2:
                        pages.append({"raw": None, "state": "too_long"})
                        continue
                    total += max(chars, 0)
                    if total > max_total:
                        return {
                            "ok": False,
                            "reason": "text_too_large",
                            "detail": f"more than {max_total} characters by page {index + 1}",
                        }
                    raw = textpage.get_text_range()
                finally:
                    textpage.close()
            finally:
                page.close()
            if len(raw) > max_chars * 2:
                pages.append({"raw": None, "state": "too_long"})
            else:
                pages.append({"raw": raw})
        return {"ok": True, "engine": engine, "page_count": count, "info": info, "pages": pages}
    finally:
        document.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pages", type=int, required=True)
    parser.add_argument("--max-chars", type=int, required=True)
    parser.add_argument("--max-total-chars", type=int, required=True)
    args = parser.parse_args(argv)
    data = sys.stdin.buffer.read()
    reply = extract(data, args.max_pages, args.max_chars, args.max_total_chars)
    # UTF-8 (lone surrogates passed through; the parent removes them), not ASCII escapes, so
    # the reply stays close to the size of the text itself.
    out = json.dumps(reply, ensure_ascii=False).encode("utf-8", "surrogatepass")
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
