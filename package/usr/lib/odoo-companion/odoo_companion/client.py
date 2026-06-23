import requests

from .constants import MODULE_MODELS, UNSAFE_MODELS
from .secret_store import lookup_secret
from .storage import config_store


class OdooError(Exception):
    def __init__(self, message, *, name2="", code=None):
        super().__init__(message)
        self.name2 = name2 or ""
        self.code = code


def normalize_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


class OdooClient:
    def __init__(self, config=None, secret=None):
        self.config = config_store.read() if config is None else dict(config)
        self.config["odoo_url"] = normalize_url(self.config.get("odoo_url", ""))
        self.secret = secret
        self.http = requests.Session()

    @property
    def odoo_url(self):
        return self.config.get("odoo_url", "")

    def _content_type(self):
        version = str(self.config.get("odoo_version") or "")
        major = int(version.split(".")[0] or 0) if version[:1].isdigit() else 0
        return "application/json-rpc" if major >= 19 else "application/json"

    def json_rpc(self, route, params):
        if not self.odoo_url:
            raise OdooError("Odoo URL is not configured.")
        try:
            response = self.http.post(
                f"{self.odoo_url}{route}",
                json={"jsonrpc": "2.0", "method": "call", "params": params},
                headers={"Content-Type": self._content_type()},
                timeout=20,
            )
        except requests.RequestException as exc:
            raise OdooError(f"Could not reach {self.odoo_url}: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise OdooError(
                "Odoo returned a non-JSON response "
                f"(HTTP {response.status_code}). The server may be down, behind a login wall, "
                "or returning an error page."
            ) from exc

        if data.get("error"):
            error = data["error"]
            details = error.get("data") or {}
            raise OdooError(
                details.get("message") or error.get("message") or "Odoo RPC error",
                name2=details.get("name") or "",
                code=error.get("code"),
            )
        return data.get("result")

    def fetch_odoo_version(self):
        version = self.json_rpc("/jsonrpc", {"service": "common", "method": "version", "args": []})
        server_version = version.get("server_version") if isinstance(version, dict) else None

        def update(config):
            config["odoo_version"] = server_version

        config_store.update(update)
        self.config["odoo_version"] = server_version
        return server_version

    def authenticate(self, odoo_url=None, db=None, login=None, secret=None):
        if odoo_url is not None:
            self.config["odoo_url"] = normalize_url(odoo_url)
        if db is not None:
            self.config["db"] = db
        if login is not None:
            self.config["login"] = login
        if secret is not None:
            self.secret = secret

        try:
            self.fetch_odoo_version()
        except OdooError:
            pass

        uid = self.json_rpc(
            "/jsonrpc",
            {
                "service": "common",
                "method": "authenticate",
                "args": [self.config.get("db"), self.config.get("login"), self.secret, {}],
            },
        )
        if not uid:
            raise OdooError("Invalid database, username, or password/API key")

        user_row = self.exec_object_kw_raw(
            uid,
            self.secret,
            "res.users",
            "read",
            [[uid], ["partner_id"]],
        )[0]
        partner_id = user_row.get("partner_id", [None])[0] if user_row.get("partner_id") else None

        def update(config):
            config["uid"] = uid
            config["partner_id"] = partner_id

        config_store.update(update)
        self.config["uid"] = uid
        self.config["partner_id"] = partner_id
        return {"uid": uid, "partner_id": partner_id}

    def ensure_logged_in(self):
        if self.config.get("uid") and self.config.get("partner_id"):
            if self.secret is None:
                self.secret = lookup_secret(self.config.get("login"))
            if not self.secret:
                raise OdooError("No password/API key saved. Open Odoo Companion Settings first.")
            return self.config

        for key, label in (("odoo_url", "Odoo URL"), ("db", "database"), ("login", "username")):
            if not self.config.get(key):
                raise OdooError(f"{label} is not configured. Open Odoo Companion Settings first.")
        if self.secret is None:
            self.secret = lookup_secret(self.config.get("login"))
        if not self.secret:
            raise OdooError("No password/API key saved. Open Odoo Companion Settings first.")
        self.authenticate(secret=self.secret)
        return self.config

    def exec_object_kw_raw(self, uid, secret, model, method, args, kwargs=None):
        return self.json_rpc(
            "/jsonrpc",
            {
                "service": "object",
                "method": "execute_kw",
                "args": [
                    self.config.get("db"),
                    uid,
                    secret,
                    model,
                    method,
                    args,
                    kwargs or {},
                ],
            },
        )

    def call_kw(self, model, method, args, kwargs=None):
        if model in UNSAFE_MODELS:
            raise OdooError(f'Refusing to query "{model}" because it is known to lack per-employee access rules.')
        self.ensure_logged_in()
        try:
            return self.exec_object_kw_raw(self.config["uid"], self.secret, model, method, args, kwargs or {})
        except OdooError as exc:
            if not exc.name2.endswith("AccessDenied"):
                raise
            self.config["uid"] = None
            self.authenticate(secret=self.secret)
            return self.exec_object_kw_raw(self.config["uid"], self.secret, model, method, args, kwargs or {})

    def check_module_access(self):
        self.ensure_logged_in()
        access = {}
        for key, model in MODULE_MODELS.items():
            try:
                allowed = self.call_kw(model, "check_access_rights", ["read"], {"raise_exception": False})
                access[key] = bool(allowed)
            except OdooError:
                access[key] = False

        def update(config):
            config["module_access"] = access

        config_store.update(update)
        return access

    def list_databases(self):
        if not self.odoo_url:
            raise OdooError("Enter the Odoo URL first.")
        try:
            response = self.http.post(
                f"{self.odoo_url}/web/database/list",
                json={"jsonrpc": "2.0", "method": "call", "id": 1, "params": {}},
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise OdooError(f"Database detection failed: {exc}") from exc
        except ValueError as exc:
            raise OdooError("Database detection returned a non-JSON response.") from exc
        return data.get("result") or []
