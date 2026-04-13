# Contributing to TorpTradingBot

First off, thank you for considering contributing to TorpTradingBot! This document provides guidelines and instructions for contributing to the project.

## Code of Conduct

We are committed to providing a welcoming and inclusive environment. Please be respectful and constructive in all interactions.

## Before You Start

- **Read** [SECURITY.md](SECURITY.md) - Important security policies
- **Review** the project [README.md](README.md) to understand the project
- **Check** existing [Issues](https://github.com/SoderTorp/TorpTradingBot/issues) and [Pull Requests](https://github.com/SoderTorp/TorpTradingBot/pulls)

## Getting Started

### Prerequisites

- Python 3.8+
- Git with GPG signing capability
- GitHub account with two-factor authentication (2FA) enabled
- Ollama running locally (for AI features)
- Alpaca API account (for testing)

### Development Setup

```bash
# 1. Fork the repository
# Click "Fork" on GitHub

# 2. Clone your fork
git clone https://github.com/YOUR_USERNAME/TorpTradingBot.git
cd TorpTradingBot

# 3. Add upstream remote
git remote add upstream https://github.com/SoderTorp/TorpTradingBot.git

# 4. Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# 5. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt  # Optional: for development tools

# 6. Configure Git for signed commits
# See "Signing Commits" section below
```

## Signing Commits

All commits must be signed with a GPG key. This ensures code authenticity.

### Generate GPG Key (One-time setup)

```bash
# Generate a new GPG key
gpg --full-generate-key

# At the prompts:
# Key type: RSA and RSA (default)
# Key size: 4096
# Validity: 0 (no expiration, or set to 1y)
# Name: Your Name
# Email: your-github-email@example.com
# Passphrase: Strong password (you'll need this for each commit)

# List your keys to get the KEY_ID
gpg --list-secret-keys --keyid-format=long

# Output example:
# sec   rsa4096/3AA5C34371567BD2 2024-01-15
#       ^^^^^^^^^^^^^^^^^^^^^^^^^
#       Copy this KEY_ID
```

### Configure Git

```bash
# Set your default signing key
git config --global user.signingkey 3AA5C34371567BD2

# Enable automatic commit signing
git config --global commit.gpgsign true

# Verify configuration
git config --global --list | grep gpg
```

### Add Key to GitHub

1. Get your public key:
   ```bash
   gpg --armor --export 3AA5C34371567BD2
   ```

2. Copy the output (including `-----BEGIN PGP PUBLIC KEY BLOCK-----`)

3. Go to [GitHub Settings → SSH and GPG keys](https://github.com/settings/keys)

4. Click "New GPG key" and paste the key

### Make Signed Commits

```bash
# Normal workflow with automatic signing
git commit -m "Your message"

# Or explicitly sign a commit
git commit -S -m "Your message"

# Verify the commit is signed
git log --show-signature
```

## Development Workflow

### 1. Create a Feature Branch

```bash
# Update main branch
git checkout main
git pull upstream main

# Create a new branch
# Use format: feat/description, fix/description, docs/description, etc.
git checkout -b feat/add-new-strategy
```

### 2. Make Your Changes

```bash
# Edit files as needed
# Keep commits atomic and focused

# Add files
git add .

# Commit with descriptive message
git commit -S -m "feat(strategies): add new trading strategy

Detailed description of what this does.
- Key point 1
- Key point 2

Fixes #123"
```

### Commit Message Format

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types:**
- `feat` - New feature
- `fix` - Bug fix
- `docs` - Documentation changes
- `style` - Code style (formatting, missing semicolons, etc.)
- `refactor` - Code refactoring without feature changes
- `perf` - Performance improvements
- `test` - Adding or updating tests
- `chore` - Build, dependencies, or other non-code changes
- `security` - Security improvements or fixes

**Examples:**

```
feat(wheel): add strike price validation

Implements validation against CBOE strike price limits.
Prevents placing invalid orders that would be rejected.

Fixes #456
```

```
security(ollama): sanitize LLM input

Adds input validation to prevent prompt injection attacks.
Validates input length and character sets.

Refs #789
```

```
fix(politician): handle missing trade fields

Handle cases where politician trades lack complete field data.

Fixes #890
```

### 3. Write Tests

```bash
# Add tests for your changes
# Tests go in tests/ directory with naming convention: test_*.py

# Run tests locally
pytest tests/

# Check coverage
pytest --cov=strategies tests/
```

### 4. Run Security Checks

```bash
# Lint with pylint
pylint strategies/ ai/ main.py

# Security check with bandit
bandit -r strategies/ ai/ main.py

# Type checking with mypy (optional)
mypy strategies/ --strict

# Dependency audit
pip-audit

# Secret scanning locally (optional)
pip install detect-secrets
detect-secrets scan
```

### 5. Push Your Changes

```bash
# Push to your fork
git push origin feat/add-new-strategy
```

## Creating a Pull Request

### Before Opening PR

- [ ] Code follows project style
- [ ] Commits are signed
- [ ] Changes are tested
- [ ] Security checks pass
- [ ] Documentation is updated
- [ ] No hardcoded secrets or credentials

### PR Title Format

Follow the same format as commit messages:

```
feat(scope): brief description
```

### PR Description Template

```markdown
## Description
Brief description of what this PR does.

## Type of Change
- [ ] New feature
- [ ] Bug fix
- [ ] Documentation
- [ ] Security improvement
- [ ] Performance improvement

## Changes Made
- Change 1
- Change 2
- Change 3

## Testing Done
- Test 1 results
- Test 2 results

## Screenshots/Output (if applicable)
Include screenshots or command output if helpful.

## Issues Fixed
Fixes #123
Closes #456

## Checklist
- [ ] Code is signed
- [ ] Tests added/updated
- [ ] Documentation updated
- [ ] No breaking changes
- [ ] Security guidelines followed
- [ ] No hardcoded secrets
```

### PR Review Requirements

Your PR must:

✅ Pass all automated checks (CodeQL, tests, security scans)  
✅ Have approval from at least 1 code owner  
✅ Have signed commits  
✅ Have resolved conversations  
✅ Be up to date with main branch  

## Code Style Guide

### Python Style

- Follow [PEP 8](https://www.python.org/dev/peps/pep-0008/)
- Use 4 spaces for indentation
- Max line length: 100 characters (for readability)
- Use type hints where possible

### Code Structure

```python
# Imports at top
import os
from typing import Optional

# Constants
DEFAULT_TIMEOUT = 30

# Classes
class MyClass:
    """Docstring explaining the class."""
    
    def __init__(self, param: str):
        """Initialize with parameters."""
        self.param = param
    
    def method(self) -> str:
        """Document methods with docstrings."""
        return self.param

# Functions
def my_function(arg: str) -> bool:
    """Document public functions."""
    return bool(arg)
```

### Documentation

```python
def complex_function(trades: List[Trade], threshold: float = 0.5) -> Dict:
    """Calculate strategy signals based on recent trades.
    
    Args:
        trades: List of Trade objects to analyze
        threshold: Confidence threshold (0.0 to 1.0)
    
    Returns:
        Dictionary with signal and confidence
        
    Raises:
        ValueError: If trades list is empty or threshold is invalid
        
    Example:
        >>> signals = complex_function([trade1, trade2])
        >>> print(signals)
        {'signal': 'BUY', 'confidence': 0.85}
    """
```

## Testing Guidelines

### Writing Tests

```python
# tests/test_strategies.py
import pytest
from strategies.wheel import WheelStrategy

class TestWheelStrategy:
    """Test the wheel strategy."""
    
    @pytest.fixture
    def strategy(self):
        """Create a strategy instance for testing."""
        return WheelStrategy(tickers=["AAPL"])
    
    def test_calculate_premium(self, strategy):
        """Test premium calculation."""
        premium = strategy.calculate_premium(strike=150.0, price=151.0)
        assert premium > 0
    
    def test_invalid_strike(self, strategy):
        """Test validation of invalid strike prices."""
        with pytest.raises(ValueError):
            strategy.validate_strike(-100)
```

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=strategies --cov-report=html

# Run specific test file
pytest tests/test_strategies.py

# Run specific test
pytest tests/test_strategies.py::TestWheelStrategy::test_calculate_premium

# Verbose output
pytest -v
```

## Reporting Issues

### Bug Report

Include:
- Clear description of the bug
- Steps to reproduce
- Expected behavior
- Actual behavior
- Screenshots/logs if applicable
- Environment info (Python version, OS, etc.)

### Feature Request

Include:
- Clear description of the feature
- Use cases and benefits
- Potential implementation approach
- Any relevant references

### Security Issues

⚠️ **Do not open public issues for security vulnerabilities!**

See [SECURITY.md](SECURITY.md) for reporting procedures.

## Documentation

### Update When

- Adding new features
- Changing behavior
- Adding configuration options
- Fixing documentation errors

### Documentation Types

1. **Inline Comments** - Explain why, not what
   ```python
   # Alpaca requires a minimum 2-hour timeout for overnight positions
   timeout = 7200
   ```

2. **Docstrings** - Document public APIs
   ```python
   def execute_trade(self, symbol: str) -> Trade:
       """Execute a trade for the given symbol."""
   ```

3. **README** - Project overview and setup
4. **CONTRIBUTING** - This file, contribution guidelines
5. **API Docs** - Complex systems or new modules

## Performance Considerations

- Avoid N+1 API calls
- Cache results when possible
- Use async for I/O operations
- Monitor Ollama response times
- Test with realistic market data

## Security Checklist

Before submitting a PR:

- [ ] No hardcoded secrets or API keys
- [ ] Input validation on external data
- [ ] Appropriate error handling
- [ ] No SQL injection possibilities
- [ ] Proper authentication/authorization
- [ ] Sensitive data not logged
- [ ] Dependencies are secure

## Getting Help

- **Discussions:** [GitHub Discussions](https://github.com/SoderTorp/TorpTradingBot/discussions)
- **Issues:** [GitHub Issues](https://github.com/SoderTorp/TorpTradingBot/issues)
- **Security:** See [SECURITY.md](SECURITY.md)

## Recognition

Contributors are recognized:
- In commit messages and PR descriptions
- In release notes for significant contributions
- In a CONTRIBUTORS file (coming soon)

## License

By contributing, you agree that your contributions will be licensed under the MIT License. See [LICENSE](LICENSE) for details.

## Questions?

Don't hesitate to ask! Open a discussion or issue if you're unsure about anything.

---

**Thank you for contributing to TorpTradingBot!** 🚀

Last updated: April 2026