# Contributing to openstax-flash

Thanks for helping improve a free study tool for OpenStax learners.

## Ways to contribute

- **Report bugs** — [open a bug report](https://github.com/Un0nnn/openstax-flash/issues/new?template=bug_report.yml)
- **Request features** — [feature request](https://github.com/Un0nnn/openstax-flash/issues/new?template=feature_request.yml)
- **Flag a broken book** — [book support issue](https://github.com/Un0nnn/openstax-flash/issues/new?template=book_support.yml)
- **Submit a PR** — fork, branch, test, open a pull request

## Development setup

```bash
git clone https://github.com/Un0nnn/openstax-flash.git
cd openstax-flash
python3 openstax_flash.py --help
```

No dependencies to install — stdlib only.

## Before you open a PR

1. Test your change against at least one book:

   ```bash
   python3 -m py_compile openstax_flash.py
   python3 openstax_flash.py verify --sample university-physics-volume-1
   ```

2. Do **not** commit:
   - Generated flashcard files (`.tsv`, `*-deck.txt`, `.apkg`)
   - `__pycache__/` or virtualenvs
   - Secrets or credentials

3. Keep PRs focused — one fix or feature per pull request.

## Commit messages

Use clear, short messages:

```
Fix MathML fraction parsing for calculus definitions
Add quizlet export example to README
```

Use a GitHub-linked email on your commits so they appear on your profile ([GitHub docs](https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-email-preferences/setting-your-commit-email-address)).

## Security

Do **not** open public issues for security vulnerabilities. See [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
