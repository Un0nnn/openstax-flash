# openstax-flash

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![OpenStax](https://img.shields.io/badge/source-OpenStax-green.svg)](https://openstax.org)

Generate flashcards from **OpenStax** textbook Key Terms, Glossary, and Summary pages — exported ready for **Anki**, Quizlet, or JSON/Markdown pipelines.

Works with **70+ live OpenStax books** on [openstax.org](https://openstax.org). Stdlib-only Python — no `pip install` required.

**[Download & get started](#download--get-started)** · **[Report a bug](https://github.com/Un0nnn/openstax-flash/issues/new?template=bug_report.yml)** · **[Contributing](CONTRIBUTING.md)**

---

## Table of contents

- [Download & get started](#download--get-started)
- [Features](#features)
- [Commands](#commands)
- [Examples](#examples)
- [Export formats](#export-formats)
- [Example books](#example-books)
- [How it works](#how-it-works)
- [Limitations](#limitations)
- [Community](#community)
- [License](#license)

---

## Download & get started

### Requirements

- **Python 3.10+** (`python3 --version`)
- Internet access (fetches glossary pages from openstax.org)

### 1. Download

**Option A — Git clone (recommended)**

```bash
git clone https://github.com/Un0nnn/openstax-flash.git
cd openstax-flash
```

**Option B — Download ZIP**

1. Open [github.com/Un0nnn/openstax-flash](https://github.com/Un0nnn/openstax-flash)
2. Click **Code → Download ZIP**
3. Unzip and open a terminal in the folder

**Option C — Single file**

Download only [`openstax_flash.py`](https://raw.githubusercontent.com/Un0nnn/openstax-flash/main/openstax_flash.py) and run it from any directory.

### 2. Make it executable (optional)

```bash
chmod +x openstax_flash.py
```

### 3. Run your first command

```bash
# List physics books
python3 openstax_flash.py list --query physics

# See chapters for a book
python3 openstax_flash.py chapters university-physics-volume-1

# Export a full book as an Anki deck
python3 openstax_flash.py extract university-physics-volume-1 --format anki -o up1-deck.txt
```

If you used `chmod +x`, you can also run:

```bash
./openstax_flash.py list --query physics
```

### 4. Import into Anki

1. Open Anki → **File → Import**
2. Select your `.txt` file (e.g. `up1-deck.txt`)
3. Type: **Fields separated by Tab**
4. Map: Field 1 → **Front**, Field 2 → **Back**, Field 3 → **Tags**
5. Click **Import**

For math-heavy books (calculus, physics), use `--format anki-latex` and a note type with MathJax enabled.

### 5. Verify a book before a full export

```bash
python3 openstax_flash.py verify --sample college-physics-2e
```

---

## Why this exists

Every OpenStax chapter ends with structured vocabulary: Key Terms, chapter glossaries, or summary bullets with bolded concepts. Students manually copy these into Anki decks. **openstax-flash** automates that in seconds.

Built for learners using OpenStax texts (physics, chemistry, biology, calculus, nursing, economics, and more).

---

## Features

- **70+ books supported** — verified against live OpenStax Rex books
- **All chapters by default** — or filter with `--chapters 1-5`
- **Math-aware** — converts MathML in definitions to readable text and `$LaTeX$`
- **Multiple book layouts** — standard glossaries, linked key-term indexes (organic/chemistry), summary bullets (microbiology/nursing)
- **Parallel fetching** with disk cache for fast re-runs
- **Export formats** — Anki TSV, Anki+LaTeX, CSV, JSON, Markdown, Quizlet

---

## Commands

| Command | Description |
|---------|-------------|
| `list` | List all OpenStax books (`--query`, `--subject`) |
| `chapters <slug>` | Show term pages per chapter for a book |
| `extract <slug>` | Download terms and export flashcards |
| `verify` | Check which books have extractable term pages (`--sample` for live test) |

### Extract options

| Flag | Description |
|------|-------------|
| `--format`, `-f` | `anki` (default), `anki-latex`, `tsv`, `csv`, `json`, `md`, `quizlet` |
| `--output`, `-o` | Output file (default: stdout) |
| `--chapters`, `-c` | Filter: `3`, `1,4,7`, or `1-5` (default: **all chapters**) |
| `--all` | Explicit all-chapters mode (same as default) |
| `--concurrency` | Parallel page fetches (default: 8) |
| `--no-cache` | Disable HTTP cache |
| `-v` | Verbose progress |

---

## Examples

```bash
# Full book → Anki deck
python3 openstax_flash.py extract university-physics-volume-1 --format anki -o up1-deck.txt

# Math-heavy book → LaTeX for Anki
python3 openstax_flash.py extract calculus-volume-1 --format anki-latex -o calc-deck.txt

# Specific chapters only
python3 openstax_flash.py extract college-physics-2e --chapters 1-5 --format anki -o cp2e-ch1-5.txt

# JSON for app integration
python3 openstax_flash.py extract chemistry-2e --format json -o chemistry.json -v

# Quizlet import
python3 openstax_flash.py extract psychology-2e --format quizlet -o psych.csv
```

---

## Export formats

| Format | Output |
|--------|--------|
| `anki` | Tab-separated with header: Front, Back, Tags |
| `anki-latex` | Same as Anki, definitions use `$LaTeX$` for equations |
| `tsv` | Plain TSV without header |
| `csv` | Full CSV with metadata columns |
| `json` | Structured JSON (`term`, `definition`, `definition_latex`, chapter, tags) |
| `md` | Markdown grouped by chapter |
| `quizlet` | Quizlet-compatible CSV |

---

## Example books

| Slug | Cards (full book) | Notes |
|------|-------------------|-------|
| `university-physics-volume-1` | ~321 | Key Terms + MathML |
| `college-physics-2e` | ~938 | Chapter glossaries |
| `calculus-volume-1` | ~196 | Heavy math → use `anki-latex` |
| `chemistry-2e` | ~763 | Standard glossaries |
| `organic-chemistry` | varies | Linked terms; fetches section definitions |
| `microbiology` | varies | Summary bullets with bold terms |
| `biology-2e` | ~2,358 | Large deck — filter chapters |

---

## How it works

1. Resolves book slug via OpenStax CMS API
2. Loads the book TOC from Rex SSR pages
3. Finds chapter term pages (`*-key-terms`, `*-glossary`, `*-summary`)
4. Prefers key-terms → glossary → summary per chapter (no duplicates)
5. Fetches pages in parallel, parses HTML glossaries
6. Converts MathML equations to plain text and LaTeX
7. Exports in your chosen format

Cache directory: `~/.cache/openstax-flash`

---

## Limitations

- Requires OpenStax Rex web pages (books hosted on openstax.org)
- A few books have no glossary structure (e.g. `algebra-1`, `writing-guide`)
- Linked-term books (e.g. organic chemistry) make extra requests per chapter
- Summary-only books extract bold terms from bullet points, not formal definitions

---

## Community

| | |
|---|---|
| **Report a bug** | [Bug report](https://github.com/Un0nnn/openstax-flash/issues/new?template=bug_report.yml) |
| **Book not working** | [Book support](https://github.com/Un0nnn/openstax-flash/issues/new?template=book_support.yml) |
| **Request a feature** | [Feature request](https://github.com/Un0nnn/openstax-flash/issues/new?template=feature_request.yml) |
| **Contribute code** | [CONTRIBUTING.md](CONTRIBUTING.md) |
| **Security** | [SECURITY.md](SECURITY.md) (private reports only) |

### Maintainer

**[@Un0nnn](https://github.com/Un0nnn)** — project author and maintainer

Pull requests from the community are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

### Contributors

Community contributors who merge PRs appear on the [GitHub contributors graph](https://github.com/Un0nnn/openstax-flash/graphs/contributors). Thank you to everyone who reports issues and improves the tool.

---

## License

**openstax-flash** (this tool) is licensed under the [MIT License](LICENSE).

Exported flashcard text is derived from [OpenStax](https://openstax.org) textbooks (CC BY-licensed content). This tool does not redistribute full books — only user-requested glossary excerpts.
