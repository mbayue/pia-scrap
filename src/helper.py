import base64
import html as html_module
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from http.cookiejar import CookieJar, MozillaCookieJar
from typing import Any, TypedDict
from urllib.parse import parse_qs, urljoin, urlparse

from src.const import BASE_URL, CONFIG_PATH, IMG_BASE_HTTPS
from src.logutil import get_logger

logger = get_logger(__name__)

# ----------------------------
# Helpers
# ----------------------------


def extract_genre_names(novel: Mapping[str, Any]) -> list[str]:
    """Extract unique genre/tag names from a NovelResponse."""
    result = novel.get("result", {})
    nv = result.get("novel", {}) if isinstance(result, Mapping) else {}
    tag_items = (
        (result.get("tag_list") if isinstance(result, Mapping) else None)
        or (nv.get("tag_list") if isinstance(nv, Mapping) else None)
        or []
    )
    names: list[str] = []
    for tag in tag_items:
        if isinstance(tag, str):
            names.append(tag)
        elif isinstance(tag, dict):
            name = tag.get("tag_name") or tag.get("name") or tag.get("title")
            if isinstance(name, str):
                names.append(name)
    return list(dict.fromkeys(names))


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", (name or "").strip()) or "book"


def normalize_url(u: str) -> str:
    if not u:
        return u
    if u.startswith("//"):
        return IMG_BASE_HTTPS + u
    if u.startswith("/"):
        return urljoin(BASE_URL, u)
    return u


def media_type_from_ext(ext: str) -> str:
    ext = ext.lower()
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".gif":
        return "image/gif"
    if ext == ".webp":
        return "image/webp"
    return "image/jpeg"


def looks_like_jwt(token: str | None) -> bool:
    if not isinstance(token, str):
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    for p in parts:
        try:
            base64.urlsafe_b64decode(p + "===")
        except Exception:
            return False
    return True


def kebab(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "book"


def normalize_description(raw: str) -> str:
    """Convert Novelpia's synopsis text into plain text with real newlines.

    ``novel_story`` uses literal ``<br>``/``<br/>`` markers as paragraph breaks
    but is otherwise plain text, not real HTML. Rendering it verbatim (or via
    ``html.escape``) leaves the literal ``<br>`` characters visible instead of a
    line break. Any other stray tags are stripped defensively since this is
    user-generated content, not markup we should trust or execute.
    """
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html_module.unescape(text).strip()


# ----------------------------
# Config/auth management
# ----------------------------


class AuthConfig(TypedDict, total=False):
    login_at: str
    userkey: str
    tkey: str


@dataclass(frozen=True, slots=True)
class CookieAuth:
    login_at: str | None
    userkey: str | None
    tkey: str | None


def _clean_config_value(raw: object) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def normalize_auth_config(raw: Mapping[str, object]) -> AuthConfig:
    return {
        "login_at": _clean_config_value(raw.get("login_at")),
        "userkey": _clean_config_value(raw.get("userkey")),
        "tkey": _clean_config_value(raw.get("tkey")),
    }


def load_config() -> AuthConfig:
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as f:
                raw = json.load(f) or {}
                if isinstance(raw, dict):
                    return normalize_auth_config({str(k): v for k, v in raw.items()})
                logger.error("Error occurred while loading config: config root is not an object")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.error(f"Error occurred while loading config: {e}")
        return {}
    return {}


def save_config(cfg: Mapping[str, str | None]) -> None:
    tmp_path = ""
    try:
        config_dir = os.path.dirname(CONFIG_PATH) or "."
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=config_dir, delete=False) as f:
            tmp_path = f.name
            json.dump({k: v or "" for k, v in cfg.items()}, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, CONFIG_PATH)
    except OSError as e:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        logger.error(f"Error occurred while saving config: {e}")


# ----------------------------
# Auth token management & header merging
# ----------------------------


def merge_login_at(headers: Mapping[str, str], login_at: str | None) -> dict[str, str]:
    h = dict(headers or {})
    if login_at:
        h["login-at"] = login_at
    return h


SENSITIVE_KEY_PARTS = (
    "pass",
    "passwd",
    "password",
    "authorization",
    "token",
    "login-at",
    "login_at",
    "loginat",
    "_t",
    "cookie",
    "set-cookie",
)


def _is_sensitive_key(key: Any) -> bool:
    lowered = str(key).lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _mask_value(v: Any) -> Any:
    try:
        if isinstance(v, dict):
            return {k: "***" if _is_sensitive_key(k) else _mask_value(v2) for k, v2 in v.items()}
        if isinstance(v, list):
            return [_mask_value(x) for x in v]
        if isinstance(v, str):
            low = v.lower()
            # Mask long tokens / JWT-like
            if low.count(".") == 2 and all(len(p) > 5 for p in v.split(".")):
                parts = v.split(".")
                return parts[0][:6] + "..." + parts[-1][-6:]
            if len(v) > 64:
                return v[:32] + "…(trunc)"
            return v
        return v
    except Exception:
        return "<masked>"


