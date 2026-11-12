"""Portable preview-access credential format, separate from deployment tokens."""
import re

KEY_PATTERN = re.compile(r'poteto_preview_[A-Za-z0-9_-]{43}')
