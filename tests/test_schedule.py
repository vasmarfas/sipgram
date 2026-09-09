"""Working hours, quiet hours and the number lists."""
from datetime import datetime

import pytest

from sipgram.config import ConfigError, ScheduleConfig, load_config
from sipgram.schedule import Schedule, ScheduleError, match_number, screen, time_closed

MON_10 = datetime(2026, 9, 7, 10, 0)     # Monday
MON_20 = datetime(2026, 9, 7, 20, 0)
SAT_12 = datetime(2026, 9, 12, 12, 0)
SUN_02 = datetime(2026, 9, 13, 2, 0)


def test_windows_parse_from_both_shapes():
    a = Schedule.parse("mon-fri 09:00-18:00")
    b = Schedule.parse([{"days": "weekdays", "from": "09:00", "to": "18:00"}])
    assert str(a) == str(b) == "mon,tue,wed,thu,fri 09:00-18:00"
    assert a.matches(MON_10) and not a.matches(MON_20) and not a.matches(SAT_12)


def test_window_over_midnight_belongs_to_the_day_it_starts_on():
    night = Schedule.parse("sat 22:00-03:00")
    assert night.matches(datetime(2026, 9, 12, 23, 30))
    assert night.matches(SUN_02), "the tail after midnight still counts as Saturday's window"
    assert not night.matches(datetime(2026, 9, 12, 21, 0))


def test_day_ranges_wrap_and_aliases_work():
    assert Schedule.parse("sat-sun 00:00-24:00").matches(SAT_12)
    assert Schedule.parse("fri-mon 10:00-11:00").matches(MON_10), "fri-mon wraps over the weekend"
    assert Schedule.parse("weekend 12:00-13:00").matches(SAT_12)
    assert Schedule.parse("пн 09:00-18:00").matches(MON_10)


def test_empty_schedule_matches_nothing_and_is_empty():
    assert Schedule.parse(None).empty and not Schedule.parse([]).matches(MON_10)


@pytest.mark.parametrize("spec", ["mon-fri", "mon-fri 9:00", "xyz 09:00-10:00", "mon-fri 25:00-26:00",
                                  [{"days": "mon", "until": "10:00"}]])
def test_bad_windows_are_rejected(spec):
    with pytest.raises(ScheduleError):
        Schedule.parse(spec)


@pytest.mark.parametrize("pattern,number,expected", [
    ("+79991234567", "79991234567", True),
    ("7999*", "+7 999 123-45-67", True),
    ("7999*", "74951234567", False),
    ("101", "1010", False),
    ("~^749[5-9]", "74951234567", True),
    ("anonymous", "", True),
    ("anonymous", "101", False),
])
def test_number_patterns(pattern, number, expected):
    assert match_number(pattern, number) is expected


def test_quiet_hours_and_lists():
    rules = ScheduleConfig(quiet_hours=["all 22:00-08:00"], blacklist=["7495*"])
    assert screen(rules, "79991234567", moment=MON_10).allowed
    assert screen(rules, "74951234567", moment=MON_10).reason == "blacklist"
    assert screen(rules, "79991234567", moment=datetime(2026, 9, 7, 23, 0)).reason == "quiet"
    assert time_closed(rules, MON_10) == "" and time_closed(rules, SUN_02) == "quiet"


def test_whitelist_beats_the_clock():
    rules = ScheduleConfig(work_hours=["mon-fri 09:00-18:00"], whitelist=["101", "+79990000000"])
    assert screen(rules, "101", moment=MON_20).allowed, "a whitelisted number rings at any hour"
    assert screen(rules, "102", moment=MON_20).reason == "whitelist"
    assert screen(rules, "102", moment=MON_10).reason == "whitelist", "a whitelist filters everyone else"
    assert screen(ScheduleConfig(work_hours=["mon-fri 09:00-18:00"]), "102", moment=MON_20).reason == "off_hours"


def test_empty_config_lets_everything_through():
    rules = ScheduleConfig()
    assert rules.empty and screen(rules, "").allowed and screen(rules, "74951234567").allowed


def test_action_maps_to_a_sip_code():
    assert ScheduleConfig().code == 480
    assert ScheduleConfig(action="busy").code == 486 and ScheduleConfig(action="reject").code == 603
    with pytest.raises(ConfigError):
        ScheduleConfig(action="voicemail")


def test_config_file_gives_each_user_their_own_rules(tmp_path):
    (tmp_path / "config.yaml").write_text("""
telegram: {api_id: 1, api_hash: h}
users:
  - {id: 111, name: A}
  - {id: 222, name: B, schedule: {quiet_hours: ["all 00:00-23:59"]}}
sip:
  server: pbx
  accounts:
    - {username: "491", password: x, users: [111, 222]}
schedule:
  work_hours: ["mon-fri 09:00-18:00"]
  blacklist: ["7495*"]
  action: busy
""", encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    a, b = cfg.users
    assert a.rules is cfg.schedule and a.rules.code == 486
    assert screen(a.rules, "101", moment=MON_10).allowed and screen(a.rules, "101", moment=MON_20).reason == "off_hours"
    assert b.rules.blacklist == ["7495*"], "a per-user schedule inherits the global keys it does not set"
    assert screen(b.rules, "101", moment=MON_10).reason == "quiet"


def test_bad_schedule_in_the_file_is_a_config_error(tmp_path):
    (tmp_path / "config.yaml").write_text("""
telegram: {api_id: 1, api_hash: h}
users: [111]
sip:
  server: pbx
  accounts: [{username: "491", password: x, users: [111]}]
schedule: {work_hours: ["mon-fri"]}
""", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path / "config.yaml")
