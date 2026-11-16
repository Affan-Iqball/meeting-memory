"""Local settings for running the model steps: where the Claude CLI is, how it signs in, which models.

Stored in ~/.config/meetmem/settings.json (file mode 600, outside the repository), so an API key
is never written into the project. Environment variables always win over the saved file:
ANTHROPIC_API_KEY, CLAUDE_BIN, MEETMEM_ANSWER_MODEL, MEETMEM_EXTRACT_MODEL.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "meetmem")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")
MODELS = ["haiku", "sonnet", "opus"]
DEFAULTS = {"answer_model": "sonnet", "extract_model": "haiku"}

# set at process start, before anything we apply ourselves
_ENV_KEY = os.environ.get("ANTHROPIC_API_KEY")
_ENV_BIN = os.environ.get("CLAUDE_BIN")
_session_key: str | None = None  # entered in the interface without "remember"


def load() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(d: dict) -> None:
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)
    os.replace(tmp, CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)


def detect_claude() -> str | None:
    """CLAUDE_BIN, then `claude` on PATH, then the copy bundled with the Claude desktop app (newest)."""
    for cand in (_ENV_BIN, load().get("claude_bin"), shutil.which("claude")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    pattern = os.path.join(os.path.expanduser("~"), ".config", "Claude", "claude-code", "*", "*", "claude")
    found = [p for p in glob.glob(pattern) if os.access(p, os.X_OK)]
    found.sort(key=os.path.getmtime, reverse=True)
    return found[0] if found else None


def claude_version(path: str | None) -> str | None:
    if not path:
        return None
    try:
        p = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20)
        return (p.stdout or p.stderr).strip().splitlines()[0][:80] if (p.stdout or p.stderr) else None
    except (OSError, subprocess.SubprocessError):
        return None


def current_key() -> tuple[str | None, str]:
    if _ENV_KEY:
        return _ENV_KEY, "environment variable"
    if _session_key:
        return _session_key, "entered in the interface (this session only)"
    k = load().get("api_key")
    if k:
        return k, f"saved on this computer ({CONFIG_PATH})"
    return None, "none"


def set_key(key: str | None, remember: bool) -> None:
    global _session_key
    d = load()
    if key is None:  # forget everywhere we control
        _session_key = None
        d.pop("api_key", None)
        save(d)
    elif remember:
        _session_key = None
        d["api_key"] = key
        save(d)
    else:
        _session_key = key
        if "api_key" in d:
            d.pop("api_key")
            save(d)
    apply()


def models() -> dict:
    d = load()
    return {"answer_model": os.environ.get("MEETMEM_ANSWER_MODEL") or d.get("answer_model") or DEFAULTS["answer_model"],
            "extract_model": os.environ.get("MEETMEM_EXTRACT_MODEL") or d.get("extract_model") or DEFAULTS["extract_model"]}


def set_models(answer_model=None, extract_model=None, claude_bin=None) -> None:
    d = load()
    for k, v in (("answer_model", answer_model), ("extract_model", extract_model)):
        if v:
            if v not in MODELS:
                raise ValueError(f"model must be one of {MODELS}")
            d[k] = v
    if claude_bin is not None:
        if claude_bin and not (os.path.isfile(claude_bin) and os.access(claude_bin, os.X_OK)):
            raise ValueError(f"not an executable file: {claude_bin}")
        if claude_bin:
            d["claude_bin"] = claude_bin
        else:
            d.pop("claude_bin", None)
    save(d)
    apply()


def apply() -> None:
    """Point this process's environment at the chosen CLI and key (the agent runner reads os.environ)."""
    b = detect_claude()
    if b:
        os.environ["CLAUDE_BIN"] = b
    k, _ = current_key()
    if k:
        os.environ["ANTHROPIC_API_KEY"] = k
    else:
        os.environ.pop("ANTHROPIC_API_KEY", None)


def status(with_version=True) -> dict:
    k, src = current_key()
    b = detect_claude()
    return {"key": {"set": bool(k), "source": src, "hint": ("…" + k[-4:]) if k and len(k) > 8 else None},
            "auth_mode": "API key (billed per token)" if k else "Claude subscription login (claude login)",
            "claude_bin": {"path": b, "found": bool(b), "version": claude_version(b) if with_version else None},
            "models": models(), "model_choices": MODELS, "config_path": CONFIG_PATH}
