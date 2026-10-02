import os
from pathlib import Path


def db_path() -> Path:
    return Path(os.environ.get("APP_DB", "var/app.sqlite"))

def tenant_header() -> str:
    return os.environ.get("APP_TENANT_HEADER", "X-Tenant")
