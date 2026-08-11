---
name: presentations
description: Read and create PowerPoint .pptx presentations. Use for extracting slide text, summarizing decks, drafting slide structure, and generating a local presentation artifact.
---

# Presentations

1. Read existing decks with `read_pptx`; follow `next_slide` until the required range is covered.
2. Design one message per slide with short titles and concise bullets; avoid paragraph-sized bullets.
3. Create the deck with `write_pptx` using a structured `slides` array.
4. Re-read the generated deck with `read_pptx` and verify slide count, order, titles, and required content.
5. Never use text file tools for binary `.pptx` files.
