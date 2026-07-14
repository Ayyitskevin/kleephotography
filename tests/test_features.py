"""Unit tests for centralized feature flags.

Part of test extraction plan; covers the dormant-by-env contract.
"""

from unittest.mock import patch

import pytest

from app import features

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_stripe_enabled_true_when_secret_set():
    with patch("app.features.config") as mock_cfg:
        mock_cfg.STRIPE_SECRET_KEY = "sk_live_xxx"
        assert features.stripe_enabled() is True


@pytest.mark.unit
def test_stripe_enabled_false_when_empty():
    with patch("app.features.config") as mock_cfg:
        mock_cfg.STRIPE_SECRET_KEY = ""
        assert features.stripe_enabled() is False


@pytest.mark.unit
def test_odysseus_caption_enabled_requires_both():
    with patch("app.features.config") as mock_cfg:
        mock_cfg.ODYSSEUS_CAPTION_URL = "http://x"
        mock_cfg.ODYSSEUS_CAPTION_TOKEN = "t"
        assert features.odysseus_caption_enabled() is True

        mock_cfg.ODYSSEUS_CAPTION_TOKEN = ""
        assert features.odysseus_caption_enabled() is False


@pytest.mark.unit
def test_gmail_enabled_and_telegram_and_sms():
    with patch("app.features.config") as mock_cfg:
        mock_cfg.GMAIL_USER = "u"
        mock_cfg.GMAIL_APP_PASSWORD = "p"
        assert features.gmail_enabled() is True

        mock_cfg.QUO_API_KEY = "k"
        mock_cfg.QUO_NUMBER = "+1"
        assert features.sms_enabled() is True

        mock_cfg.TELEGRAM_TOKEN = "tok"
        mock_cfg.TELEGRAM_CHAT_ID = "-1"
        assert features.telegram_enabled() is True


@pytest.mark.unit
def test_notion_variants():
    with patch("app.features.config") as mock_cfg:
        mock_cfg.NOTION_TOKEN = "n"
        mock_cfg.NOTION_BOOKINGS_DB = "db1"
        mock_cfg.NOTION_SESSIONS_DB = ""
        assert features.notion_enabled() is True
        assert features.notion_bookings_enabled() is True
        assert features.notion_sessions_enabled() is False


@pytest.mark.unit
def test_screening_room_and_aerials_flags():
    with patch("app.features.config") as mock_cfg:
        mock_cfg.SCREENING_ROOM = True
        mock_cfg.AERIALS_LIVE = False
        assert features.screening_room() is True
        assert features.aerials_live() is False

        mock_cfg.SCREENING_ROOM = False
        mock_cfg.AERIALS_LIVE = True
        assert features.screening_room() is False
        assert features.aerials_live() is True


@pytest.mark.unit
def test_demo_gallery_and_plausible():
    with patch("app.features.config") as mock_cfg:
        mock_cfg.DEMO_GALLERY_SLUG = "demo"
        mock_cfg.DEMO_GALLERY_PIN = "1234"
        assert features.demo_gallery_enabled() is True

        mock_cfg.PLAUSIBLE_DOMAIN = "kleephotography.com"
        assert features.plausible_enabled() is True
