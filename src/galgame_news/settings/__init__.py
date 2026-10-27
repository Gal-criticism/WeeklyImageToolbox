"""Headless settings and security services for the desktop toolbox.

The module deliberately has no GUI or keyring imports at module import time so
the regular CLI remains usable when desktop extras are not installed.
"""

from .credentials import (
    CredentialBackend,
    CredentialBundle,
    CredentialStore,
    InMemoryCredentialBackend,
    KeyringCredentialBackend,
    KeyringCredentialStore,
)
from .ffmpeg import FFmpegStatus, discover_ffmpeg, resolve_ffmpeg, resolve_ffmpeg_location
from .browser import PLAYWRIGHT_VERSION, browser_install_commands, check_browser_runtime
from .model import ArchiveSettings, AppSettings, SettingsStore, default_app_data_dir

__all__ = [
    "AppSettings",
    "ArchiveSettings",
    "PLAYWRIGHT_VERSION",
    "CredentialBackend",
    "CredentialBundle",
    "CredentialStore",
    "FFmpegStatus",
    "InMemoryCredentialBackend",
    "KeyringCredentialBackend",
    "KeyringCredentialStore",
    "SettingsStore",
    "default_app_data_dir",
    "browser_install_commands",
    "check_browser_runtime",
    "discover_ffmpeg",
    "resolve_ffmpeg",
    "resolve_ffmpeg_location",
]
