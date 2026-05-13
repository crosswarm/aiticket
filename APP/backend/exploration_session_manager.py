"""Exploration Session Manager — Phase 4 of exploration quality evolution.

Manages browser session lifecycle for competitive product exploration:
- Cookie freshness checking
- Chrome cookie extraction and decryption
- Playwright storage state generation
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent
REGISTRY_PATH = BASE_DIR / "config" / "competitor_registry.yaml"
PROFILES_DIR = PROJECT_ROOT / "data_cache" / "competitor_validation" / "profiles"

# Chrome Cookies DB path on macOS
CHROME_COOKIES_DB = Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "Default" / "Cookies"
# Keychain service name for Chrome Safe Storage
CHROME_KEYCHAIN_SERVICE = "Chrome Safe Storage"


class ExplorationSessionManager:
    """Manage browser sessions for competitive product exploration."""

    def __init__(self, registry_path: str | Path | None = None):
        self.registry_path = Path(registry_path or REGISTRY_PATH)
        self.registry = self._load_registry()
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Registry loading
    # ------------------------------------------------------------------

    def _load_registry(self) -> dict:
        """Load competitor_registry.yaml."""
        if not self.registry_path.exists():
            return {"vendors": []}
        with self.registry_path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {"vendors": []}

    def _get_vendor(self, vendor_id: str) -> dict:
        """Get vendor entry from registry."""
        for vendor in self.registry.get("vendors", []):
            if vendor.get("id") == vendor_id:
                return vendor
        return {}

    # ------------------------------------------------------------------
    # Cookie freshness
    # ------------------------------------------------------------------

    def check_cookie_freshness(self, vendor_id: str) -> dict:
        """Check if stored cookies/storage state are still valid.

        Returns: {valid, reason, profile_path, age_hours}
        """
        vendor = self._get_vendor(vendor_id)
        if not vendor:
            return {
                "valid": False,
                "reason": f"Vendor '{vendor_id}' not found in registry",
                "profile_path": "",
                "age_hours": -1,
            }

        # Find profile path from accounts
        profile_path = self._get_profile_path(vendor)
        if not profile_path:
            return {
                "valid": False,
                "reason": "No profile_path configured in registry accounts",
                "profile_path": "",
                "age_hours": -1,
            }

        full_path = PROJECT_ROOT / profile_path
        if not full_path.exists():
            return {
                "valid": False,
                "reason": f"Profile file does not exist: {profile_path}",
                "profile_path": str(profile_path),
                "age_hours": -1,
            }

        # Check file age
        try:
            mtime = datetime.fromtimestamp(full_path.stat().st_mtime)
            age = datetime.now() - mtime
            age_hours = round(age.total_seconds() / 3600, 1)

            if age_hours < 24:
                return {
                    "valid": True,
                    "reason": f"Profile is {age_hours}h old (< 24h threshold)",
                    "profile_path": str(profile_path),
                    "age_hours": age_hours,
                }
            else:
                return {
                    "valid": False,
                    "reason": f"Profile is {age_hours}h old (> 24h threshold, needs refresh)",
                    "profile_path": str(profile_path),
                    "age_hours": age_hours,
                }
        except OSError as e:
            return {
                "valid": False,
                "reason": f"Cannot stat profile file: {e}",
                "profile_path": str(profile_path),
                "age_hours": -1,
            }

    # ------------------------------------------------------------------
    # Vendor domains
    # ------------------------------------------------------------------

    def get_vendor_domains(self, vendor_id: str) -> list[str]:
        """Get all domains for a vendor from competitor_registry.yaml."""
        vendor = self._get_vendor(vendor_id)
        if not vendor:
            return []
        domains_dict = vendor.get("domains", {})
        # Collect unique domain values
        domains = []
        for _key, domain in domains_dict.items():
            if domain and domain not in domains:
                domains.append(domain)
        return domains

    # ------------------------------------------------------------------
    # Chrome cookie extraction
    # ------------------------------------------------------------------

    def refresh_cookies_from_chrome(self, vendor_id: str) -> dict:
        """Extract fresh cookies from Chrome for vendor domains.

        Returns: {success, cookie_count, state_path, error}

        NOTE: May fail on Chrome 130+ encryption changes. Returns error gracefully.
        """
        vendor = self._get_vendor(vendor_id)
        if not vendor:
            return {
                "success": False,
                "cookie_count": 0,
                "state_path": "",
                "error": f"Vendor '{vendor_id}' not found in registry",
            }

        domains = self.get_vendor_domains(vendor_id)
        if not domains:
            return {
                "success": False,
                "cookie_count": 0,
                "state_path": "",
                "error": f"No domains configured for vendor '{vendor_id}'",
            }

        # 1. Get Chrome decryption key from macOS Keychain
        try:
            chrome_key = self._get_chrome_key()
        except Exception as e:
            return {
                "success": False,
                "cookie_count": 0,
                "state_path": "",
                "error": f"Failed to get Chrome decryption key: {e}",
            }

        # 2. Copy Chrome Cookies DB to avoid lock
        if not CHROME_COOKIES_DB.exists():
            return {
                "success": False,
                "cookie_count": 0,
                "state_path": "",
                "error": f"Chrome Cookies DB not found at {CHROME_COOKIES_DB}",
            }

        cookies = []
        try:
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                tmp_path = tmp.name
            shutil.copy2(str(CHROME_COOKIES_DB), tmp_path)

            # 3. Query cookies for vendor domains
            conn = sqlite3.connect(tmp_path)
            try:
                for domain in domains:
                    # Match domain and subdomains
                    cursor = conn.execute(
                        "SELECT host_key, name, encrypted_value, path, "
                        "is_secure, is_httponly, expires_utc "
                        "FROM cookies WHERE host_key LIKE ?",
                        (f"%{domain}%",),
                    )
                    for row in cursor:
                        host_key, name, encrypted_value, path, is_secure, is_httponly, expires_utc = row
                        # 4. Decrypt cookie value
                        try:
                            value = self._decrypt_cookie(encrypted_value, chrome_key)
                        except Exception:
                            value = ""  # Skip undecryptable cookies

                        if value:
                            cookies.append({
                                "name": name,
                                "value": value,
                                "domain": host_key,
                                "path": path or "/",
                                "secure": bool(is_secure),
                                "httpOnly": bool(is_httponly),
                                "expires": self._chrome_epoch_to_unix(expires_utc),
                            })
            finally:
                conn.close()
        except Exception as e:
            return {
                "success": False,
                "cookie_count": 0,
                "state_path": "",
                "error": f"Failed to read Chrome cookies: {e}",
            }
        finally:
            # Clean up temp file
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        if not cookies:
            return {
                "success": False,
                "cookie_count": 0,
                "state_path": "",
                "error": f"No cookies found for domains: {domains}",
            }

        # 5. Write to Playwright storage state
        state_path = self.update_storage_state(vendor_id, cookies)

        return {
            "success": True,
            "cookie_count": len(cookies),
            "state_path": state_path,
            "error": "",
        }

    def update_storage_state(self, vendor_id: str, cookies: list[dict]) -> str:
        """Write cookies to Playwright storage state JSON.

        Returns path to state file.
        """
        profile_path = PROFILES_DIR / f"{vendor_id}_state.json"
        profile_path.parent.mkdir(parents=True, exist_ok=True)

        # Playwright storage state format
        state = {
            "cookies": [
                {
                    "name": c.get("name", ""),
                    "value": c.get("value", ""),
                    "domain": c.get("domain", ""),
                    "path": c.get("path", "/"),
                    "expires": c.get("expires", -1),
                    "httpOnly": c.get("httpOnly", False),
                    "secure": c.get("secure", False),
                    "sameSite": c.get("sameSite", "Lax"),
                }
                for c in cookies
            ],
            "origins": [],
            "_metadata": {
                "vendor_id": vendor_id,
                "created_at": datetime.now().isoformat(),
                "cookie_count": len(cookies),
                "source": "chrome_extraction",
            },
        }

        with profile_path.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)

        return str(profile_path)

    # ------------------------------------------------------------------
    # Chrome decryption helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_chrome_key() -> bytes:
        """Get Chrome Safe Storage key from macOS Keychain."""
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", CHROME_KEYCHAIN_SERVICE,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Keychain access failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        password = result.stdout.strip()

        # Derive key using PBKDF2 (Chrome uses 1003 iterations on macOS)
        import hashlib
        key = hashlib.pbkdf2_hmac(
            "sha1",
            password.encode("utf-8"),
            b"saltysalt",
            1003,
            dklen=16,
        )
        return key

    @staticmethod
    def _decrypt_cookie(encrypted_value: bytes, key: bytes) -> str:
        """Decrypt a Chrome cookie value.

        Chrome on macOS uses AES-128-CBC with:
        - Key: PBKDF2(keychain_password, 'saltysalt', 1003, 16)
        - IV: 16 bytes of 0x20 (space)
        - Prefix: b'v10' (3 bytes stripped before decrypt)

        NOTE: Chrome 130+ may use different encryption. This handles v10 format.
        """
        if not encrypted_value:
            return ""

        # Unencrypted cookie
        if not encrypted_value.startswith(b"v10") and not encrypted_value.startswith(b"v11"):
            try:
                return encrypted_value.decode("utf-8")
            except UnicodeDecodeError:
                return ""

        # v11 (Chrome 130+) uses a different method we cannot easily decrypt
        if encrypted_value.startswith(b"v11"):
            raise ValueError("v11 encryption not supported (Chrome 130+)")

        # v10 decryption
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.primitives import padding as sym_padding

            ciphertext = encrypted_value[3:]  # strip 'v10' prefix
            iv = b" " * 16  # 16 bytes of 0x20

            cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(ciphertext) + decryptor.finalize()

            # Remove PKCS7 padding
            unpadder = sym_padding.PKCS7(128).unpadder()
            unpadded = unpadder.update(decrypted) + unpadder.finalize()

            return unpadded.decode("utf-8")
        except ImportError:
            # Fallback: try without cryptography library
            raise ImportError(
                "cryptography library required for cookie decryption. "
                "Install with: pip install cryptography"
            )
        except Exception as e:
            raise ValueError(f"Decryption failed: {e}")

    @staticmethod
    def _chrome_epoch_to_unix(chrome_timestamp: int) -> float:
        """Convert Chrome epoch (microseconds since 1601-01-01) to Unix timestamp."""
        if chrome_timestamp <= 0:
            return -1
        # Chrome epoch offset: seconds between 1601-01-01 and 1970-01-01
        chrome_epoch_offset = 11644473600
        return (chrome_timestamp / 1_000_000) - chrome_epoch_offset

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_profile_path(vendor: dict) -> str:
        """Extract first profile_path from vendor accounts."""
        for account in vendor.get("accounts", []):
            pp = account.get("profile_path", "")
            if pp:
                return pp
        return ""
