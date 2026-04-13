# Security Policy

## Reporting Security Vulnerabilities

⚠️ **Do NOT open public GitHub issues for security vulnerabilities.**

If you discover a security vulnerability in TorpTradingBot, please report it responsibly to avoid potential misuse before a fix is available.

### How to Report

**GitHub Security Advisory:** 
   https://github.com/SoderTorp/TorpTradingBot/security/advisories/new

### Information to Include

Please provide as much detail as possible:

```
- Type of vulnerability (e.g., injection, authentication bypass, data exposure)
- Location in code (file path, line number if possible)
- Description of the vulnerability
- Potential impact and severity assessment
- Proof of concept or steps to reproduce (if safe to share)
- Suggested remediation (optional)
```

### Response Timeline

| Severity | Acknowledgment | Fix Target | Disclosure |
|----------|---|---|---|
| Critical | 4 hours | 24-48 hours | 7 days after fix |
| High | 8 hours | 3-5 days | 14 days after fix |
| Medium | 24 hours | 1-2 weeks | 30 days after fix |
| Low | 48 hours | 2-4 weeks | 60 days after fix |

## Supported Versions

| Version | Status | Security Updates | End of Life |
|---------|--------|---|---|
| 1.x | Current | ✅ Active | TBD |
| 0.x | Deprecated | ⚠️ Limited | Immediate |

Only the current version receives active security updates.

## Security Best Practices for Contributors

### Credential Management
- ❌ **Never** commit API keys, tokens, or passwords
- ✅ Use `.env` files for local configuration
- ✅ Add secrets to `.gitignore` before committing
- ✅ Use GitHub Secrets for CI/CD workflows
- ✅ Rotate credentials if accidentally exposed

### Commit Signing
All commits must be signed with a GPG key:

```bash
# Configure Git
git config user.signingkey <YOUR_GPG_KEY_ID>
git config commit.gpgsign true

# Create signed commit
git commit -S -m "message"
```

### Code Review Standards
- All changes require peer review
- Must pass automated security scans
- No merge without code owner approval
- Address all security findings before merge

### Development Practices
- Use strong, unique passwords (12+ characters)
- Enable two-factor authentication (2FA)
- Keep local development environment updated
- Use HTTPS for all remote connections
- Validate and sanitize all external inputs

### Dependency Management
- Review `requirements.txt` changes carefully
- Use `pip-audit` to check for known vulnerabilities:
  ```bash
  pip install pip-audit
  pip-audit
  ```
- Keep dependencies up-to-date
- Monitor Dependabot alerts

## Security Controls in Place

### Automated Scanning
- **CodeQL Analysis:** Detects security vulnerabilities in Python code
- **Secret Scanning:** Prevents accidental exposure of credentials
- **Push Protection:** Blocks commits containing secrets
- **Dependency Scanning:** Identifies vulnerable packages
- **SAST (Static Analysis):** Analyzes code for security issues

### Access Control
- Two-factor authentication required for all contributors
- Code owners must approve changes to sensitive files
- Signed commits required on main branch
- Deploy protections on production environment

### Monitoring & Audit
- All commits are logged with signatures
- Security alerts sent to maintainers
- Weekly automated security reviews
- Monthly manual security audits

## Sensitive Files & Configurations

The following files/directories require heightened security:

```
config.yaml           - Contains Alpaca API configuration
.env*                - Local environment variables
secrets/              - Credential storage
strategies/wheel.py   - Trading logic
strategies/politician.py - Trade logic
```

Changes to these files:
- Require code owner approval
- Trigger additional security scans
- May require security team review
- Are logged in audit trail

## Known Security Considerations

### Current Limitations
1. **Ollama Integration:** Runs on localhost (11434) - not exposed to internet
2. **API Credentials:** Stored in config (use environment variables in production)
3. **Trade Logic:** No formal verification of correctness
4. **Data Validation:** Input validation against market limits recommended

### Mitigation Strategies
- Use environment variables instead of config files for credentials
- Implement rate limiting on API calls
- Monitor trading activity for anomalies
- Regular code audits of financial logic
- Unit tests for critical functions

## Compliance & Standards

This project adheres to:

- **OWASP Top 10:** https://owasp.org/www-project-top-ten/
- **CWE (Common Weakness Enumeration):** https://cwe.mitre.org/
- **NIST Cybersecurity Framework:** https://www.nist.gov/cyberframework
- **Python Security Standards:** PEP 480 (Secure Supply Chains)

## Dependencies & Supply Chain Security

### Current Dependencies

See `requirements.txt` for full list. Key dependencies:

| Package | Purpose | Monitored |
|---------|---------|-----------|
| alpaca-trade-api | Trading API client | ✅ Dependabot |
| requests | HTTP client | ✅ Dependabot |
| pyyaml | YAML parsing | ✅ Dependabot |
| ollama | AI model client | ✅ Manual review |

### Dependency Verification

All dependencies are:
- Listed in `requirements.txt` with pinned versions
- Scanned by GitHub Dependabot
- Reviewed for known vulnerabilities
- Updated via automated PRs
- Tested before merge

## Incident Response

If a vulnerability is discovered after public disclosure:

1. **Immediate:** Create security advisory draft
2. **1-2 hours:** Assess impact and severity
3. **2-4 hours:** Develop patch/fix
4. **4-8 hours:** Test and review patch
5. **8+ hours:** Release patch version
6. **Coordination:** Notify affected users
7. **Public:** Update advisory and document

## Security Badges

Security status is indicated by badges in the README:

```markdown
![GHAS Secret Scanning](https://img.shields.io/badge/secret%20scanning-active-brightgreen)
![GHAS Code Scanning](https://img.shields.io/badge/code%20scanning-CodeQL-brightgreen)
![Dependabot](https://img.shields.io/badge/dependabot-active-brightgreen)
```

## Questions?

For security-related questions:
- Email: security@example.com
- Open a discussion (non-sensitive topics only)
- Do not open GitHub issues about vulnerabilities

## License

This security policy is part of TorpTradingBot and is licensed under the MIT License.

---

**Last Updated:** April 2026  
**Policy Version:** 1.0  
**Maintained by:** SoderTorp