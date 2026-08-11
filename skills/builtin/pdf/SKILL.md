---
name: pdf
description: Read, summarize, reorganize, and create PDF files. Use for PDF page extraction, long-document processing, PDF-to-text workflows, and generating a final PDF artifact with Chinese text support.
---

# PDF

1. Inspect input PDFs with `read_pdf` in page ranges; follow `next_page` and preserve progress incrementally for long documents.
2. Stop with a precise explanation when the tool reports a password requirement or `pdf_ocr_required`.
3. Build a new PDF with `write_pdf` from concise Markdown-like headings, paragraphs, and lists.
4. Verify a generated PDF with `read_pdf` and confirm that required text is extractable.
5. Do not install ad-hoc PDF packages or invoke `pdftotext`; use the provided tools.
