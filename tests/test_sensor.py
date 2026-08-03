"""Unit tests for CSG sensor data handling."""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import STATE_UNAVAILABLE, UnitOfEnergy

from custom_components.csg.const import (
    ATTR_KEY_SETTLEMENT_DATE,
    SUFFIX_ENERGY_TOTAL,
    SUFFIX_LATEST_DAY_COST,
    SUFFIX_LATEST_DAY_KWH,
    SUFFIX_SETTLED_COST_TOTAL,
)
from custom_components.csg.csg_client import CSGAPIError
from custom_components.csg.sensor import (
    BILLING_DESCRIPTIONS,
    CURRENT_DESCRIPTIONS,
    ENERGY_TOTAL,
    REALTIME_DESCRIPTIONS,
    SETTLED_COST_TOTAL,
    BillingCoordinator,
    CSGSensor,
    EnergyLedger,
    _ladder_data,
    _merge_daily_days,
    _set_latest_day,
)


class MemoryStore:
    """Minimal Store replacement for unit-testing the ledger."""

    def __init__(self) -> None:
        self.saved_data: dict | None = None

    async def async_save(self, data: dict) -> None:
        self.saved_data = data


def make_ledger() -> EnergyLedger:
    """Create an EnergyLedger without a Home Assistant instance."""
    ledger = EnergyLedger.__new__(EnergyLedger)
    ledger._data = {"accounts": {}}
    ledger._lock = asyncio.Lock()
    ledger._store = MemoryStore()
    return ledger


def run(coroutine):
    """Run an async unit under pytest without pytest-asyncio."""
    return asyncio.run(coroutine)


def test_energy_sensor_descriptions_have_correct_statistics_semantics() -> None:
    """Only Energy dashboard counters are total-increasing sensors."""
    assert ENERGY_TOTAL.device_class is SensorDeviceClass.ENERGY
    assert ENERGY_TOTAL.unit is UnitOfEnergy.KILO_WATT_HOUR
    assert ENERGY_TOTAL.state_class is SensorStateClass.TOTAL_INCREASING
    assert SETTLED_COST_TOTAL.device_class is SensorDeviceClass.MONETARY
    assert SETTLED_COST_TOTAL.unit == "CNY"
    assert SETTLED_COST_TOTAL.state_class is SensorStateClass.TOTAL_INCREASING

    snapshots = (*REALTIME_DESCRIPTIONS, *CURRENT_DESCRIPTIONS, *BILLING_DESCRIPTIONS)
    assert all(
        description.state_class is not SensorStateClass.TOTAL_INCREASING
        for description in snapshots
    )


def test_ledger_records_realtime_usage_once_per_day() -> None:
    """Realtime values only increase a day's contribution."""
    ledger = make_ledger()

    assert run(ledger.async_record_realtime("account", "2026-08-01", 10)) == 10
    assert run(ledger.async_record_realtime("account", "2026-08-01", 10)) == 10
    assert run(ledger.async_record_realtime("account", "2026-08-01", 12)) == 12
    assert run(ledger.async_record_realtime("account", "2026-08-01", 11)) == 12
    assert run(ledger.async_record_realtime("account", "2026-08-02", 4)) == 16


def test_ledger_initializes_zero_totals() -> None:
    """Valid zero readings must not leave the running totals undefined."""
    ledger = make_ledger()

    assert run(ledger.async_record_realtime("account", "2026-08-01", 0)) == 0
    assert run(
        ledger.async_record_billing("account", [{"date": "2026-08-01", "kwh": 0}])
    )[0] == 0


def test_ledger_billing_correction_and_settlement_lock() -> None:
    """Billing changes are reported for Recorder and never double-count usage."""
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", "2026-08-01", 10))

    cost_total, changes = run(
        ledger.async_record_billing(
            "account", [{"date": "2026-08-01", "kwh": 12, "charge": 6}]
        )
    )
    assert cost_total == 6
    assert changes == {"2026-08-01": ({"kwh": 10.0}, {"kwh": 12.0, "charge": 6.0})}
    assert run(ledger.async_record_realtime("account", "2026-08-01", 13)) == 10

    cost_total, changes = run(
        ledger.async_record_billing(
            "account", [{"date": "2026-08-01", "kwh": 12, "charge": 7}]
        )
    )
    assert cost_total == 6
    assert changes == {
        "2026-08-01": (
            {"kwh": 12.0, "charge": 6.0},
            {"kwh": 12.0, "charge": 7.0},
        )
    }