def mask_kv(d: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if d is None:
        return d
    out = {}
    for k, v in d.items():
        if _is_sensitive_key(k):
            out[k] = "***"
        else:
            out[k] = _mask_value(v)
    return out


def j(x: Any) -> str:
    try:
        return json.dumps(x, ensure_ascii=False)
    except Exception:
        return str(x)


def load_netscape_cookies(path: str) -> MozillaCookieJar:
    jar = MozillaCookieJar()
    jar.load(path, ignore_discard=True, ignore_expires=True)
    return jar


def load_netscape_cookies_text(cookie_text: str) -> MozillaCookieJar:
    jar = MozillaCookieJar()
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
        tmp.write(cookie_text)
        tmp_path = tmp.name
    try:
        jar.load(tmp_path, ignore_discard=True, ignore_expires=True)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return jar


def get_cookie_value(cookie_jar: CookieJar, name: str) -> str | None:
    target = name.lower()
    try:
        for cookie in cookie_jar:
            if cookie.name.lower() == target:
                return cookie.value
    except (AttributeError, TypeError) as e:
        logger.error(f"Error occurred while reading cookies: {e}")
    return None


def cookie_auth_from_jar(cookie_jar: CookieJar, login_at_fallback: str | None = None) -> CookieAuth:
    return CookieAuth(
        login_at=get_cookie_value(cookie_jar, "LOGINAT")
        or get_cookie_value(cookie_jar, "login_at")
        or login_at_fallback,
        userkey=get_cookie_value(cookie_jar, "USERKEY"),
        tkey=get_cookie_value(cookie_jar, "TKEY"),
    )


def is_placeholder_userkey(userkey: str | None) -> bool:
    return userkey in {"login-user"}


def attach_auth_cookies(session, headers: Mapping[str, str] | None = None) -> dict[str, str] | None:
    ck = getattr(session, "cookies", None)
    if ck is None:
        return dict(headers) if headers is not None else None

    auth = cookie_auth_from_jar(ck)

    cookie_parts = []
    if auth.userkey:
        cookie_parts.append(f"USERKEY={auth.userkey}")
    if auth.tkey:
        cookie_parts.append(f"TKEY={auth.tkey}")

    cookie_parts.append("last_login=basic")

    if cookie_parts:
        merged = dict(headers or {})
        merged.setdefault("Cookie", "; ".join(cookie_parts))
        return merged

    return dict(headers) if headers is not None else None


# ----------------------------
# Token extraction (STRICT)
# ----------------------------


def iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_strings(v)


TOKEN_KEYS = ("_t", "t", "token")
CONTENT_ENDPOINT_HOST = "api-global.novelpia.com"
CONTENT_ENDPOINT_PATH = "/v1/novel/episode/content"


def _scan_mapping_for_token(
    source: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """Scan ``source`` for token keys.

    Returns ``(jwt_token, fallback_token)``. ``jwt_token`` is non-None only when a
    JWT-like token is found (caller should return immediately then).
    """
    fallback: str | None = None
    for k in TOKEN_KEYS:
        v = source.get(k)
        if isinstance(v, str) and v:
            if looks_like_jwt(v):
                return v, None
            fallback = fallback or v
    return None, fallback


def _content_url_token(s: str) -> tuple[str | None, str | None, bool]:
    """If ``s`` is the official content endpoint, return ``(jwt, fallback, is_content_url)``."""
    try:
        p = urlparse(s)
        if p.netloc.endswith(CONTENT_ENDPOINT_HOST) and p.path.endswith(CONTENT_ENDPOINT_PATH):
            q = parse_qs(p.query)
            values = q.get("_t")
            cand = values[0] if values else None
            if isinstance(cand, str) and cand:
                if looks_like_jwt(cand):
                    return cand, None, True
                return None, cand, True
    except Exception as e:
        logger.error(f"Error occurred while parsing URL: {e}")
    return None, None, False


def extract_t_token(tdata: Mapping[str, Any]) -> str | None:
    """Return the ``_t`` token to use with the episode content endpoint.

    Prefer JWT-like tokens, but accept any non-empty string if present.
    Checks common keys at the top level and in nested dicts, then falls back
    to scanning for a ``_t`` value embedded in the query string of a URL that
    points at the official content endpoint (Novelpia sometimes nests the
    token this way instead of as a bare key). Every case that yields a token
    yields one usable directly as the ``_t`` query param for
    ``NovelpiaClient.episode_content`` -- there is no case where only a
    standalone content URL is available without also yielding its token.
    """
    res = tdata.get("result", {}) if isinstance(tdata, dict) else {}
    fallback_token: str | None = None

    # 1) common keys at result
    jwt, fb = _scan_mapping_for_token(res)
    if jwt:
        return jwt
    fallback_token = fb

    # 2) nested dicts under result
    if isinstance(res, dict):
        for _, v in res.items():
            if isinstance(v, dict):
                j, fb2 = _scan_mapping_for_token(v)
                if j:
                    return j
                fallback_token = fallback_token or fb2

    # 3) URL that is the official content endpoint with any _t
    for s in iter_strings(tdata):
        if isinstance(s, str) and (s.startswith("http://") or s.startswith("https://")):
            jwt, fb, is_url = _content_url_token(s)
            if is_url:
                if jwt:
                    return jwt
                fallback_token = fallback_token or fb
    return fallback_token
