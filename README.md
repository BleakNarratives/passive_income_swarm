# Passive Income Swarm

**FastAPI gateway for automated income generation — Mrs. Higgins, Thoth, and swarm agents.**

A microservices architecture that routes tasks to specialized agents for automated income generation: content creation, market analysis, lead generation, and service delivery.

---

## Architecture

```
api.py              FastAPI gateway — routes tasks to agents
arena/              Agent marketplace and task dispatch
analysis/           Market analysis and opportunity detection
control/            Swarm control plane
curation/           Content curation pipeline
dna_mutation/       Agent evolution and optimization
```

## Quick Start

```bash
pip install fastapi uvicorn
python api.py
# API runs on http://localhost:8000
```

## Stack

- **FastAPI** — async gateway
- **Pydantic** — request/response models
- **Swarm agents** — Mrs. Higgins (content), Thoth (analysis)

## API Key Management

Store keys in `.env` (DO NOT COMMIT).

---

*BleakNarratives // 2026*