def test_ledger_imports_billing_usage_missed_after_installation() -> None:
    """Settled usage fills a post-installation realtime polling gap."""
    ledger = make_ledger()
    run(ledger.async_record_realtime("account", "2026-08-01", 1))

    _, changes = run(
        ledger.async_record_billing(
            "account", [{"date": "2026-08-02", "kwh": 4, "charge": 2}]
        )
    )

    assert ledger.energy_total("account") == 1
    assert changes["2026-08-02"] == ({"kwh": 0.0}, {"kwh": 4.0, "charge": 2.0})


def test_sensor_uses_initial_coordinator_data_and_clears_missing_values() -> None:
    """Sensors expose the first refresh and never retain a failed value."""
    coordinator = SimpleNamespace(
        data={"account": {SUFFIX_ENERGY_TOTAL: 3.5}}, last_update_success=True
    )
    sensor = CSGSensor(coordinator, "account", ENERGY_TOTAL)

    assert sensor.native_value == 3.5
    assert sensor.available
    coordinator.data = {"account": {SUFFIX_ENERGY_TOTAL: STATE_UNAVAILABLE}}
    sensor._update_from_coordinator()
    assert sensor.native_value is None
    assert not sensor.available


def test_ledger_keeps_accounts_and_cost_days_independent() -> None:
    """Separate payment accounts must not share a ledger or cost baseline."""
    ledger = make_ledger()

    assert run(ledger.async_record_realtime("first", "2026-08-01", 1)) == 1
    assert run(ledger.async_record_realtime("second", "2026-08-01", 2)) == 2
    assert run(
        ledger.async_record_billing(
            "first", [{"date": "2026-08-01", "kwh": 1, "charge": 1.5}]
        )
    )[0] == 1.5
    assert run(
        ledger.async_record_billing(
            "second", [{"date": "2026-08-01", "kwh": 2, "charge": 3.0}]
        )
    )[0] == 3.0


def test_merge_daily_days_prefers_usage_endpoint_for_kwh() -> None:
    """Usage and charge endpoints have separate authoritative fields."""
    merged = _merge_daily_days(
        [
            {"date": "2026-08-01", "kwh": 1.2},
            {"date": "2026-08-02", "kwh": 2.3},
        ],
        [
            {"date": "2026-08-01", "kwh": 1.1, "charge": 0.6},
            {"date": "2026-08-03", "kwh": 3.4, "charge": 1.2},
        ],
    )

    assert merged == [
        {"date": "2026-08-01", "kwh": 1.2, "charge": 0.6},
        {"date": "2026-08-02", "kwh": 2.3},
        {"date": "2026-08-03", "kwh": 3.4, "charge": 1.2},
    ]


def test_set_latest_day_marks_missing_data_unavailable() -> None:
    """Latest settlement sensors are unavailable when no daily bill exists."""
    data: dict = {}
    _set_latest_day(data, [])
    assert data == {
        SUFFIX_LATEST_DAY_KWH: STATE_UNAVAILABLE,
        SUFFIX_LATEST_DAY_COST: STATE_UNAVAILABLE,
    }

    _set_latest_day(data, [{"date": "2026-08-03", "kwh": 4.5, "charge": 2.0}])
    assert data[SUFFIX_LATEST_DAY_KWH] == 4.5
    assert data[SUFFIX_LATEST_DAY_COST] == 2.0
    assert data[ATTR_KEY_SETTLEMENT_DATE] == {ATTR_KEY_SETTLEMENT_DATE: "2026-08-03"}


def test_ladder_data_handles_missing_values() -> None:
    """Null ladder fields are exposed as unavailable rather than invalid values."""
    data = _ladder_data({})
    assert all(value == STATE_UNAVAILABLE for key, value in data.items() if key != "current_ladder_start_date")


class FakeLedger:
    """Capture billing rows passed from the billing coordinator."""

    def __init__(self) -> None:
        self.days: list[dict] = []

    async def async_record_billing(self, account: str, days: list[dict]):
        self.days = days
        return 5.0, {}


class FakeBillingCoordinator:
    """Small collaborator for testing BillingCoordinator._update_account."""

    _fetch = staticmethod(lambda function, *args: _call(function, *args))

    def __init__(self) -> None:
        self.ledger = FakeLedger()
        self.corrected: dict | None = None

    async def _async_correct_statistics(self, account: str, changed: dict) -> None:
        self.corrected = changed

    async def _add_year_data(self, client, account, data: dict) -> None:
        return None


