"""The speed caches never show stale data: settings are read again after any change to the file,
the scored demo book follows the scoring config and the day, and copies handed out can be
changed without touching what the next caller gets."""

from __future__ import annotations

from datetime import date, timedelta

from wardmind.core import service
from wardmind.core.config import load_scoring_config, save_scoring_config
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.settings import Theme, load_settings, save_settings, settings_path


def test_a_saved_setting_is_read_back_at_once_every_time() -> None:
    for theme in [Theme.DARK, Theme.LIGHT, Theme.SYSTEM, Theme.DARK]:
        save_settings(load_settings().model_copy(update={"theme": theme}))
        assert load_settings().theme is theme


def test_a_hand_edit_to_the_file_is_read_too() -> None:
    save_settings(load_settings().model_copy(update={"theme": Theme.DARK}))
    assert load_settings().theme is Theme.DARK
    path = settings_path()
    path.write_text(path.read_text(encoding="utf-8").replace("dark", "light"), encoding="utf-8")
    assert load_settings().theme is Theme.LIGHT


def test_a_removed_file_gives_the_defaults() -> None:
    save_settings(load_settings().model_copy(update={"theme": Theme.DARK}))
    assert load_settings().theme is Theme.DARK
    settings_path().unlink()
    assert load_settings().theme is Theme.LIGHT


def test_changing_the_settings_you_were_given_changes_nothing_saved() -> None:
    save_settings(load_settings().model_copy(update={"theme": Theme.DARK}))
    given = load_settings()
    given.theme = Theme.SYSTEM
    assert load_settings().theme is Theme.DARK


def test_the_demo_book_follows_the_scoring_config() -> None:
    before = {item.account.id: item.risk for item in service.get_book()}
    config = load_scoring_config()
    signals = {kind: rule.model_copy(update={"weight": 0}) for kind, rule in config.signals.items()}
    save_scoring_config(config.model_copy(update={"signals": signals}))
    after = {item.account.id: item.risk for item in service.get_book()}
    assert max(before.values()) > 0
    assert set(after.values()) == {0}  # no weight, no risk: the old scores weren't reused


def test_the_demo_book_is_scored_for_the_day_asked() -> None:
    today = date.today()
    earlier_day = today - timedelta(days=60)
    now = service.get_book(as_of=today)
    earlier = service.get_book(as_of=earlier_day)
    # Motion windows run from as_of: 60 days earlier, renewals and onboarding fall differently.
    assert [item.motion for item in now] != [item.motion for item in earlier]
    service.reset_connectors()
    assert service.get_book(as_of=earlier_day) == earlier  # reused equals worked out afresh


def test_the_list_handed_out_can_be_changed_safely() -> None:
    book = service.get_book()
    count = len(book)
    book.clear()
    assert len(service.get_book()) == count


def test_the_demo_dates_follow_the_day_the_connector_is_for() -> None:
    today = DemoConnector().list_accounts()
    tomorrow = DemoConnector(today=date.today() + timedelta(days=1)).list_accounts()
    assert tomorrow[0].renewal_date == today[0].renewal_date + timedelta(days=1)


def test_the_trend_is_the_same_whether_worked_out_or_reused() -> None:
    book = service.get_book()
    first = service.account_trend(book[0], 1)
    service.reset_connectors()
    again = service.account_trend(book[0], 1)
    assert first == again
    assert service.account_trend(book[0], 1) == first
