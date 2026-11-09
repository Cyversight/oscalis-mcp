# Security policy

## Reporting a vulnerability

Please report security problems privately. Do not open a public issue.

Use GitHub's private vulnerability reporting:
<https://github.com/Cyversight/oscalis-mcp/security/advisories/new>

Include the version, how to reproduce the problem, and what you think the impact is. Reports are handled on a
best-effort basis by Cyversight LLC. There is no bug bounty.

## Scope

In scope: the server code in `src/oscalis/`, the SSE bearer-token authentication and bind guard, and the importers in
`src/scripts/`.

Out of scope:

- The content of the NIST, FedRAMP and GovRAMP data files. Report content errors in those sources to their publishers.
  Report a mistake in how Oscalis reads or maps them as an ordinary issue.
- SSE exposed beyond a trusted network without TLS. The bearer token is a single shared secret over plain HTTP, and the
  README says to put TLS in front of it. There is no per-user identity, rate limiting or audit log.

## Supported versions

Only the latest release receives fixes.
