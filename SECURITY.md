# Security policy

## Reporting a vulnerability

Report it privately through GitHub:
[open a draft security advisory](https://github.com/Haibread/phare/security/advisories/new).
Only the maintainers and you can see it, and the fix is prepared there.

Do not describe a vulnerability in a public issue, pull request or discussion: it would be
disclosed before it is fixed.

A useful report holds:

- the affected version or commit
- the steps to reproduce
- the impact as you see it

## Supported versions

Phare is not released as versioned packages: the `main` branch is the only supported version.

## Scope

In scope, in Phare's own code and its container images:

- the backend HTTP API: authentication, per-account isolation, token handling, source tokens
  encrypted at rest (see [`docs/auth.md`](docs/auth.md))
- the chat agent's guardrails (see [`docs/agent.md`](docs/agent.md))
- the frontend SPA
- the defaults shipped in `docker-compose.yaml` and `.env.example`

Out of scope:

- a vulnerability in a dependency that is already public upstream: report it there, and the
  dependency bot brings the fix in
- a finding that needs an already-compromised host or account
- scanner output with no path to exploitation

## What happens next

1. Your report is acknowledged.
2. The maintainers assess it and confirm or decline it.
3. The fix is prepared in the advisory's private fork.
4. The fix lands on `main`.
5. The advisory is published, with a CVE requested through GitHub when the impact warrants one,
   and you are credited unless you prefer otherwise.

Reports are handled on a best-effort basis.
