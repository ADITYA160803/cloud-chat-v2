# Cloud-Chat V2

Cloud-Chat V2 is an AI-powered application that helps users create AWS infrastructure using simple English instructions.

Instead of writing Terraform code manually, users can describe what they want to build. The application uses AI to understand the request, prepares Terraform code, checks the configuration, and shows a deployment plan for review.

Users can review the plan before allowing any real AWS deployment.

## Features

- **Simple Instructions:** Describe the AWS resources you want using normal English.
- **AI Support:** Uses Groq as the main AI provider, with Google Gemini as an optional provider.
- **Terraform Integration:** Automatically prepares Terraform code for AWS infrastructure.
- **Security Checks:** Checks infrastructure settings using built-in security rules and Checkov.
- **Deployment Review:** Shows the Terraform plan before deployment.
- **AWS Verification:** Checks deployed AWS resources independently using Boto3.
- **Deployment History:** Stores deployment information using SQLite.

## Technologies Used

| Technology | Purpose |
|---|---|
| Python | Main application language |
| Streamlit | User interface |
| Groq / Google Gemini | AI processing |
| Terraform | AWS infrastructure creation |
| SQLite | Storing deployment information |
| Boto3 | Checking AWS resources |
| Checkov | Infrastructure security checks |

## How It Works

1. Enter your AWS infrastructure requirements in simple English.
2. AI understands your request.
3. The application generates Terraform configuration.
4. Security checks are performed.
5. Review the Terraform deployment plan.
6. Approve the plan before real deployment.
7. Verify the created AWS resources.

## Safety Settings

Cloud-Chat V2 uses safe default settings:

- `ENABLE_MOCK_MODE=True` – Runs in mock mode by default, without performing real AWS deployment operations.
- `ENABLE_REAL_TERRAFORM_APPLY=False` – Real Terraform deployment is disabled by default.

Real AWS deployment requires the appropriate configuration and explicit approval.

## Installation and Setup

### 1. Clone the Repository

```bash
git clone https://github.com/ADITYA160803/cloud-chat-v2.git
cd cloud-chat-v2
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Create Environment File

Copy the example environment file:

**Windows (PowerShell):**
```powershell
Copy-Item .env.example .env
```

**Linux / macOS:**
```bash
cp .env.example .env
```

Add the required configuration and API keys to your `.env` file.

### 4. Run the Application

```bash
streamlit run app.py
```

The application will open in your browser.

## Project Structure

```text
Cloud-Chat-V2/
├── core/          # Main application logic
├── database/      # Database operations
├── policies/      # Security rules
├── services/      # Supporting services
├── tests/         # Application tests
├── ui/            # User interface components
├── app.py         # Application entry point
├── config.py      # Application settings
├── requirements.txt
├── .env.example
└── README.md
```

## Security Notes

- Never upload your `.env` file or API keys to GitHub.
- Keep AWS credentials private.
- Review Terraform plans before approving deployment.
- Real AWS operations should only be enabled when required.

## Project Status

Cloud-Chat V2 is an academic project demonstrating how AI and Terraform can work together to simplify AWS infrastructure creation.

