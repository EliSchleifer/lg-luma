"""Home Assistant tests for custom_components/luma (pytest-homeassistant-custom-component)."""

import pathlib
import sys

import pytest

# custom_components/ lives at the repository root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
