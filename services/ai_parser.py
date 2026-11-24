import json
import re
from typing import Any, Dict, Optional
from config import config
from core.exceptions import AIParserError, ConfigurationError, ValidationError
from core.models import (
    AWSService,
    DeploymentMode,
    EC2ResourceSpec,
    InterpretedInfrastructure,
    NetworkSpec,
    StorageSpec,
    VolumeType,
)

REGION_MAPPINGS = {
    "mumbai": "ap-south-1",
    "n. virginia": "us-east-1",
    "virginia": "us-east-1",
    "ohio": "us-east-2",
    "oregon": "us-west-2",
    "ireland": "eu-west-1",
    "frankfurt": "eu-central-1",
    "singapore": "ap-southeast-1",
    "tokyo": "ap-northeast-1",
    "sydney": "ap-southeast-2",
    "london": "eu-west-2",
}

SYSTEM_PROMPT = """You are an expert AWS cloud infrastructure interpreter.
Convert the user's natural-language infrastructure request into a structured JSON representation matching the schema below.

JSON SCHEMA:
{
  "service": "ec2",
  "region": "ap-south-1",
  "resources": [
    {
      "name": "app-server",
      "instance_type": "t3.micro",
      "ami_id": null,
      "key_name": null,
      "storage": {
        "size_gb": 8,
        "volume_type": "gp3",
        "encrypted": true,
        "iops": null,
        "throughput": null
      },
      "network": {
        "deployment_mode": "PRIVATE",
        "public_ip": false,
        "vpc_id": null,
        "subnet_id": null,
        "security_group_ids": [],
        "allowed_ssh_cidrs": [],
        "use_existing_vpc": false,
        "use_existing_subnet": false
      },
      "tags": {}
    }
  ],
  "assumptions": [
    "Enabled EBS gp3 volume encryption by default"
  ],
  "missing_information": [],
  "requires_clarification": false,
  "clarification_question": null
}

RULES:
1. ONLY return a single valid JSON object. Do not include markdown preamble, postamble or explanations.
2. Supported services right now: 'ec2'.
3. Region conversion: Common names must map to valid AWS region codes (e.g. 'Mumbai' -> 'ap-south-1', 'Virginia' -> 'us-east-1'). If unspecified, use the default provided in user request or 'us-east-1'.
4. Safe defaults:
   - If storage volume type is omitted, default to 'gp3' with encrypted=true.
   - If public/private IP is omitted, default to public_ip=false (safe private default).
   - If user explicitly requested 'private', strictly ensure public_ip=false.
5. EXISTING VPC / SUBNET RULES (CRITICAL):
   - If the user explicitly mentions 'existing VPC', 'my VPC', or similar:
     * Set use_existing_vpc=true.
     * If an explicit VPC ID (e.g. 'vpc-0123456789abcdef0') is provided in the prompt, set vpc_id to that ID.
     * If NO VPC ID was provided, set vpc_id to null, DO NOT invent a synthetic ID, DO NOT assume default VPC, add 'Existing VPC ID or selection required' to 'missing_information', and set requires_clarification=true.
   - If the user explicitly mentions 'existing subnet', 'my subnet', or similar:
     * Set use_existing_subnet=true.
     * If an explicit subnet ID is provided, set subnet_id to that ID.
     * If NO subnet ID was provided, set subnet_id to null, DO NOT invent a synthetic ID, add 'Existing Subnet ID or selection required' to 'missing_information', and set requires_clarification=true.
   - NEVER silently select the default VPC when the user requested an existing VPC! Remove any assumption claiming default VPC is used in that case.
   - When clarification is required for missing VPC/subnet, set clarification_question asking the user to provide or select the existing VPC and Subnet IDs.
6. TAGS:
   - Do NOT add arbitrary tags like {"Environment": "Production"} unless explicitly requested by the user. Leave tags as {} if no tags were asked for.
7. Clearly list every legitimate assumption in 'assumptions'.
"""


