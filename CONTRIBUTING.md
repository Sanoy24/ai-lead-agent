# Contributing

Thanks for your interest in improving ai-lead-agent.

## Getting started

1. Fork the repository and clone your fork.
2. Install [uv](https://docs.astral.sh/uv/) and set up the environment:

   ```bash
   uv sync
   uv run pre-commit install
   ```

3. Copy `.env.example` to `.env` and adjust it if needed.
4. Run the checks:

   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy
   uv run pytest
   ```

## Making changes

- `main` is protected. Create a branch from `main`, e.g. `feat/sms-channel` or `fix/follow-up-timing`, and open a pull request. CI must pass before it can be merged.
- Keep pull requests focused on one change.
- Add or update tests for any behavior change.
- Update `CHANGELOG.md` under **Unreleased**.

## Commit messages

This project follows [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add SMS channel
fix: respect quiet hours for follow-ups
docs: explain Cal.com setup
```

Common types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`.

## Reporting bugs and requesting features

Use the issue templates. For security problems, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Code of conduct

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
