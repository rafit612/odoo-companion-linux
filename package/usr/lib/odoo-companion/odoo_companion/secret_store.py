import gi

gi.require_version("Secret", "1")
from gi.repository import Secret

from .constants import APP_NAME

SCHEMA = Secret.Schema.new(
    "com.dotbdsolutions.OdooCompanion",
    Secret.SchemaFlags.NONE,
    {"login": Secret.SchemaAttributeType.STRING},
)


def store_secret(login: str, secret: str):
    if not login:
        raise ValueError("Login is required before storing a secret.")
    Secret.password_store_sync(
        SCHEMA,
        {"login": login},
        Secret.COLLECTION_DEFAULT,
        f"{APP_NAME} Odoo password/API key",
        secret,
        None,
    )


def lookup_secret(login: str):
    if not login:
        return None
    return Secret.password_lookup_sync(SCHEMA, {"login": login}, None)


def clear_secret(login: str):
    if not login:
        return
    Secret.password_clear_sync(SCHEMA, {"login": login}, None)
