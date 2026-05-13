from __future__ import annotations

import os
from datetime import datetime, date
from pathlib import Path
from typing import Any

import yaml


class CompetitorAccountManager:
    """Read/write accessor for competitor_registry.yaml.

    Provides vendor lookups, feature module access, account status checks,
    and simple keyword-based requirement-to-feature matching.
    """

    def __init__(self, registry_path: str = "APP/backend/config/competitor_registry.yaml") -> None:
        self._registry_path = Path(registry_path)
        self.registry = self._load_registry(self._registry_path)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_registry(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"Competitor registry not found: {path}")
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def _save_registry(self) -> None:
        with self._registry_path.open("w", encoding="utf-8") as fh:
            yaml.dump(self.registry, fh, allow_unicode=True, default_flow_style=False, sort_keys=False)

    # ------------------------------------------------------------------
    # Vendor accessors
    # ------------------------------------------------------------------

    def get_all_vendors(self) -> list[dict[str, Any]]:
        """Return all vendor entries from the registry."""
        return list(self.registry.get("vendors", []))

    def get_vendor(self, vendor_id: str) -> dict[str, Any]:
        """Return a single vendor dict by id, or empty dict if not found."""
        for vendor in self.registry.get("vendors", []):
            if vendor.get("id") == vendor_id:
                return vendor
        return {}

    # ------------------------------------------------------------------
    # Feature module accessors
    # ------------------------------------------------------------------

    def get_feature_modules(self, vendor_id: str) -> list[dict[str, Any]]:
        """Return the feature_modules list for a vendor."""
        vendor = self.get_vendor(vendor_id)
        return list(vendor.get("feature_modules", []))

    def get_feature_taxonomy(self) -> list[dict[str, Any]]:
        """Return the global feature taxonomy list."""
        return list(self.registry.get("feature_taxonomy", []))

    # ------------------------------------------------------------------
    # Account accessors
    # ------------------------------------------------------------------

    def get_active_account(self, vendor_id: str, tier: str = "trial") -> dict[str, Any] | None:
        """Return the first account entry matching vendor_id + tier that is active.

        Returns None if not found or not active.
        """
        vendor = self.get_vendor(vendor_id)
        for account in vendor.get("accounts", []):
            if account.get("tier") == tier and account.get("status") == "active":
                # Resolve environment variable references for username
                account_copy = dict(account)
                raw_username = account_copy.get("username", "")
                if isinstance(raw_username, str) and raw_username.startswith("${") and raw_username.endswith("}"):
                    env_key = raw_username[2:-1]
                    account_copy["username"] = os.environ.get(env_key, "")
                return account_copy
        return None

    def check_account_status(self, vendor_id: str, tier: str = "trial") -> str:
        """Return account status: 'active', 'expired', or 'missing'.

        Does not perform live network check — reads registry status field only.
        """
        vendor = self.get_vendor(vendor_id)
        if not vendor:
            return "missing"
        for account in vendor.get("accounts", []):
            if account.get("tier") == tier:
                return account.get("status", "missing")
        return "missing"

    def update_account_status(
        self,
        vendor_id: str,
        tier: str,
        status: str,
        last_login: str | None = None,
    ) -> None:
        """Update account status (and optionally last_login) and persist to disk.

        Parameters
        ----------
        vendor_id:
            Registry vendor id (e.g. 'sap', 'kingdee').
        tier:
            Account tier ('public', 'trial', 'demo').
        status:
            New status value ('active', 'expired', 'missing').
        last_login:
            ISO date string (YYYY-MM-DD). Defaults to today if not provided.
        """
        if last_login is None:
            last_login = date.today().isoformat()

        for vendor in self.registry.get("vendors", []):
            if vendor.get("id") != vendor_id:
                continue
            for account in vendor.get("accounts", []):
                if account.get("tier") == tier:
                    account["status"] = status
                    if tier != "public":
                        account["last_login"] = last_login
                    self._save_registry()
                    return

        raise ValueError(f"Account not found: vendor={vendor_id!r}, tier={tier!r}")

    # ------------------------------------------------------------------
    # Requirement → feature matching
    # ------------------------------------------------------------------

    def match_requirement_to_features(self, requirement_text: str) -> list[str]:
        """Simple keyword matching to identify which feature_ids are relevant.

        Returns a list of feature_id strings ordered by match score (highest first).
        """
        text_lower = requirement_text.lower()
        taxonomy = self.get_feature_taxonomy()
        scores: list[tuple[int, str]] = []

        for feature in taxonomy:
            feature_id = feature.get("id", "")
            score = 0
            search_terms = feature.get("search_terms", {})

            for term in search_terms.get("zh", []):
                if term.lower() in text_lower:
                    score += 2

            for term in search_terms.get("en", []):
                if term.lower() in text_lower:
                    score += 1

            # Also match on the feature name itself
            name = feature.get("name", "")
            if name and name.lower() in text_lower:
                score += 3

            if score > 0:
                scores.append((score, feature_id))

        scores.sort(key=lambda x: x[0], reverse=True)
        return [feature_id for _, feature_id in scores]

    # ------------------------------------------------------------------
    # Convenience summary
    # ------------------------------------------------------------------

    def status_summary(self) -> list[dict[str, Any]]:
        """Return a compact status summary for all vendors and their accounts."""
        summary = []
        for vendor in self.get_all_vendors():
            vendor_id = vendor.get("id", "")
            accounts = []
            for account in vendor.get("accounts", []):
                tier = account.get("tier", "")
                if tier == "public":
                    continue
                accounts.append({
                    "tier": tier,
                    "status": account.get("status", "missing"),
                    "last_login": account.get("last_login"),
                })
            summary.append({
                "vendor_id": vendor_id,
                "name": vendor.get("name", ""),
                "feature_count": len(vendor.get("feature_modules", [])),
                "accounts": accounts,
            })
        return summary
