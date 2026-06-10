# Security Policy

## Supported version

Security fixes apply to the latest release on the `main` branch.

## Reporting a vulnerability

If you find a security issue, please **do not** open a public GitHub issue.

Email or DM the maintainer privately with:

- Description of the vulnerability
- Steps to reproduce
- Impact assessment (if known)

Will aim to respond within 7 days.

## Scope

**openstax-flash** is a read-only CLI that:

- Fetches publicly available pages from [openstax.org](https://openstax.org)
- Writes output only to paths you specify (`-o`) or stdout
- Stores HTTP cache in `~/.cache/openstax-flash` by default
- Requires **no API keys, passwords, or credentials**

## Out of scope

- Content accuracy of OpenStax textbook definitions
- OpenStax website availability or rate limiting
- How you use exported flashcard files after generation

## Safe usage notes

- Do not commit generated `.tsv` / `.txt` decks if they contain course-specific notes you added
- Run from a trusted network; the tool makes outbound HTTPS requests only to openstax.org
- Review exported files before importing into Anki or sharing
