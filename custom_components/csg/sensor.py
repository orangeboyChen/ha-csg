"""Sensors for the China Southern Power Grid integration."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, STATE_UNAVAILABLE, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.components import persistent_notification
from homeassistant.util import dt as dt_util
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    ATTR_KEY_CURRENT_LADDER_START_DATE,
    ATTR_KEY_MONTH_BILLING_DELAY,
    ATTR_KEY_SETTLEMENT_DATE,
    ATTR_KEY_YEAR_BILLING_DELAY,
    CONF_AUTH_TOKEN,
    CONF_ELE_ACCOUNTS,
    CONF_SETTINGS,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    SETTING_UPDATE_TIMEOUT,
    STORAGE_KEY,
    STORAGE_VERSION,
    SUFFIX_ARR,
    SUFFIX_BAL,
    SUFFIX_CURRENT_LADDER,
    SUFFIX_CURRENT_LADDER_REMAINING_KWH,
    SUFFIX_CURRENT_LADDER_TARIFF,
    SUFFIX_ENERGY_TOTAL,
    SUFFIX_LAST_MONTH_COST,
    SUFFIX_LAST_MONTH_KWH,
    SUFFIX_LAST_YEAR_COST,
    SUFFIX_LAST_YEAR_KWH,
    SUFFIX_LATEST_DAY_COST,
    SUFFIX_LATEST_DAY_KWH,
    SUFFIX_SETTLED_COST_TOTAL,
    SUFFIX_THIS_MONTH_COST,
    SUFFIX_THIS_MONTH_KWH,
    SUFFIX_THIS_YEAR_COST,
    SUFFIX_THIS_YEAR_KWH,
    SUFFIX_YESTERDAY_KWH,
)
from .csg_client import (
    WF_ATTR_CHARGE,
    WF_ATTR_DATE,
    WF_ATTR_KWH,
    WF_ATTR_LADDER,
    WF_ATTR_LADDER_REMAINING_KWH,
    WF_ATTR_LADDER_START_DATE,
    WF_ATTR_LADDER_TARIFF,
    CSGAPIError,
    CSGClient,
    CSGElectricityAccount,
)

_LOGGER = logging.getLogger(__name__)
_BILLING_DELAY = 2
_CSG_TIME_ZONE = ZoneInfo("Asia/Shanghai")
FETCH_EXCEPTIONS = (CSGAPIError, asyncio.TimeoutError, ValueError, requests.RequestException)


@dataclass(frozen=True)
class SensorDescription:
    """Metadata for a CSG sensor."""

    suffix: str
    translation_key: str
    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    state_class: SensorStateClass | None = None
    icon: str | None = None
    attributes_key: str | None = None


ENERGY_TOTAL = SensorDescription(
    SUFFIX_ENERGY_TOTAL,
    "energy_total",
    SensorDeviceClass.ENERGY,
    UnitOfEnergy.KILO_WATT_HOUR,
    SensorStateClass.TOTAL_INCREASING,
    "mdi:lightning-bolt",
)
SETTLED_COST_TOTAL = SensorDescription(
    SUFFIX_SETTLED_COST_TOTAL,
    "settled_cost_total",
    SensorDeviceClass.MONETARY,
    "CNY",
    SensorStateClass.TOTAL_INCREASING,
    "mdi:currency-cny",
)
REALTIME_DESCRIPTIONS = (
    SensorDescription(SUFFIX_YESTERDAY_KWH, "yesterday_usage", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.MEASUREMENT, "mdi:calendar-arrow-left"),
    SensorDescription(SUFFIX_BAL, "balance", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:wallet"),
    SensorDescription(SUFFIX_ARR, "arrears", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:cash-remove"),
)
CURRENT_DESCRIPTIONS = (
    SensorDescription(SUFFIX_CURRENT_LADDER, "current_ladder", icon="mdi:stairs", attributes_key=ATTR_KEY_CURRENT_LADDER_START_DATE),
    SensorDescription(SUFFIX_CURRENT_LADDER_REMAINING_KWH, "current_ladder_remaining", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.MEASUREMENT, "mdi:lightning-bolt-circle"),
    SensorDescription(SUFFIX_CURRENT_LADDER_TARIFF, "current_ladder_tariff", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:currency-cny"),
)
BILLING_DESCRIPTIONS = (
    SensorDescription(SUFFIX_LATEST_DAY_KWH, "latest_settlement_usage", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.MEASUREMENT, "mdi:calendar-check", ATTR_KEY_SETTLEMENT_DATE),
    SensorDescription(SUFFIX_LATEST_DAY_COST, "latest_settlement_cost", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:calendar-check", ATTR_KEY_SETTLEMENT_DATE),
    SensorDescription(SUFFIX_THIS_MONTH_KWH, "this_month_usage", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.MEASUREMENT, "mdi:calendar-month", ATTR_KEY_MONTH_BILLING_DELAY),
    SensorDescription(SUFFIX_THIS_MONTH_COST, "this_month_cost", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:calendar-month", ATTR_KEY_MONTH_BILLING_DELAY),
    SensorDescription(SUFFIX_LAST_MONTH_KWH, "last_month_usage", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.MEASUREMENT, "mdi:calendar-minus"),
    SensorDescription(SUFFIX_LAST_MONTH_COST, "last_month_cost", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:calendar-minus"),
    SensorDescription(SUFFIX_THIS_YEAR_KWH, "this_year_usage", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.MEASUREMENT, "mdi:calendar-range", ATTR_KEY_YEAR_BILLING_DELAY),
    SensorDescription(SUFFIX_THIS_YEAR_COST, "this_year_cost", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:calendar-range", ATTR_KEY_YEAR_BILLING_DELAY),
    SensorDescription(SUFFIX_LAST_YEAR_KWH, "last_year_usage", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.MEASUREMENT, "mdi:calendar-arrow-left"),
    SensorDescription(SUFFIX_LAST_YEAR_COST, "last_year_cost", SensorDeviceClass.MONETARY, "CNY", SensorStateClass.MEASUREMENT, "mdi:calendar-arrow-left"),
)


class EnergyLedger:
    """Persist source values used to build and correct energy statistics."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry_id}")
        self._data: dict[str, Any] = {"accounts": {}}
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {"accounts": {}}
        self._data.setdefault("accounts", {})

    async def async_record_realtime(self, account: str, day: str, value: float) -> float:
        async with self._lock:
            ledger = self._account(account)
            ledger.setdefault("energy_started_on", day)
            realtime = ledger.setdefault("realtime", {})
            realtime[day] = value
            if day in ledger.setdefault("billing", {}):
                await self._store.async_save(self._data)
                return float(ledger.get("energy_total", 0))
            reported_days = ledger.setdefault("reported_realtime", {})
            counted_days = ledger.setdefault("counted_realtime", {})
            reported = float(reported_days.get(day, 0))
            if value > reported:
                ledger["energy_total"] = float(ledger.get("energy_total", 0)) + value - reported
                reported_days[day] = value
                counted_days[day] = value
            await self._store.async_save(self._data)
            return float(ledger.setdefault("energy_total", 0.0))

    async def async_record_billing(
        self, account: str, days: Iterable[dict[str, float | str]]
    ) -> tuple[float, dict[str, tuple[dict[str, float], dict[str, float]]]]:
        async with self._lock:
            ledger = self._account(account)
            billing = ledger.setdefault("billing", {})
            reported_energy_days = ledger.setdefault("reported_realtime", {})
            reported_cost_days = ledger.setdefault("reported_cost_days", {})
            pending = ledger.setdefault("pending_corrections", {})
            changed: dict[str, tuple[dict[str, float], dict[str, float]]] = {}
            for item in days:
                day = str(item[WF_ATTR_DATE])
                values = {key: float(item[key]) for key in (WF_ATTR_KWH, WF_ATTR_CHARGE) if key in item}
                existing = billing.get(day)
                previous = existing
                if previous is None:
                    realtime_usage = ledger.setdefault("realtime", {}).get(day)
                    previous = (
                        {WF_ATTR_KWH: float(realtime_usage)}
                        if realtime_usage is not None
                        else {}
                    )
                merged = {**previous, **values}
                if existing != merged:
                    billing[day] = merged
                if previous != merged:
                    changed[day] = (previous, merged)
                usage = merged.get(WF_ATTR_KWH)
                reported_usage = reported_energy_days.get(day)
                if usage is not None and (
                    reported_usage is not None
                    or day >= ledger.get("energy_started_on", "9999-12-31")
                ):
                    baseline = float(reported_usage or 0)
                    reported_energy_days[day] = usage
                    if WF_ATTR_KWH not in previous:
                        changed[day] = ({**previous, WF_ATTR_KWH: baseline}, merged)
                charge = merged.get(WF_ATTR_CHARGE)
                reported_charge = reported_cost_days.get(day)
                if charge is not None and reported_charge != charge:
                    if reported_charge is None:
                        ledger["settled_cost_total"] = float(
                            ledger.get("settled_cost_total", 0)
                        ) + charge
                    reported_cost_days[day] = charge
                if previous != merged:
                    correction_previous, correction_current = changed.get(
                        day, (previous, merged)
                    )
                    original, _ = pending.get(
                        day, (correction_previous, correction_current)
                    )
                    pending[day] = (original, correction_current)
            await self._store.async_save(self._data)
            corrections = {
                day: (dict(previous), dict(current))
                for day, (previous, current) in pending.items()
            }
            return float(ledger.setdefault("settled_cost_total", 0.0)), corrections

    async def async_acknowledge_corrections(
        self,
        account: str,
        acknowledgements: dict[str, set[str]],
    ) -> None:
        """Record individual Recorder adjustments without losing failed ones."""
        async with self._lock:
            pending = self._account(account).setdefault("pending_corrections", {})
            for day, keys in acknowledgements.items():
                if day not in pending:
                    continue
                previous, current = pending[day]
                previous = dict(previous)
                for key in keys:
                    if key in current:
                        previous[key] = current[key]
                pending[day] = (previous, current)
                if all(
                    key not in previous or current.get(key, 0) == previous[key]
                    for key in (WF_ATTR_KWH, WF_ATTR_CHARGE)
                ):
                    del pending[day]
            await self._store.async_save(self._data)

    def billing_days(self, account: str) -> dict[str, dict[str, float]]:
        return self._account(account).get("billing", {})

    def energy_total(self, account: str) -> float | None:
        value = self._account(account).get("energy_total")
        return float(value) if value is not None else None

    def energy_total_at(self, account: str, when: dt.datetime) -> float | None:
        """Estimate today's cumulative value from the latest complete day.

        The API reports complete daily totals. The exposed sensor advances that
        latest total linearly during the following day while keeping the ledger
        itself authoritative for Recorder corrections.
        """
        total = self.energy_total(account)
        if total is None:
            return None
        realtime = self._account(account).get("realtime", {})
        if not realtime:
            return total
        latest_day = max(realtime)
        # Billing-only days are deliberately excluded from energy_total. Only
        # interpolate a day whose realtime contribution is in the ledger total.
        latest_value = self._account(account).get("counted_realtime", {}).get(latest_day)
        if latest_value is None:
            return total
        latest_value = float(latest_value)
        local = when.astimezone(_CSG_TIME_ZONE)
        if local.date() != dt.date.fromisoformat(latest_day) + dt.timedelta(days=1):
            return total
        fraction = (
            local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6
        ) / 86400
        return max(0.0, total - latest_value + latest_value * fraction)

    def _account(self, account: str) -> dict[str, Any]:
        return self._data["accounts"].setdefault(account, {})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up CSG sensors."""
    if not entry.data[CONF_ELE_ACCOUNTS]:
        return
    ledger = EnergyLedger(hass, entry.entry_id)
    await ledger.async_load()
    realtime = RealtimeCoordinator(hass, entry, ledger)
    current = CurrentCoordinator(hass, entry, ledger)
    billing = BillingCoordinator(hass, entry, ledger)
    await realtime.async_refresh()
    await current.async_refresh()
    await billing.async_refresh()
    entities: list[CSGSensor] = []
    for account in entry.data[CONF_ELE_ACCOUNTS]:
        entities.extend(
            [CSGSensor(realtime, account, ENERGY_TOTAL), CSGSensor(billing, account, SETTLED_COST_TOTAL)]
        )
        entities.extend(CSGSensor(realtime, account, description) for description in REALTIME_DESCRIPTIONS)
        entities.extend(CSGSensor(current, account, description) for description in CURRENT_DESCRIPTIONS)
        entities.extend(CSGSensor(billing, account, description) for description in BILLING_DESCRIPTIONS)
    async_add_entities(entities)


class CSGSensor(CoordinatorEntity, SensorEntity):
    """A sensor backed by a CSG data coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DataUpdateCoordinator, account: str, description: SensorDescription) -> None:
        super().__init__(coordinator)
        self._account = account
        self._description = description
        self._attr_unique_id = f"{DOMAIN}.{account}.{description.suffix}"
        self._attr_translation_key = description.translation_key
        self._attr_translation_placeholders = {"account": account}
        self._attr_native_unit_of_measurement = description.unit
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class
        self._attr_icon = description.icon
        self._attributes_key = description.attributes_key
        self._value_present = False
        self._unsub_interpolation = None
        self._update_from_coordinator()

    async def async_added_to_hass(self) -> None:
        """Refresh interpolated energy state between cloud polls."""
        await super().async_added_to_hass()
        if self._description.suffix == SUFFIX_ENERGY_TOTAL:
            self._unsub_interpolation = async_track_time_interval(
                self.hass, self._handle_interpolation_tick, timedelta(minutes=5)
            )

    async def async_will_remove_from_hass(self) -> None:
        """Stop the interpolation timer when the entity is removed."""
        if self._unsub_interpolation:
            self._unsub_interpolation()
            self._unsub_interpolation = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_interpolation_tick(self, _now: dt.datetime) -> None:
        """Write the estimated cumulative value without making an API call."""
        if self._description.suffix == SUFFIX_ENERGY_TOTAL:
            self._update_from_coordinator()
            self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._account)},
            name=f"CSGAccount-{self._account}",
            manufacturer="CSG",
            model="CSG Virtual Electricity Meter",
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_from_coordinator()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return whether this sensor has a current value."""
        return super().available and self._value_present

    def _update_from_coordinator(self) -> None:
        """Synchronize the cached state with the coordinator's latest data."""
        value = (self.coordinator.data or {}).get(self._account, {}).get(self._description.suffix)
        self._value_present = value is not None and value != STATE_UNAVAILABLE
        if self._value_present:
            if self._description.suffix == SUFFIX_ENERGY_TOTAL:
                ledger = getattr(self.coordinator, "ledger", None)
                if ledger is not None:
                    value = ledger.energy_total_at(self._account, dt_util.utcnow())
            self._attr_native_value = value
            self._attr_extra_state_attributes = (
                self.coordinator.data[self._account].get(self._attributes_key, {})
                if self._attributes_key
                else {}
            )
        else:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}


class CSGCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Shared CSG coordinator functions."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, ledger: EnergyLedger, name: str) -> None:
        self.entry = entry
        self.ledger = ledger
        super().__init__(
            hass,
            _LOGGER,
            name=name,
            update_interval=timedelta(
                seconds=entry.data[CONF_SETTINGS][CONF_UPDATE_INTERVAL]
            ),
        )

    async def _client(self) -> CSGClient:
        try:
            client = await self.hass.async_add_executor_job(
                CSGClient.load, {CONF_AUTH_TOKEN: self.entry.data[CONF_AUTH_TOKEN]}
            )
            if not await self.hass.async_add_executor_job(client.verify_login):
                raise ConfigEntryAuthFailed("Login expired")
            await self.hass.async_add_executor_job(client.initialize)
        except ConfigEntryAuthFailed:
            raise
        except FETCH_EXCEPTIONS as err:
            self._notify_failure("all", "connection", err)
            raise UpdateFailed(f"Unable to initialize CSG client: {err}") from err
        self._clear_failure("all", "connection")
        return client

    async def _fetch(self, function: Any, *args: Any) -> Any:
        async with asyncio.timeout(SETTING_UPDATE_TIMEOUT):
            return await self.hass.async_add_executor_job(function, *args)

    def _accounts(self) -> Iterable[CSGElectricityAccount]:
        return (CSGElectricityAccount.load(value) for value in self.entry.data[CONF_ELE_ACCOUNTS].values())

    def _notify_failure(self, account: str, kind: str, err: Exception) -> None:
        """Make transient cloud failures visible without discarding all entities."""
        persistent_notification.async_create(
            self.hass,
            f"CSG {kind} request for account {account} failed: {err}",
            title="China Southern Power Grid update failed",
            notification_id=f"{DOMAIN}_{self.entry.entry_id}_{kind}_{account}",
        )

    def _clear_failure(self, account: str, kind: str) -> None:
        persistent_notification.async_dismiss(
            self.hass,
            f"{DOMAIN}_{self.entry.entry_id}_{kind}_{account}",
        )


