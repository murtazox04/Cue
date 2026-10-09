# AI personalisation

Cue can rewrite a message's title and body for each recipient — warmer, in their
language, mentioning their name — using any model supported by
[Pydantic AI](https://ai.pydantic.dev): OpenAI, Anthropic, Google, Groq, Mistral, Ollama
and OpenAI-compatible gateways, among others.

It is **off by default** and designed to be safe for transactional and financial
messaging.

## Enable it

```bash
pip install 'cue-notify[ai]'
```

```toml
[ai]
enabled = true
model = "openai:gpt-5-mini"                   # or "anthropic:claude-haiku-4-5",
fallback_models = ["google:gemini-2.5-flash"]  # "ollama:llama3.2", …
instructions = "Friendly and concise. Never use exclamation marks in SMS."
daily_token_budget = 2000000
```

Credentials are read from each provider's standard environment variable
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `OLLAMA_BASE_URL`…).

Then opt templates in:

```json
{
  "key": "renewal-reminder",
  "locales": {"en": {"title": "Your plan renews soon", "body": "Pro renews on {{ data.date }} for {{ data.price }} USD."}},
  "ai_instructions": "Mention how long they have been a customer if known.",
  "ai_attributes": ["first_name", "customer_since"]
}
```

## Guardrails

- **The template always renders first.** The model receives that text and may only
  rephrase it; links, images and data payloads never come from the model.
- **Facts must survive.** Every number and URL of the original must appear in the
  rewrite, and no new ones may be introduced (`1 250 000` and `1,250,000` are treated
  as equal). Violations are sent back to the model to fix; if it still fails, the
  template text is used.
- **Length limits** relative to the original keep SMS short.
- **Minimal data sharing.** Only the attributes in `ai_attributes` are sent, never the
  full profile, addresses or event payload.
- **Prompt-injection resistant framing.** Original text and recipient context are
  passed as data, and the model is instructed to ignore instructions inside them.
- **Never blocks delivery.** Timeouts, provider errors, budget exhaustion — the message
  is sent with the template text.

Personalisation happens at send time (after quiet hours and caps), so tokens are never
spent on messages that end up suppressed. Messages record `generated_by`
(`template` or `ai:<model>`).
