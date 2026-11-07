"""Runtime settings, loaded once at start-up from the environment (cutover kit 1, ADR-27). No third-party dependency.

Where a value may come from, strongest first:
  1. the real environment (what `docker compose` or the host passes in);
  2. `NAME_FILE=/path` (for the secrets in SECRET_NAMES): the value is read from that file (Docker/Kubernetes secrets: the secret never appears in `docker inspect` or `ps`);
  3. an env file (`.env`), for development only: never committed (.gitignore), never copied into the image (.dockerignore).
Giving a name both directly and as NAME_FILE is an error, not a silent choice.

What this module guarantees:
  * Secrets are held in `Secret`, whose repr and str are fixed text. A Settings object, a traceback or a log line that includes one shows `***`.
  * A bad configuration raises ConfigError listing every problem at once, by NAME and a fixed reason. It never echoes a value, so a mistyped key
    is not copied into a log.
  * Nothing is read lazily: a missing setting stops the process at start-up, not at the first email.
`.env` format: one `NAME=value` per line, `#` comments and blank lines ignored, optional matching single or double quotes around the value.
There is no variable expansion and no `export`: what you see is the value."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, FrozenSet, List, Mapping, Optional, Tuple

_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_WORKER = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_PRODUCT_ID = re.compile(r"^[A-Za-z0-9_:.=-]{1,128}$")
_STREAM = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_FROM = re.compile(r"^[^@\s<>\r\n]+@[^@\s<>\r\n]+\.[^@\s<>\r\n]+$")
_TRUE, _FALSE = {"1", "true", "yes", "on"}, {"0", "false", "no", "off"}
SECRET_NAMES = ("DATABASE_URL", "POSTMARK_SERVER_TOKEN", "GUMROAD_ACCESS_TOKEN", "LEMON_SQUEEZY_API_KEY", "PAYHIP_API_KEY", "LLM_API_KEY",
                "ADMIN_DATABASE_URL", "SAP_WORKER_PASSWORD", "SAP_REVIEW_PASSWORD")      # the last three are read by infra/docker/provision_roles.py


class ConfigError(Exception):
    """problems is a tuple of 'NAME: fixed reason' strings: never a value."""

    def __init__(self, problems):
        self.problems: Tuple[str, ...] = tuple(problems)
        super().__init__("invalid configuration: " + "; ".join(self.problems))


class Secret:
    """A value that must never be printed. reveal() is the only way to read it, which makes every use greppable."""
    __slots__ = ("_value",)

    def __init__(self, value: str):
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name, value):
        raise AttributeError("a Secret is immutable")

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other) -> bool:
        return isinstance(other, Secret) and self._value == other._value

    def __hash__(self) -> int:
        return hash(("Secret", self._value))

    def __reduce__(self):
        raise TypeError("a Secret cannot be pickled")


# ------------------------------------------------------------------ reading sources
def read_env_file(path) -> Dict[str, str]:
    """Parse a `.env` file. A malformed line is reported by number only."""
    out: Dict[str, str] = {}
    problems: List[str] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        name, sep, value = text.partition("=")
        name = name.strip()
        if not sep or not _NAME.match(name):
            problems.append(f"line {number}: not NAME=value")
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[name] = value
    if problems:
        raise ConfigError(problems)
    return out


def load_environ(environ: Optional[Mapping[str, str]] = None, env_file: Optional[str] = None) -> Dict[str, str]:
    """The merged view: env file < real environment, with NAME_FILE resolved into NAME."""
    merged: Dict[str, str] = {}
    if env_file and Path(env_file).is_file():
        merged.update(read_env_file(env_file))
    merged.update(dict(os.environ if environ is None else environ))
    problems: List[str] = []
    for target in SECRET_NAMES:                               # only the secrets: other *_FILE variables (a tokens file, a config file) are not ours
        name = target + "_FILE"
        if name not in merged:
            continue
        if target in merged:
            problems.append(f"{target}: set both directly and as {name}")
            continue
        try:
            merged[target] = Path(merged[name]).read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeDecodeError):
            problems.append(f"{name}: file cannot be read")
    if problems:
        raise ConfigError(problems)
    return merged


# ------------------------------------------------------------------ the settings
@dataclass(frozen=True)
class Settings:
    database_url: Secret
    postmark_token: Secret
    email_from: str
    postal_address: str
    unsubscribe_base_url: str
    allowed_link_hosts: FrozenSet[str]
    icp_product_ids: FrozenSet[str]
    gumroad_token: Optional[Secret] = None
    lemon_squeezy_key: Optional[Secret] = None
    lemon_squeezy_store_id: Optional[str] = None
    payhip_key: Optional[Secret] = None
    payhip_catalog_file: Optional[str] = None            # Payhip cannot list products (ADR-25): the seller keeps a JSON catalogue
    llm_provider: Optional[str] = None
    llm_key: Optional[Secret] = field(default=None)
    postmark_stream: str = "outbound"
    postmark_lookup: bool = False
    max_discount_pct: Decimal = Decimal("0")
    routing_enabled: bool = False
    worker_id: str = "worker-1"
    poll_interval: float = 1.0
    sweep_seconds: float = 60.0

    @property
    def marketplaces(self) -> Tuple[str, ...]:
        """The marketplaces that have credentials, in a fixed order (the first is the default product adapter when routing is off)."""
        return tuple(m for m, present in (("GUMROAD", self.gumroad_token), ("LEMON_SQUEEZY", self.lemon_squeezy_key), ("PAYHIP", self.payhip_key)) if present)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_provider and self.llm_key)


def load_settings(environ: Optional[Mapping[str, str]] = None, env_file: Optional[str] = None) -> Settings:
    env = load_environ(environ, env_file)
    problems: List[str] = []

    def text(name: str, *, required: bool = False, default: Optional[str] = None, pattern=None, reason: str = "has an invalid format") -> Optional[str]:
        raw = env.get(name)
        if raw is None or raw.strip() == "":
            if required:
                problems.append(f"{name}: is required")
            return default
        raw = raw.strip()
        if pattern is not None and not pattern.match(raw):
            problems.append(f"{name}: {reason}")
            return default
        return raw

    def secret(name: str, *, required: bool = False) -> Optional[Secret]:
        value = text(name, required=required)
        return Secret(value) if value else None

    def number(name: str, default: float, low: float, high: float) -> float:
        raw = text(name, default=None)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError:
            problems.append(f"{name}: must be a number")
            return default
        if not low <= value <= high:
            problems.append(f"{name}: must be between {low:g} and {high:g}")
            return default
        return value

    def flag(name: str) -> bool:
        raw = (text(name, default="") or "").lower()
        if raw == "":
            return False
        if raw in _TRUE | _FALSE:
            return raw in _TRUE
        problems.append(f"{name}: must be true or false")
        return False

    def csv(name: str, item: re.Pattern, *, lower: bool = False, required: bool = False) -> FrozenSet[str]:
        raw = text(name, required=required)
        if not raw:
            return frozenset()
        items = [p.strip().lower() if lower else p.strip() for p in raw.split(",") if p.strip()]
        if not items or any(not item.match(i) for i in items):
            problems.append(f"{name}: must be a comma-separated list of valid entries")
            return frozenset()
        return frozenset(items)

    database_url = secret("DATABASE_URL", required=True)
    if database_url and not database_url.reveal().startswith(("postgresql://", "postgres://")):
        problems.append("DATABASE_URL: must be a postgresql:// URL")
    postmark = secret("POSTMARK_SERVER_TOKEN", required=True)
    email_from = text("EMAIL_FROM", required=True, pattern=_FROM, reason="must be a plain address such as hello@example.com")
    postal = text("EMAIL_POSTAL_ADDRESS", required=True)
    if postal and (len(postal) < 10 or "\n" in postal or "\r" in postal):
        problems.append("EMAIL_POSTAL_ADDRESS: must be one line with a real address")
    unsubscribe = text("EMAIL_UNSUBSCRIBE_URL", required=True)
    if unsubscribe and (not unsubscribe.startswith("https://") or " " in unsubscribe):
        problems.append("EMAIL_UNSUBSCRIBE_URL: must be an https URL")
    hosts = csv("ALLOWED_LINK_HOSTS", _HOST, lower=True, required=True)
    icp = csv("ICP_PRODUCT_IDS", _PRODUCT_ID, required=True)

    gumroad, ls_key, payhip = secret("GUMROAD_ACCESS_TOKEN"), secret("LEMON_SQUEEZY_API_KEY"), secret("PAYHIP_API_KEY")
    ls_store = text("LEMON_SQUEEZY_STORE_ID", pattern=re.compile(r"^[0-9]{1,18}$"), reason="must be the numeric store id")
    if bool(ls_key) != bool(ls_store):
        problems.append("LEMON_SQUEEZY_API_KEY: set together with LEMON_SQUEEZY_STORE_ID, or neither")
    if not (gumroad or ls_key or payhip):
        problems.append("GUMROAD_ACCESS_TOKEN: at least one marketplace needs credentials (Gumroad, Lemon Squeezy or Payhip)")
    payhip_catalog = text("PAYHIP_CATALOG_FILE")
    if payhip and not payhip_catalog:
        problems.append("PAYHIP_CATALOG_FILE: required with PAYHIP_API_KEY (Payhip cannot list products)")
    llm_provider = text("LLM_PROVIDER", pattern=re.compile(r"^[a-z][a-z0-9_-]{0,31}$"), reason="must be a short lowercase name")
    llm_key = secret("LLM_API_KEY")
    if bool(llm_provider) != bool(llm_key):
        problems.append("LLM_API_KEY: set together with LLM_PROVIDER, or neither")

    stream = text("POSTMARK_MESSAGE_STREAM", default="outbound", pattern=_STREAM, reason="must be lowercase letters, digits and hyphens")
    worker_id = text("WORKER_ID", default="worker-1", pattern=_WORKER, reason="must be an opaque id")
    raw_discount = text("MAX_DISCOUNT_PCT", default="0")
    try:
        discount = Decimal(raw_discount)
        if not (Decimal(0) <= discount <= Decimal(100)) or not discount.is_finite():
            raise InvalidOperation
    except InvalidOperation:
        problems.append("MAX_DISCOUNT_PCT: must be a number from 0 to 100")
        discount = Decimal("0")
    poll, sweep = number("POLL_INTERVAL_SECONDS", 1.0, 0.1, 60.0), number("CLOSER_SWEEP_SECONDS", 60.0, 0.0, 86400.0)
    lookup, routing = flag("POSTMARK_LOOKUP"), flag("ROUTING_ENABLED")

    if problems:
        raise ConfigError(problems)
    return Settings(database_url=database_url, postmark_token=postmark, email_from=email_from, postal_address=postal, unsubscribe_base_url=unsubscribe,
                    allowed_link_hosts=hosts, icp_product_ids=icp, gumroad_token=gumroad, lemon_squeezy_key=ls_key, lemon_squeezy_store_id=ls_store,
                    payhip_key=payhip, payhip_catalog_file=payhip_catalog, llm_provider=llm_provider, llm_key=llm_key, postmark_stream=stream, postmark_lookup=lookup,
                    max_discount_pct=discount, routing_enabled=routing, worker_id=worker_id, poll_interval=poll, sweep_seconds=sweep)
