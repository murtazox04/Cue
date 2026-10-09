# Cue

**Intelligent notification orchestration service for fintech products**

Cue receives events from CRM systems or product analytics, evaluates triggers with JSON Logic, and delivers personalized notifications via Firebase push or Twilio SMS. Built with FastAPI, Dramatiq workers, and PostgreSQL.

---

## 📚 Table of Contents

- [Overview](#overview)
- [Quick Start](#quick-start)
  - [Local Development](#local-development)
  - [Docker Development](#docker-development)
- [Project Structure](#project-structure)
- [Architecture & Design](#architecture--design)
- [Development Workflow](#development-workflow)
- [Deployment](#deployment)
- [Documentation](#documentation)

---

## Overview

### Tech Stack

- **API**: FastAPI with Pydantic validation
- **Workers**: Dramatiq for background job processing
- **Database**: PostgreSQL with Piccolo ORM
- **Cache/Queue**: Redis for jobs, rate limiting, and caching
- **Notifications**: Firebase Cloud Messaging (push), Twilio (SMS)
- **AI**: OpenAI for content personalization
- **Deployment**: Docker Compose (dev), Docker (production)

### Key Features

✅ **Event-driven triggers** with JSON Logic for flexible conditions
✅ **Rate limiting** per user, per trigger, with daily/weekly caps
✅ **Smart timing optimization** using Thompson Sampling algorithm
✅ **AI-powered content** generation with template fallbacks
✅ **Multi-channel delivery** (push, SMS) with automatic failover
✅ **Real-time analytics** and conversion tracking
✅ **Webhook signature verification** for secure CRM integration

---

## Quick Start

### Prerequisites

- **uv** (Python package manager) - [Install Guide](https://docs.astral.sh/uv/getting-started/installation/)
- **Docker & Docker Compose** (for full stack deployment)
- **PostgreSQL 16+** and **Redis 7+** (if running locally without Docker)

### Local Development

**1. Clone and configure:**

```bash
git clone <repository-url>
cd cue
cp config/.env.example config/.env
```

**2. Install dependencies:**

```bash
uv sync  # Creates .venv and installs all dependencies from uv.lock
```

> **Important:** We use `uv` exclusively. Never use `pip install` or `uv pip`.
> Use `uv sync` to install, `uv add <package>` to add deps, and `uv lock` to update lockfile.

**3. Configure environment:**

Edit `config/.env` and set required variables:

```env
# Database
CUE_DB_HOST=localhost
CUE_DB_PORT=5432
CUE_DB_NAME=cue
CUE_DB_USER=cue
CUE_DB_PASSWORD=your_password

# Redis
CUE_REDIS_HOST=localhost
CUE_REDIS_PORT=6379

# Firebase (optional for push notifications)
CUE_FIREBASE_CREDENTIALS_PATH=path/to/firebase-credentials.json
CUE_FIREBASE_PROJECT_ID=your-project-id

# Twilio (optional for SMS)
CUE_SMS_PROVIDER=twilio
CUE_SMS_ACCOUNT_SID=your_account_sid
CUE_SMS_AUTH_TOKEN=your_auth_token
CUE_SMS_FROM_NUMBER=+1234567890

# OpenAI (optional for AI content generation)
CUE_OPENAI_API_KEY=sk-...
CUE_AI_ENABLED=true

# Security
CUE_SECRET_KEY=change-this-secret-key-in-production
CUE_CRM_WEBHOOK_SECRET=your-webhook-secret
```

**4. Run database migrations:**

```bash
uv run piccolo migrations forwards all
```

**5. Seed initial triggers:**

```bash
uv run python -m cue_notify.cli.seed_triggers --force-update
```

**6. Start the services:**

```bash
# Terminal 1: API server
uv run uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

# Terminal 2: Background workers
uv run dramatiq cue.workers --processes 4 --threads 8 --watch
```

**7. Test the API:**

```bash
curl http://localhost:8000/api/v1/health
# Should return: {"status": "healthy"}
```

---

### Docker Development

For the complete stack (API + workers + PostgreSQL + Redis):

**1. Start all services:**

```bash
docker compose -f ops/docker-compose.yml up --build
```

This starts:

- `postgres` - PostgreSQL database on port 5432
- `redis` - Redis on port 6379
- `web` - FastAPI API server on port 8000
- `worker` - Dramatiq background workers

**2. Run migrations inside container:**

```bash
docker compose -f ops/docker-compose.yml exec web piccolo migrations forwards all
```

**3. Seed triggers:**

```bash
docker compose -f ops/docker-compose.yml exec web python -m cue_notify.cli.seed_triggers --force-update
```

**4. View logs:**

```bash
docker compose -f ops/docker-compose.yml logs -f web     # API logs
docker compose -f ops/docker-compose.yml logs -f worker  # Worker logs
```

**5. Stop services:**

```bash
docker compose -f ops/docker-compose.yml down
```

---

## Project Structure

```
cue/
├── config/                    # Environment configuration
│   ├── .env.example          # Template for environment variables
│   └── .env                  # Your local config (not committed)
│
├── docs/                      # Documentation
│   ├── api.md                # HTTP API reference
│   ├── architecture.md       # System design & layer responsibilities
│   ├── development.md        # Development guide & conventions
│   ├── PROJECT_STRUCTURE.md  # Directory guide & workflows
│   └── internal/             # Internal design docs
│
├── ops/                       # Operations & deployment
│   ├── docker/
│   │   └── Dockerfile        # Multi-stage Docker build (dev/prod)
│   ├── docker-compose.yml    # Local development stack
│   └── scripts/              # Deployment scripts
│
├── src/
│   ├── main.py               # FastAPI application entrypoint
│   └── cue/
│       ├── __init__.py
│       ├── config.py         # Settings, DB, Redis configuration
│       ├── workers.py        # Dramatiq actor definitions
│       │
│       ├── domain/           # 📦 Domain Models (Pydantic)
│       │   └── entities.py   # Events, Users, Triggers, Notifications
│       │
│       ├── infrastructure/   # 🔧 External Integrations
│       │   ├── persistence/  # Piccolo ORM tables & migrations
│       │   ├── services/     # Firebase, SMS, OpenAI, Redis, etc.
│       │   └── security/     # Webhook signature validation
│       │
│       ├── application/      # 🎯 Business Logic
│       │   ├── services/     # Core services (EventProcessor, TriggerEngine, etc.)
│       │   ├── triggers/     # Trigger definitions & manager
│       │   └── use_cases/    # High-level use case orchestration
│       │
│       ├── interfaces/       # 🌐 External Interfaces
│       │   ├── http/         # FastAPI routers (API v1)
│       │   └── cli/          # CLI commands (seed, maintenance)
│       │
│       └── cli/              # Command-line tools
│
├── tests/                     # Test suites
│   ├── unit/                 # Unit tests
│   ├── integration/          # Integration tests
│   └── e2e/                  # End-to-end tests
│
├── pyproject.toml            # Project metadata & dependencies
├── uv.lock                   # Locked dependency versions (commit this!)
├── pyrightconfig.json        # Type checking configuration
└── piccolo_conf.py           # Piccolo ORM configuration
```

### Layer Architecture

Cue follows a **clean architecture** with strict dependencies:

```
interfaces → application → infrastructure → domain
```

**Each layer only depends on the layer below it.**

#### 🎯 Domain Layer (`src/cue_notify/domain/`)

- **What:** Pure Pydantic models representing business concepts
- **Contains:** `ScoringEvent`, `UserProfile`, `TriggerConfig`, `Notification`, etc.
- **Dependencies:** None (no external libraries except Pydantic)
- **Rule:** Never import from other layers

#### 🔧 Infrastructure Layer (`src/cue_notify/infrastructure/`)

- **What:** External integrations and data persistence
- **Contains:**
  - `persistence/` - Piccolo ORM tables and migrations
  - `services/` - Firebase, SMS, OpenAI, Redis, rate limiting
  - `security/` - Webhook signature validation
- **Dependencies:** Domain layer only
- **Rule:** Don't contain business logic

#### 🧠 Application Layer (`src/cue_notify/application/`)

- **What:** Core business logic and workflows
- **Contains:**
  - `services/` - `EventProcessor`, `TriggerEngine`, `NotificationService`
  - `triggers/` - Declarative trigger definitions
  - `use_cases/` - High-level orchestration
- **Dependencies:** Domain + Infrastructure
- **Rule:** This is where business rules live

#### 🌐 Interfaces Layer (`src/cue_notify/interfaces/`)

- **What:** External-facing adapters
- **Contains:**
  - `http/api/v1/` - FastAPI routers
  - `cli/` - Command-line tools
- **Dependencies:** All layers
- **Rule:** Keep thin - validate input, call services, return response

---

## Architecture & Design

### Event Flow Example: Credit Scoring

1. **CRM webhook** sends `ScoringEvent` → `POST /api/v1/webhooks/crm/scoring`
2. **Webhook validates** HMAC signature using `CUE_CRM_WEBHOOK_SECRET`
3. **EventProcessor** stores event in DB, loads user context
4. **TriggerEngine** evaluates active triggers matching the event
5. **Rate limiter** (Redis) checks if user hasn't exceeded daily/weekly caps
6. **Queue notification** - Dramatiq enqueues `queue_notification` task
7. **Worker picks up task** and calls `NotificationService`
8. **Content generation**:
   - Try AI generation with OpenAI (if enabled & budget available)
   - Fallback to template if AI fails or is disabled
9. **Timing optimization** - Thompson Sampling picks best send time
10. **Scheduling** - APScheduler schedules delivery for optimal time
11. **Delivery** - Send via Firebase (push) or Twilio (SMS)
12. **Tracking** - Client apps call `/track/open`, `/track/click`, `/track/conversion`
13. **Analytics aggregation** - Daily job updates analytics tables

### Data Stores

| Store      | Purpose                                       | Libraries           |
| ---------- | --------------------------------------------- | ------------------- |
| PostgreSQL | Users, triggers, notification logs, analytics | Piccolo ORM         |
| Redis DB 0 | Default cache and session data                | redis-py            |
| Redis DB 1 | Dramatiq job broker and results               | dramatiq[redis]     |
| Redis DB 2 | Rate limiting counters                        | Custom rate limiter |

### Adding a New Trigger

**1. Define trigger configuration** in `src/cue_notify/application/triggers/`:

```python
# src/cue_notify/application/triggers/welcome_approved.py
WELCOME_APPROVED_TRIGGER = {
    "code": "welcome_approved",
    "name": "Welcome - Approved",
    "description": "Welcome message when user is approved",
    "condition_rules": {
        "==": [{"var": "decision"}, "APPROVED"]
    },
    "delay_minutes": 5,
    "priority": 8,
    "ai_prompt_template": "Create a warm welcome message...",
    "fallback_templates": {
        "uz": {"title": "Tabriklaymiz!", "body": "..."},
        "ru": {"title": "Поздравляем!", "body": "..."},
        "en": {"title": "Congratulations!", "body": "..."}
    },
    "daily_cap_per_user": 1,
    "cooldown_hours": 24,
    "enabled": True,
    "test_mode": False,
    "rollout_percentage": 100.0
}
```

**2. Register in `src/cue_notify/application/triggers/__init__.py`:**

```python
from .welcome_approved import WELCOME_APPROVED_TRIGGER

AVAILABLE_TRIGGERS = [
    WELCOME_APPROVED_TRIGGER,
    # ... other triggers
]
```

**3. Seed to database:**

```bash
uv run python -m cue_notify.cli.seed_triggers --force-update
```

### Where to Write Code

| Task                     | Location                             | Example                              |
| ------------------------ | ------------------------------------ | ------------------------------------ |
| **New trigger**          | `application/triggers/`              | Credit scoring, limit changes        |
| **Business logic**       | `application/services/`              | Event processing, trigger evaluation |
| **External integration** | `infrastructure/services/`           | Email provider, payment gateway      |
| **Database table**       | `infrastructure/persistence/tables/` | New entity type                      |
| **API endpoint**         | `interfaces/http/api/v1/`            | New webhook or tracking endpoint     |
| **CLI command**          | `cli/`                               | Maintenance scripts                  |
| **Domain model**         | `domain/entities.py`                 | New event or entity type             |

### What NOT to Write Where

❌ **DON'T** put database queries in HTTP routers
✅ **DO** call a service that handles the query

❌ **DON'T** put business logic in infrastructure services
✅ **DO** keep infrastructure services focused on external integration

❌ **DON'T** import from `interfaces/` in `application/` or lower
✅ **DO** keep dependencies flowing downward only

❌ **DON'T** make HTTP routers fat with logic
✅ **DO** validate input, call a service, return response

---

## Development Workflow

### Common Commands

| Task                        | Command                                                     |
| --------------------------- | ----------------------------------------------------------- |
| **Install dependencies**    | `uv sync`                                                   |
| **Add new dependency**      | `uv add <package>`                                          |
| **Add dev dependency**      | `uv add --dev <package>`                                    |
| **Update dependencies**     | `uv lock` then `uv sync`                                    |
| **Run API server**          | `uv run uvicorn src.main:app --reload`                      |
| **Run workers**             | `uv run dramatiq cue.workers --watch`                   |
| **Run tests**               | `uv run pytest`                                             |
| **Run tests with coverage** | `uv run pytest --cov=src --cov-report=html`                 |
| **Lint code**               | `uv run ruff check`                                         |
| **Format code**             | `uv run ruff format`                                        |
| **Type check**              | `uv run pyright`                                            |
| **Create migration**        | `uv run piccolo migrations new <name> --auto`               |
| **Run migrations**          | `uv run piccolo migrations forwards all`                    |
| **Seed triggers**           | `uv run python -m cue_notify.cli.seed_triggers --force-update` |

### Code Quality Standards

All code must pass these checks before merging:

```bash
# Format code
uv run ruff format src tests

# Check linting
uv run ruff check src tests

# Type checking (0 errors required)
uv run pyright src tests

# Run tests
uv run pytest
```

### Git Workflow

1. Create feature branch from `main`
2. Make changes following layer architecture rules
3. Write tests for new functionality
4. Run quality checks (`ruff`, `pyright`, `pytest`)
5. Commit with clear, descriptive messages
6. Push and create pull request
7. Address review feedback
8. Merge after approval

### Environment Variables

Copy `config/.env.example` to `config/.env` and customize:

**Required for basic operation:**

- `CUE_DB_*` - PostgreSQL connection details
- `CUE_REDIS_*` - Redis connection details
- `CUE_SECRET_KEY` - App secret key

**Required for CRM webhooks:**

- `CUE_CRM_WEBHOOK_SECRET` - HMAC signature validation

**Optional integrations:**

- `CUE_FIREBASE_*` - For push notifications
- `CUE_SMS_*` - For SMS delivery
- `CUE_OPENAI_*` - For AI content generation

---

## Deployment

### Docker Production Build

**1. Build production image:**

```bash
docker build -f ops/docker/Dockerfile --target prod -t cue:latest .
```

**2. Run with environment variables:**

```bash
docker run -d \
  --name cue-api \
  -p 8000:8000 \
  -e CUE_DB_HOST=postgres.example.com \
  -e CUE_DB_PASSWORD=secure_password \
  -e CUE_REDIS_HOST=redis.example.com \
  -e CUE_SECRET_KEY=production_secret_key \
  cue:latest
```

**3. Run database migrations:**

```bash
docker exec cue-api piccolo migrations forwards all
```

**4. Seed triggers:**

```bash
docker exec cue-api python -m cue_notify.cli.seed_triggers --force-update
```

### Production Checklist

- [ ] Set strong `CUE_SECRET_KEY` and `CUE_JWT_SECRET`
- [ ] Configure `CUE_CRM_WEBHOOK_SECRET` for signature verification
- [ ] Use separate Redis databases for different purposes (0, 1, 2)
- [ ] Enable connection pooling for PostgreSQL (`DB_MIN_SIZE`, `DB_MAX_SIZE`)
- [ ] Set up monitoring and alerting (consider Sentry DSN)
- [ ] Configure resource limits for Docker containers
- [ ] Use managed PostgreSQL and Redis for production
- [ ] Set up backup strategy for PostgreSQL
- [ ] Enable SSL/TLS for external connections
- [ ] Review and set rate limits appropriately
- [ ] Configure AI budget limits (`AI_DAILY_BUDGET`)
- [ ] Test webhook signature validation before go-live

### Scaling

**Horizontal scaling:**

- Run multiple web containers behind a load balancer
- Scale worker containers based on queue depth
- Use managed Redis with clustering for high availability
- Use read replicas for PostgreSQL if needed

**Monitoring:**

- Prometheus metrics exposed on `/metrics` (configure `PROMETHEUS_PORT`)
- Track notification delivery rates and latencies
- Monitor AI token usage and costs
- Watch rate limiter performance
- Alert on worker queue depth

---

## Documentation

- **[API Reference](docs/api.md)** - HTTP endpoints, webhooks, signatures
- **[Architecture](docs/architecture.md)** - Layer design, data flow, conventions
- **[Development Guide](docs/development.md)** - Setup, commands, best practices
- **[Project Structure](docs/PROJECT_STRUCTURE.md)** - Directory organization
- **[Internal Docs](docs/internal/)** - Design decisions, refactoring notes

### Need Help?

1. Check the docs above
2. Review existing trigger implementations in `application/triggers/`
3. Look at test files for usage examples
4. Ask team members familiar with the codebase

---

## License

See [LICENSE](LICENSE) file for details.