class RealtimeCoordinator(CSGCoordinator):
    """Fetch current-state and yesterday-use data without billing latency."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, ledger: EnergyLedger) -> None:
        super().__init__(hass, entry, ledger, f"CSG realtime {entry.data[CONF_USERNAME]}")

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        client = await self._client()
        data: dict[str, dict[str, Any]] = {}
        yesterday = (_csg_today() - dt.timedelta(days=1)).isoformat()
        for account in self._accounts():
            account_data: dict[str, Any] = {}
            try:
                balance, arrears = await self._fetch(client.get_balance_and_arrears, account)
                account_data.update({SUFFIX_BAL: balance, SUFFIX_ARR: arrears})
                self._clear_failure(account.account_number, "balance")
            except FETCH_EXCEPTIONS as err:
                _LOGGER.warning("Could not update balance for %s: %s", account.account_number, err)
                account_data.update({SUFFIX_BAL: STATE_UNAVAILABLE, SUFFIX_ARR: STATE_UNAVAILABLE})
                self._notify_failure(account.account_number, "balance", err)
            try:
                usage = await self._fetch(client.get_yesterday_kwh, account)
                if usage is None:
                    raise ValueError("Yesterday usage is empty")
                account_data[SUFFIX_YESTERDAY_KWH] = usage
                account_data[SUFFIX_ENERGY_TOTAL] = await self.ledger.async_record_realtime(account.account_number, yesterday, usage)
                self._clear_failure(account.account_number, "usage")
            except FETCH_EXCEPTIONS as err:
                _LOGGER.warning("Could not update yesterday usage for %s: %s", account.account_number, err)
                account_data[SUFFIX_YESTERDAY_KWH] = STATE_UNAVAILABLE
                energy_total = self.ledger.energy_total(account.account_number)
                account_data[SUFFIX_ENERGY_TOTAL] = (
                    energy_total if energy_total is not None else STATE_UNAVAILABLE
                )
                self._notify_failure(account.account_number, "usage", err)
            data[account.account_number] = account_data
        return data


class CurrentCoordinator(CSGCoordinator):
    """Fetch current ladder state separately from delayed bill data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, ledger: EnergyLedger) -> None:
        super().__init__(hass, entry, ledger, f"CSG current {entry.data[CONF_USERNAME]}")

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        client = await self._client()
        today = _csg_today()
        data: dict[str, dict[str, Any]] = {}
        for account in self._accounts():
            try:
                _, _, ladder, _ = await self._fetch(
                    client.get_month_daily_cost_detail,
                    account,
                    (today.year, today.month),
                )
                data[account.account_number] = _ladder_data(ladder)
                self._clear_failure(account.account_number, "ladder")
            except FETCH_EXCEPTIONS as err:
                _LOGGER.warning("Could not update ladder for %s: %s", account.account_number, err)
                data[account.account_number] = {
                    suffix: STATE_UNAVAILABLE
                    for suffix in (
                        SUFFIX_CURRENT_LADDER,
                        SUFFIX_CURRENT_LADDER_REMAINING_KWH,
                        SUFFIX_CURRENT_LADDER_TARIFF,
                    )
                }
                self._notify_failure(account.account_number, "ladder", err)
        return data