def extract_json_from_response(raw_text: str) -> Dict[str, Any]:
    """Robustly extracts JSON from an LLM response string."""
    clean = raw_text.strip()
    # Strip markdown code blocks if present
    if "```" in clean:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", clean)
        if match:
            clean = match.group(1).strip()

    try:
        return json.loads(clean)
    except json.JSONDecodeError as e:
        # Fallback: find first '{' and last '}'
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(clean[start : end + 1])
            except Exception:
                pass
        raise AIParserError(f"AI response did not contain valid JSON: {e}\nRaw output: {raw_text[:200]}")


def test_groq_connectivity(
    api_key: Optional[str] = None,
    target_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Tests connectivity to Groq API and validates availability of the target model.

    Returns structured status without exposing API keys or sensitive request headers:
    - api_key_configured: bool
    - connection_successful: bool
    - model_available: bool
    - configured_model: str
    - available_models: List[str]
    - last_successful_test: Optional[str]
    - error_message: Optional[str]
    """
    key = api_key if api_key is not None else config.groq_api_key
    model_name = target_model or config.groq_model

    result: Dict[str, Any] = {
        "api_key_configured": bool(key),
        "connection_successful": False,
        "model_available": False,
        "configured_model": model_name,
        "available_models": [],
        "last_successful_test": None,
        "error_message": None,
    }

    if not key:
        result["error_message"] = "Groq API key is not configured. Set GROQ_API_KEY in .env."
        return result

    try:
        from datetime import datetime, timezone
        from groq import Groq

        client = Groq(api_key=key, timeout=10.0)
        models_response = client.models.list()
        avail_models = [m.id for m in models_response.data] if models_response and models_response.data else []
        result["available_models"] = avail_models
        result["connection_successful"] = True
        result["model_available"] = model_name in avail_models

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if result["model_available"]:
            result["last_successful_test"] = now_str
        else:
            result["error_message"] = (
                f"Model '{model_name}' was not found in available Groq models "
                f"({len(avail_models)} models retrieved). Please choose a supported model."
            )
        return result

    except Exception as e:
        err_str = str(e)
        if key in err_str:
            err_str = err_str.replace(key, "***REDACTED***")
        result["connection_successful"] = False
        result["model_available"] = False
        if "authentication" in err_str.lower() or "401" in err_str or "invalid_api_key" in err_str.lower():
            result["error_message"] = "Authentication failed: GROQ_API_KEY is invalid or expired."
        elif "connection" in err_str.lower() or "connect" in err_str.lower():
            result["error_message"] = "Network connection to Groq API failed. Check your internet connection."
        else:
            result["error_message"] = f"Groq API connection test failed: {err_str}"
        return result


class AIInfrastructureParser:
    """Interprets natural-language AWS requests into validated Pydantic models."""

    def __init__(self, provider: Optional[str] = None):
        self.provider = (provider or config.default_ai_provider).lower()

    def parse(self, user_request: str) -> InterpretedInfrastructure:
        """Parses natural-language user prompt into an InterpretedInfrastructure model."""
        clean_prompt = user_request.strip()
        if not clean_prompt:
            raise ValidationError("Empty infrastructure description provided.")

        # Dispatch to configured provider
        if self.provider == "groq":
            raw_response = self._call_groq(clean_prompt)
        elif self.provider in ("gemini", "google"):
            raw_response = self._call_gemini(clean_prompt)
        else:
            raise ConfigurationError(f"Unsupported AI provider '{self.provider}'. Use 'groq' or 'gemini'.")

        # Extract JSON
        parsed_dict = extract_json_from_response(raw_response)
        parsed_dict["user_request"] = clean_prompt

        # Validate with Pydantic
        try:
            interpreted = InterpretedInfrastructure.model_validate(parsed_dict)
        except Exception as e:
            raise ValidationError(f"Failed to validate interpreted infrastructure schema: {e}") from e

        # Normalization and sanity checks
        self._normalize_region(interpreted, clean_prompt)
        self._enforce_network_and_tag_rules(interpreted, clean_prompt)
        return interpreted

    def _normalize_region(self, model: InterpretedInfrastructure, prompt: str = "") -> None:
        """Normalizes region names (e.g. 'mumbai' -> 'ap-south-1') and prevents silent overrides."""
        prompt_lower = (prompt or model.user_request or "").lower()
        lowered = model.region.lower().strip()

        # 1. First priority: Check user prompt explicitly for known city/region aliases
        for alias, code in REGION_MAPPINGS.items():
            if re.search(r'\b' + re.escape(alias) + r'\b', prompt_lower):
                model.region = code
                model.assumptions = [a for a in model.assumptions if "region defaulted" not in a.lower()]
                return

        # 2. Check if model.region itself is a known alias
        if lowered in REGION_MAPPINGS:
            model.region = REGION_MAPPINGS[lowered]
            model.assumptions = [a for a in model.assumptions if "region defaulted" not in a.lower()]
            return

        # 3. Check for standard AWS region code in prompt (e.g. ap-south-1, us-west-2)
        aws_region_match = re.search(r'\b[a-z]{2}-[a-z]+-\d\b', prompt_lower)
        if aws_region_match:
            model.region = aws_region_match.group(0)
            model.assumptions = [a for a in model.assumptions if "region defaulted" not in a.lower()]
            return

        # 4. If neither prompt nor model provides a valid region:
        if not lowered or lowered in ("null", "none"):
            from services.region_service import get_selected_region
            model.region = get_selected_region()
            model.requires_clarification = True
            if "Target AWS region selection required" not in model.missing_information:
                model.missing_information.append("Target AWS region selection required")
            if not model.clarification_question:
                model.clarification_question = (
                    f"No target AWS region was specified. "
                    f"Defaulting to '{model.region}' (specify another if desired, e.g. 'ap-south-1', 'us-east-1')."
                )

    def _enforce_network_and_tag_rules(self, model: InterpretedInfrastructure, prompt: str) -> None:
        """Enforces deterministic business rules for existing VPC/subnet handling, private networking, and tags."""
        prompt_lower = prompt.lower()
        needs_clarification_reasons = []

        wants_existing_vpc = (
            "existing vpc" in prompt_lower
            or "my vpc" in prompt_lower
            or "own vpc" in prompt_lower
            or any(r.network.use_existing_vpc for r in model.resources)
        )

        wants_existing_subnet = (
            "existing subnet" in prompt_lower
            or "my subnet" in prompt_lower
            or "own subnet" in prompt_lower
            or any(r.network.use_existing_subnet for r in model.resources)
        )

        # 1. Deployment mode inference and clarification
        has_explicit_public = bool(re.search(r'\b(public|internet-facing|external)\b', prompt_lower))
        has_explicit_private = bool(re.search(r'\b(private|internal|isolated)\b', prompt_lower))

        if has_explicit_public and not has_explicit_private:
            for r in model.resources:
                r.network.deployment_mode = DeploymentMode.PUBLIC
                if "with public ip" in prompt_lower or "assign public ip" in prompt_lower or "public ip" in prompt_lower:
                    r.network.public_ip = True
        elif has_explicit_private and not has_explicit_public:
            for r in model.resources:
                r.network.deployment_mode = DeploymentMode.PRIVATE
                r.network.public_ip = False
        elif has_explicit_public and has_explicit_private:
            model.requires_clarification = True
            if "Networking deployment mode selection required (PUBLIC or PRIVATE)" not in model.missing_information:
                model.missing_information.append("Networking deployment mode selection required (PUBLIC or PRIVATE)")
            if not model.clarification_question:
                model.clarification_question = (
                    "Your request mentioned both public and private networking. "
                    "Please clarify: should this EC2 instance be deployed in a PUBLIC subnet (internet-facing) or a PRIVATE subnet (internal/isolated)?"
                )

        for r in model.resources:
            if wants_existing_vpc:
                r.network.use_existing_vpc = True
                if not r.network.vpc_id:
                    if "Existing VPC ID or selection required" not in model.missing_information:
                        model.missing_information.append("Existing VPC ID or selection required")
                    needs_clarification_reasons.append("VPC")

            if wants_existing_subnet:
                r.network.use_existing_subnet = True
                if not r.network.subnet_id:
                    if "Existing Subnet ID or selection required" not in model.missing_information:
                        model.missing_information.append("Existing Subnet ID or selection required")
                    needs_clarification_reasons.append("subnet")

        # Strip invalid assumptions (such as using default VPC when existing was requested)
        if wants_existing_vpc:
            model.assumptions = [
                a for a in model.assumptions
                if "default vpc" not in a.lower()
            ]

        if wants_existing_subnet:
            model.assumptions = [
                a for a in model.assumptions
                if "auto-selected subnet" not in a.lower() and "default subnet" not in a.lower()
            ]

        # Clean arbitrary tags unless explicitly requested
        if "environment" not in prompt_lower and "tag" not in prompt_lower:
            for r in model.resources:
                r.tags = {}

        # Set clarification requirements if any required existing ID is missing
        if wants_existing_vpc or wants_existing_subnet:
            missing_vpc = any(not r.network.vpc_id for r in model.resources if r.network.use_existing_vpc)
            missing_subnet = any(not r.network.subnet_id for r in model.resources if r.network.use_existing_subnet)
            if missing_vpc or missing_subnet:
                model.requires_clarification = True
                items_needed = " and ".join(sorted(set(needs_clarification_reasons)))
                model.clarification_question = (
                    f"You requested using an existing {items_needed}. "
                    "Please provide the existing resource ID(s) (e.g. vpc-xxxx, subnet-xxxx) or select them."
                )


    def _call_groq(self, prompt: str) -> str:
        """Calls Groq API using groq client with configured model."""
        api_key = config.groq_api_key
        if not api_key:
            raise ConfigurationError(
                "Groq API key is not configured. Set the GROQ_API_KEY environment variable in .env."
            )

        model_name = config.groq_model
        try:
            from groq import Groq

            client = Groq(api_key=api_key, timeout=30.0)
            from services.region_service import get_selected_region
            default_reg = get_selected_region()
            completion = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"Default region is '{default_reg}'. Interpret this infrastructure request:\n\n{prompt}",
                    },
                ],
                temperature=0.1,
                max_tokens=1500,
                response_format={"type": "json_object"},
            )
            return completion.choices[0].message.content or "{}"
        except Exception as e:
            err_msg = str(e)
            if api_key in err_msg:
                err_msg = err_msg.replace(api_key, "***REDACTED***")
            if "model_not_found" in err_msg.lower() or "404" in err_msg:
                raise ConfigurationError(
                    f"Groq model '{model_name}' is not found or has been deprecated (HTTP 404). "
                    "Please check available models in Settings or update GROQ_MODEL in .env."
                ) from e
            if "authentication" in err_msg.lower() or "api_key" in err_msg.lower() or "401" in err_msg:
                raise ConfigurationError("Groq authentication failed: Check that GROQ_API_KEY is valid.") from e
            if "rate_limit" in err_msg.lower() or "429" in err_msg:
                raise AIParserError("Groq rate limit exceeded. Please wait a moment and try again.") from e
            raise AIParserError(f"Groq API request failed: {err_msg}") from e

    def _call_gemini(self, prompt: str) -> str:
        """Calls Google Gemini API using google-genai client."""
        api_key = config.gemini_api_key
        if not api_key:
            raise ConfigurationError(
                "Gemini API key is not configured. Set the GEMINI_API_KEY environment variable in .env."
            )

        try:
            from google import genai

            client = genai.Client(api_key=api_key)
            from services.region_service import get_selected_region
            default_reg = get_selected_region()
            full_prompt = (
                f"{SYSTEM_PROMPT}\n\n"
                f"Default region is '{default_reg}'.\n\n"
                f"User Request:\n{prompt}"
            )
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=full_prompt,
            )
            return response.text or "{}"
        except Exception as e:
            err_msg = str(e)
            if "authentication" in err_msg.lower() or "api_key" in err_msg.lower():
                raise ConfigurationError(f"Gemini authentication failed: Check that GEMINI_API_KEY is valid.") from e
            if "quota" in err_msg.lower() or "429" in err_msg:
                raise AIParserError("Gemini quota exceeded. Please wait a moment and retry.") from e
            raise AIParserError(f"Gemini API request failed: {e}") from e
