"""The literature library (A02, CP11; decisions 0060 and 0061).

S11.1: a folder of PDFs under an approved research root becomes a located, searchable library.
``library import --plan/--commit`` reads PDFs (never writing into the folder), keeps each
file's bytes as a source version, extracts per-page text with pypdfium2 in a separate process
into a ``pdf_pages_v1`` version derived from it, and registers one ``work`` per distinct PDF.
Page text is indexed for page-level search; reads are bounded located chunks; a quotation is
checked against the stored page text. Extracted text is untrusted data, never instructions.

Modules: ``contracts`` (the payload and plan contracts), ``text`` (normalisation and
identifier detection), ``extract`` (the isolated extraction), ``index`` (rebuildable derived
tables), ``importer`` (plan, commit and their run handlers) and ``views`` (works, search,
located reads and quote checks).
"""
