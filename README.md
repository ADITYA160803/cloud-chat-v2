# Cloud-Chat V2

Cloud-Chat V2 is a streamlined, AI-powered platform for deploying AWS infrastructure from natural-language descriptions.

## Architecture

- **Frontend**: Streamlit
- **Persistence**: SQLite (with WAL mode)
- **AI Engine**: Groq (primary) / Google Gemini (optional)
- **IaC Engine**: Terraform (isolated per-deployment workspaces)
- **Security**: Deterministic policy checks & Checkov
- **Verification**: Boto3 independent read-only AWS inspection

## Safe Defaults

By default:
- `ENABLE_MOCK_MODE=True` (no real AWS operations are executed)
- `ENABLE_REAL_TERRAFORM_APPLY=False` (real Terraform apply requires explicit operator enablement)

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy environment settings
cp .env.example .env

# 3. Launch application
streamlit run app.py
```
