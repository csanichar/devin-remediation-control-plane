"""
Centralized environment configuration.

All other modules read settings from `settings` here. This avoids
random os.environ[...] lookups scattered across the codebase and
makes it obvious what the service depends on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


# Load .env into the process environment as soon as this module is imported.
# In Docker the env_file directive provides the same variables; load_dotenv()
# is a no-op there because the file is not present in the image.
load_dotenv()


# Variables that the service cannot start without.
REQUIRED_VARS = (
    "DEVIN_API_KEY",
    "DEVIN_ORG_ID",
    "GITHUB_TOKEN",
    "GITHUB_OWNER",
    "GITHUB_REPO",
    "TARGET_REPO_URL",
)


@dataclass(frozen=True)
class Settings:
    devin_api_key: str
    devin_org_id: str
    github_token: str
    github_owner: str
    github_repo: str
    # Optional locally so a developer can run /simulate without configuring a
    # webhook. Production deployments MUST set this so /webhooks/github can
    # verify the X-Hub-Signature-256 header.
    github_webhook_secret: Optional[str]
    target_repo_url: str
    target_base_branch: Optional[str]

    @classmethod
    def from_env(cls) -> "Settings":
        missing = [name for name in REQUIRED_VARS if not os.environ.get(name)]
        if missing:
            raise RuntimeError(
                "Missing required environment variables: "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill them in."
            )

        return cls(
            devin_api_key=os.environ["DEVIN_API_KEY"],
            devin_org_id=os.environ["DEVIN_ORG_ID"],
            github_token=os.environ["GITHUB_TOKEN"],
            github_owner=os.environ["GITHUB_OWNER"],
            github_repo=os.environ["GITHUB_REPO"],
            github_webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET") or None,
            target_repo_url=os.environ["TARGET_REPO_URL"],
            target_base_branch=os.environ.get("TARGET_BASE_BRANCH") or None,
        )


settings = Settings.from_env()
