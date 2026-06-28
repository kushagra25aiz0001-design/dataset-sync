# Contributing to Dataset Sync

Thank you for your interest in contributing! Here's how to get started.

## Development Setup

```bash
git clone https://github.com/your-username/Dataset_Sync.git
cd Dataset_Sync
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

## Code Style

- Follow PEP 8 for Python code
- Use type hints for function signatures
- Write docstrings for all public functions and classes
- Keep functions focused and under 50 lines where possible

## Commit Messages

Use conventional commit format:
- `feat:` new feature
- `fix:` bug fix
- `docs:` documentation changes
- `refactor:` code refactoring
- `test:` adding or modifying tests

## Pull Request Process

1. Fork the repository
2. Create a feature branch (`git checkout -b feat/my-feature`)
3. Make your changes and add tests
4. Run tests: `pytest tests/`
5. Submit a pull request with a clear description