async def _call(function, *args):
    return function(*args)


class FakeClient:
    """Provide deterministic daily responses without network I/O."""

    def get_month_daily_usage_detail(self, account, year_month):
        if year_month == (2026, 8):
            return 0.0, []
        return 4.5, [{"date": "2026-07-31", "kwh": 4.5}]

    def get_month_daily_cost_detail(self, account, year_month):
        if year_month == (2026, 8):
            return 0.0, 0.0, {}, []
        return 2.0, 4.5, {}, [{"date": "2026-07-31", "kwh": 4.5, "charge": 2.0}]


def test_billing_coordinator_falls_back_to_last_month_settlement(monkeypatch) -> None:
    """The latest settlement day uses last month when current month is empty."""
    coordinator = FakeBillingCoordinator()
    account = SimpleNamespace(account_number="account")
    monkeypatch.setattr(
        "custom_components.csg.sensor.dt_util.now",
        lambda: SimpleNamespace(date=lambda: __import__("datetime").date(2026, 8, 3)),
    )

    data = run(
        BillingCoordinator._update_account(
            coordinator, FakeClient(), account, [(2026, 8), (2026, 7)]
        )
    )

    assert data[SUFFIX_LATEST_DAY_KWH] == 4.5
    assert data[SUFFIX_LATEST_DAY_COST] == 2.0
    assert data[ATTR_KEY_SETTLEMENT_DATE] == {ATTR_KEY_SETTLEMENT_DATE: "2026-07-31"}
    assert data[SUFFIX_SETTLED_COST_TOTAL] == 5.0


def test_billing_coordinator_marks_failed_month_unavailable(monkeypatch) -> None:
    """A failed month request leaves its snapshots explicitly unavailable."""
    class FailingClient:
        def get_month_daily_usage_detail(self, account, year_month):
            raise CSGAPIError("failure")

        def get_month_daily_cost_detail(self, account, year_month):
            raise CSGAPIError("failure")

    coordinator = FakeBillingCoordinator()
    account = SimpleNamespace(account_number="account")
    monkeypatch.setattr(
        "custom_components.csg.sensor.dt_util.now",
        lambda: SimpleNamespace(date=lambda: __import__("datetime").date(2026, 8, 3)),
    )

    data = run(
        BillingCoordinator._update_account(
            coordinator, FailingClient(), account, [(2026, 8), (2026, 7)]
        )
    )

    assert data[SUFFIX_LATEST_DAY_KWH] == STATE_UNAVAILABLE
    assert data[SUFFIX_LATEST_DAY_COST] == STATE_UNAVAILABLE


def test_billing_correction_adjusts_existing_energy_and_cost_statistics(
    monkeypatch,
) -> None:
    """Recorder adjustments use the registered entity IDs and daily delta."""
    adjustments: list[tuple[str, dt.datetime, float, str]] = []

    class FakeRecorder:
        def async_adjust_statistics(self, statistic_id, start, adjustment, unit):
            adjustments.append((statistic_id, start, adjustment, unit))

    class FakeRegistry:
        def async_get_entity_id(self, domain, platform, unique_id):
            assert domain == "sensor"
            assert platform == "csg"
            return f"sensor.{unique_id.rsplit('.', 1)[-1]}"

    coordinator = BillingCoordinator.__new__(BillingCoordinator)
    coordinator.hass = object()
    monkeypatch.setattr("custom_components.csg.sensor.er.async_get", lambda hass: FakeRegistry())
    monkeypatch.setattr(
        "homeassistant.components.recorder.get_instance", lambda hass: FakeRecorder()
    )

    run(
        BillingCoordinator._async_correct_statistics(
            coordinator,
            "account",
            {
                "2026-08-01": (
                    {"kwh": 10.0, "charge": 4.0},
                    {"kwh": 12.0, "charge": 5.5},
                ),
                "2026-08-02": ({}, {"kwh": 3.0, "charge": 1.0}),
            },
        )
    )

    assert [(statistic_id, adjustment, unit) for statistic_id, _, adjustment, unit in adjustments] == [
        ("sensor.energy_total", 2.0, "kWh"),
        ("sensor.settled_cost_total", 1.5, "CNY"),
    ]
    assert all(start.date() == dt.date(2026, 8, 1) for _, start, _, _ in adjustments)
