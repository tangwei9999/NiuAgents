"""TradingAgents: multi-agent LLM financial trading framework."""

__version__ = "0.7.1"

import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# Load .env at package import so DEFAULT_CONFIG's env-var overlay and every LLM
# client see the user's keys whichever entry point started the process.
# usecwd=True walks from the working directory, so the installed console script
# finds the project's .env rather than looking beside site-packages. Values the
# caller has already exported are never overridden.
load_dotenv(find_dotenv(usecwd=True))

# Also try loading from the package directory (for when running from outside the project)
_pkg_dir = Path(__file__).resolve().parent
_project_root = _pkg_dir.parent
_project_env = _project_root / ".env"
if _project_env.exists():
    load_dotenv(str(_project_env), override=False)

load_dotenv(find_dotenv(".env.enterprise", usecwd=True), override=False)
