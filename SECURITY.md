# Security policy

## Reporting a vulnerability

Please **do not open a public issue**. Report privately through
[GitHub security advisories](https://github.com/murtazox04/Cue/security/advisories/new).
You will get an acknowledgement within a few days and a fix or mitigation plan as soon as
the issue is understood. Credit is given in the release notes unless you prefer otherwise.

## Supported versions

Security fixes are released for the latest minor version.

## What Cue protects against

Cue sits between software that wants someone's attention and that person. It assumes
the operator and `admin` keys are trusted, and treats everything else as untrusted:

| Who | What they can do | What they cannot |
|---|---|---|
| Keys with narrow scopes (your services) | Report events, send templates, read messages | Create keys, change rules or templates, see review links |
| Agent-bound keys | Send within their agent's budget, categories and importance ceiling | Change preferences or recipient profiles and addresses, approve anything, see other agents' messages, claim another producer's events, forge Cue's own events |
| Anyone on the internet | Use a token they were sent: preference page, one-click unsubscribe, review link | Read or change anything without a token; tokens have 192–256 bits of randomness |
| Recipients' own webhook URLs | Receive their messages | Reach private networks (only public addresses, pinned against DNS rebinding, no redirects) |
| AI providers | Rephrase a rendered message, judge risk | Change numbers, dates or links (checked in code), see more than allow-listed attributes |

Request bodies are capped before they are read, tokens in paths are kept out of logs,
templates run in Jinja's sandbox, and the hosted pages send a strict Content Security
Policy.

## Verifying what you install

- **Container image:** signed build provenance and an SBOM are attached to every
  release.

  ```sh
  gh attestation verify oci://ghcr.io/murtazox04/cue:0.3 -R murtazox04/Cue
  ```

- **PyPI** (`cue-notify`, `cue-notify-client`): published through trusted publishing
  with PEP 740 attestations, shown on each file's PyPI page.
- **npm** (`cue-client`): published with provenance (`npm audit signatures`).

Every GitHub Action is pinned to a commit SHA; CodeQL, OpenSSF Scorecard, `pip-audit`,
`npm audit` and zizmor run in CI.

## Hardening checklist for operators

- Run behind TLS; Cue does not terminate TLS itself. Expose only the API, `/preferences`
  and `/review` (the Helm chart's ingress does this); keep `/metrics` internal.
- Give each integration its own API key with the narrowest scopes it needs
  (`events:write` for producers, `messages:read` for dashboards). Reserve `admin`.
- Bind every AI agent's key to an agent, and give the agent `reviewers`, an `approval`
  condition for risky messages, and a small `frequency_caps` budget.
- Supply channel credentials through environment variables or a secret store, not files
  committed to version control.
- Use PostgreSQL in production, with a role limited to the Cue database.
- Limit request sizes at your proxy too (Cue refuses bodies over `api.max_body_bytes`).
- Redact tokens in your proxy's access log: `/preferences/…`, `/review/…` and
  `/v1/track/…` paths carry credentials. Cue redacts its own logs.
- When AI personalisation is enabled, only attributes listed in a template's
  `ai_attributes` are sent to the model provider — review those lists.
- Run `cuectl prune` on a schedule to enforce your data-retention policy. Keep the
  consent ledger as long as you may need to prove consent.
- Receivers of per-recipient webhooks: one signing secret covers all of them, so also
  check that the payload's `address` is your own endpoint.