class BillingCoordinator(CSGCoordinator):
    """Fetch delayed bill data and import corrections into Recorder statistics."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, ledger: EnergyLedger) -> None:
        super().__init__(hass, entry, ledger, f"CSG billing {entry.data[CONF_USERNAME]}")
        self.update_interval = timedelta(days=1)

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        client = await self._client()
        now = _csg_today()
        previous = now.replace(day=1) - dt.timedelta(days=1)
        months = [(now.year, now.month), (previous.year, previous.month)]
        data: dict[str, dict[str, Any]] = {}
        for account in self._accounts():
            account_data = await self._update_account(client, account, months)
            data[account.account_number] = account_data
        return data

    async def _update_account(self, client: CSGClient, account: CSGElectricityAccount, months: list[tuple[int, int]]) -> dict[str, Any]:
        data: dict[str, Any] = {}
        daily: list[dict[str, float | str]] = []
        current_month = None
        last_month = None
        billing_failed = False
        for year, month in months:
            try:
                usage_total, usage_days = await self._fetch(client.get_month_daily_usage_detail, account, (year, month))
                cost_total, _, ladder, cost_days = await self._fetch(client.get_month_daily_cost_detail, account, (year, month))
                merged = _merge_daily_days(usage_days, cost_days)
                daily.extend(merged)
                values = (usage_total, cost_total, ladder, merged)
                if (year, month) == months[0]:
                    current_month = values
                else:
                    last_month = values
            except FETCH_EXCEPTIONS as err:
                _LOGGER.warning("Could not update billing for %s/%s-%02d: %s", account.account_number, year, month, err)
                billing_failed = True
                self._notify_failure(account.account_number, "billing", err)
        if not billing_failed:
            self._clear_failure(account.account_number, "billing")
        has_current_settlement_day = False
        if current_month:
            usage_total, cost_total, ladder, current_days = current_month
            data.update({SUFFIX_THIS_MONTH_KWH: usage_total, SUFFIX_THIS_MONTH_COST: cost_total, ATTR_KEY_MONTH_BILLING_DELAY: {ATTR_KEY_MONTH_BILLING_DELAY: _BILLING_DELAY}})
            _set_latest_day(data, current_days)
            has_current_settlement_day = bool(current_days)
        else:
            data.update({suffix: STATE_UNAVAILABLE for suffix in (SUFFIX_THIS_MONTH_KWH, SUFFIX_THIS_MONTH_COST, SUFFIX_LATEST_DAY_KWH, SUFFIX_LATEST_DAY_COST)})
        if last_month:
            data[SUFFIX_LAST_MONTH_KWH], data[SUFFIX_LAST_MONTH_COST] = last_month[:2]
            if not has_current_settlement_day:
                _set_latest_day(data, last_month[3])
        else:
            data.update({SUFFIX_LAST_MONTH_KWH: STATE_UNAVAILABLE, SUFFIX_LAST_MONTH_COST: STATE_UNAVAILABLE})
        total_cost, changed_days = await self.ledger.async_record_billing(
            account.account_number, daily
        )
        if daily:
            data[SUFFIX_SETTLED_COST_TOTAL] = total_cost
        else:
            data[SUFFIX_SETTLED_COST_TOTAL] = STATE_UNAVAILABLE
        # Corrections are independent of whether this refresh returned rows.
        acknowledgements = await self._async_correct_statistics(
            account.account_number, changed_days
        )
        if acknowledgements:
            await self.ledger.async_acknowledge_corrections(
                account.account_number, acknowledgements
            )
        await self._add_year_data(client, account, data)
        return data

    async def _add_year_data(self, client: CSGClient, account: CSGElectricityAccount, data: dict[str, Any]) -> None:
        now = _csg_today()
        for year, usage_suffix, cost_suffix in ((now.year, SUFFIX_THIS_YEAR_KWH, SUFFIX_THIS_YEAR_COST), (now.year - 1, SUFFIX_LAST_YEAR_KWH, SUFFIX_LAST_YEAR_COST)):
            try:
                cost, usage, _ = await self._fetch(client.get_year_month_stats, account, year)
                data[usage_suffix] = usage
                data[cost_suffix] = cost
                if year == now.year:
                    billing_through = now.replace(day=1) - dt.timedelta(days=1)
                    data[ATTR_KEY_YEAR_BILLING_DELAY] = {
                        ATTR_KEY_YEAR_BILLING_DELAY: billing_through.strftime("%Y-%m")
                    }
            except FETCH_EXCEPTIONS as err:
                _LOGGER.warning("Could not update year billing for %s/%s: %s", account.account_number, year, err)
                data[usage_suffix] = STATE_UNAVAILABLE
                data[cost_suffix] = STATE_UNAVAILABLE

    async def _async_correct_statistics(
        self,
        account: str,
        changed_days: dict[str, tuple[dict[str, float], dict[str, float]]],
    ) -> dict[str, set[str]]:
        """Adjust corrected daily energy and cost sums through Recorder."""
        if not changed_days:
            return {}
        try:
            from homeassistant.components.recorder import get_instance
        except ImportError:
            _LOGGER.warning("Recorder external statistics API unavailable; skipped bill correction")
            return {}
        try:
            statistics = (
                (self._statistic_id(account, SUFFIX_ENERGY_TOTAL), WF_ATTR_KWH, "kWh"),
                (self._statistic_id(account, SUFFIX_SETTLED_COST_TOTAL), WF_ATTR_CHARGE, "CNY"),
            )
        except ValueError as err:
            _LOGGER.warning("Skipped bill correction: %s", err)
            return {}
        acknowledgements: dict[str, set[str]] = {}
        for day, (previous, current) in changed_days.items():
            start = dt_util.start_of_local_day(dt.date.fromisoformat(day))
            for statistic_id, key, unit in statistics:
                if key not in previous:
                    continue
                adjustment = current.get(key, 0) - previous[key]
                if adjustment:
                    try:
                        get_instance(self.hass).async_adjust_statistics(
                            statistic_id, start, adjustment, unit
                        )
                    except Exception:  # Keep this statistic's correction pending.
                        _LOGGER.exception("Could not correct bill statistic %s", statistic_id)
                        continue
                acknowledgements.setdefault(day, set()).add(key)
        return acknowledgements

    def _statistic_id(self, account: str, suffix: str) -> str:
        """Return the Recorder statistic ID after any user entity-ID rename."""
        unique_id = f"{DOMAIN}.{account}.{suffix}"
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is None:
            raise ValueError(f"Entity registry has no entity for {unique_id}")
        return entity_id


def _merge_daily_days(usage_days: list[dict[str, Any]], cost_days: list[dict[str, Any]]) -> list[dict[str, float | str]]:
    """Merge bill responses by date without assuming equal response lengths."""
    days: dict[str, dict[str, float | str]] = {item[WF_ATTR_DATE]: dict(item) for item in usage_days}
    for item in cost_days:
        day = item[WF_ATTR_DATE]
        target = days.setdefault(day, {WF_ATTR_DATE: day})
        if WF_ATTR_KWH not in target and WF_ATTR_KWH in item:
            target[WF_ATTR_KWH] = item[WF_ATTR_KWH]
        if WF_ATTR_CHARGE in item:
            target[WF_ATTR_CHARGE] = item[WF_ATTR_CHARGE]
    return [days[day] for day in sorted(days)]


def _csg_today() -> dt.date:
    """Return the current calendar date used by the CSG API."""
    return dt_util.utcnow().astimezone(_CSG_TIME_ZONE).date()


def _ladder_data(ladder: dict[str, Any]) -> dict[str, Any]:
    return {
        SUFFIX_CURRENT_LADDER: ladder.get(WF_ATTR_LADDER, STATE_UNAVAILABLE),
        SUFFIX_CURRENT_LADDER_REMAINING_KWH: ladder.get(WF_ATTR_LADDER_REMAINING_KWH, STATE_UNAVAILABLE),
        SUFFIX_CURRENT_LADDER_TARIFF: ladder.get(WF_ATTR_LADDER_TARIFF, STATE_UNAVAILABLE),
        ATTR_KEY_CURRENT_LADDER_START_DATE: {ATTR_KEY_CURRENT_LADDER_START_DATE: ladder.get(WF_ATTR_LADDER_START_DATE)},
    }


def _set_latest_day(data: dict[str, Any], days: list[dict[str, float | str]]) -> None:
    if not days:
        data[SUFFIX_LATEST_DAY_KWH] = STATE_UNAVAILABLE
        data[SUFFIX_LATEST_DAY_COST] = STATE_UNAVAILABLE
        return
    latest = days[-1]
    data[SUFFIX_LATEST_DAY_KWH] = latest.get(WF_ATTR_KWH, STATE_UNAVAILABLE)
    data[SUFFIX_LATEST_DAY_COST] = latest.get(WF_ATTR_CHARGE, STATE_UNAVAILABLE)
    data[ATTR_KEY_SETTLEMENT_DATE] = {ATTR_KEY_SETTLEMENT_DATE: latest[WF_ATTR_DATE]}
