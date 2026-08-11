---
name: documents
description: Read, create, summarize, and reorganize Microsoft Word .docx documents. Use for Word files, DOCX content extraction, document drafting, and converting structured text into a Word artifact.
---

# Documents

1. Inspect existing `.docx` files with `read_docx`; continue with `next_block` until the required scope is covered.
2. Preserve the user's language, heading hierarchy, lists, and factual meaning.
3. Create the final artifact with `write_docx`; provide Markdown-like headings and lists in `content`.
4. Re-read the output with `read_docx` and verify the title, required sections, and non-empty content.
5. Never use `read_file` or `write_file` for binary Office files.
