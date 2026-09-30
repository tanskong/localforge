from __future__ import annotations

TARGET = "LocalForge/control-plane/localforge-web"

class CredentialStore:
    def __init__(self, api=None):
        self.api = api

    def _api(self):
        if self.api is None:
            import win32cred
            self.api = win32cred
        return self.api

    def status(self) -> dict:
        try:
            self._api().CredRead(TARGET, 1, 0)
            return {"status": "available", "target": TARGET, "secret": "redacted"}
        except Exception:
            return {"status": "missing", "target": TARGET}

    def get(self) -> str:
        item = self._api().CredRead(TARGET, 1, 0)
        value = item.get("CredentialBlob")
        if isinstance(value, bytes):
            return value.decode("utf-8")
        if isinstance(value, str) and value:
            return value
        raise RuntimeError("credential is empty")

    def set(self, value: str) -> dict:
        if not isinstance(value, str) or not value:
            raise ValueError("credential must be non-empty")
        self._api().CredWrite({"Type": 1, "TargetName": TARGET, "CredentialBlob": value,
                               "Persist": 2, "UserName": "LocalForge"}, 0)
        return {"status": "stored", "target": TARGET}

    def delete(self) -> dict:
        try:
            self._api().CredDelete(TARGET, 1, 0)
        except Exception:
            pass
        return {"status": "deleted", "target": TARGET}
