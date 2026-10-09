# Security policy

## Reporting a vulnerability

Please **do not open a public issue**. Report privately through
[GitHub security advisories](https://github.com/murtazox04/Cue/security/advisories/new).
You will get an acknowledgement within a few days and a fix or mitigation plan as soon as
the issue is understood. Credit is given in the release notes unless you prefer otherwise.

## Supported versions

Security fixes are released for the latest minor version.

## Hardening checklist for operators

- Run behind TLS; Cue does not terminate TLS itself.
- Give each integration its own API key with the narrowest scopes it needs
  (`events:write` for producers, `messages:read` for dashboards). Reserve `admin`.
- Supply channel credentials through environment variables or a secret store, not files
  committed to version control.
- Use PostgreSQL in production, with a role limited to the Cue database.
- When AI personalisation is enabled, only attributes listed in a template's
  `ai_attributes` are sent to the model provider — review those lists.
- Run `cuectl prune` on a schedule to enforce your data-retention policy.
