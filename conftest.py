"""Pytest configuration: make the vendored samsungtvws importable as 'samsungtvws'."""
import sys
from pathlib import Path

# The vendored samsungtvws lives inside the custom component package.
# Adding its parent directory to sys.path lets `import samsungtvws` resolve to
# the vendored copy (custom_components/frame_art_shuffler/samsungtvws/) instead
# of requiring the separately-installed PyPI package.
_VENDORED_PARENT = Path(__file__).parent / "custom_components" / "frame_art_shuffler"
sys.path.insert(0, str(_VENDORED_PARENT))