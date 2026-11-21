import asyncio
import logging
from collections import Counter
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol, cast
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_API_TOKEN, CONF_TOKEN, STATE_UNAVAILABLE, Platform
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.solis_cloud_control import (
    async_migrate_entry,
    async_remove_config_entry_device,
    async_setup_entry,
)
from custom_components.solis_cloud_control.api.solis_api import SolisCloudControlApiError
from custom_components.solis_cloud_control.const import CONF_INVERTER_SN, DOMAIN
from custom_components.solis_cloud_control.inverters.inverter import Inverter, InverterInfo
from tests.helpers import ensure, get_state

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from custom_components.solis_cloud_control.data import SolisCloudControlConfigEntry


class CreateConfigEntry(Protocol):
    def __call__(self, *, version: int, data: dict[str, str]) -> MockConfigEntry: ...


async def test_async_setup_entry(
    hass: HomeAssistant,
    mock_api_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    any_inverter: Inverter,
    integration_patches: None,
) -> None:
    read_batch_cids = dict.fromkeys(any_inverter.read_batch_cids, "0")

    for cid in [ensure(any_inverter.on_off).on_cid, ensure(any_inverter.on_off).off_cid]:
        read_batch_cids[cid] = "190"

    for cid in [
        ensure(any_inverter.charge_discharge_slots).charge_slot1.time_cid,
        ensure(any_inverter.charge_discharge_slots).charge_slot2.time_cid,
        ensure(any_inverter.charge_discharge_slots).charge_slot3.time_cid,
        ensure(any_inverter.charge_discharge_slots).charge_slot4.time_cid,
        ensure(any_inverter.charge_discharge_slots).charge_slot5.time_cid,
        ensure(any_inverter.charge_discharge_slots).charge_slot6.time_cid,
        ensure(any_inverter.charge_discharge_slots).discharge_slot1.time_cid,
        ensure(any_inverter.charge_discharge_slots).discharge_slot2.time_cid,
        ensure(any_inverter.charge_discharge_slots).discharge_slot3.time_cid,
        ensure(any_inverter.charge_discharge_slots).discharge_slot4.time_cid,
        ensure(any_inverter.charge_discharge_slots).discharge_slot5.time_cid,
        ensure(any_inverter.charge_discharge_slots).discharge_slot6.time_cid,
    ]:
        read_batch_cids[cid] = "00:00-00:00"

    mock_api_client.read_batch.return_value = read_batch_cids

    charge_discharge_settings = "0,0,00:00,00:00,00:00,00:00,0,0,00:00,00:00,00:00,00:00,0,0,00:00,00:00,00:00,00:00"

    mock_api_client.read.side_effect = lambda inverter_sn, cid, max_retry_time: (  # noqa: ARG005
        charge_discharge_settings if cid == 103 else None
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # check if the config entry is loaded
    assert mock_config_entry.state is ConfigEntryState.LOADED

    # coordinator.async_config_entry_first_refresh()
    assert mock_api_client.read_batch.called

    assert hasattr(mock_config_entry, "runtime_data")
    assert mock_config_entry.runtime_data.coordinator is not None
    assert mock_config_entry.runtime_data.inverter is not None

    # check device registration
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, any_inverter.info.serial_number), mock_config_entry.entry_id
    )

    assert device is not None
    assert device.manufacturer == "Solis"
    assert device.name == f"Inverter Control {any_inverter.info.serial_number}"
    assert device.model_id == any_inverter.info.model
    assert device.model == any_inverter.info.machine
    assert device.sw_version == any_inverter.info.version

    # check entity registration
    entity_registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(entity_registry, mock_config_entry.entry_id)

    platform_counts = Counter(entry.domain for entry in entries)
    assert platform_counts[Platform.DATETIME] == 1
    assert platform_counts[Platform.NUMBER] == 45
    assert platform_counts[Platform.SELECT] == 1
    assert platform_counts[Platform.SENSOR] == 7
    assert platform_counts[Platform.SWITCH] == 19
    assert platform_counts[Platform.TEXT] == 18

    platform_disabled_counts = Counter(entry.domain for entry in entries if entry.disabled_by is not None)
    assert platform_disabled_counts[Platform.DATETIME] == 0
    assert platform_disabled_counts[Platform.NUMBER] == 0
    assert platform_disabled_counts[Platform.SELECT] == 0
    assert platform_disabled_counts[Platform.SENSOR] == 7
    assert platform_disabled_counts[Platform.SWITCH] == 0
    assert platform_disabled_counts[Platform.TEXT] == 0


async def test_async_setup_entry_undefined_inverter(
    hass: HomeAssistant, mock_api_client: AsyncMock, mock_config_entry: MockConfigEntry, any_inverter_info: InverterInfo
) -> None:
    undefined_inverter = Inverter(info=any_inverter_info)

    with (
        patch(
            "custom_components.solis_cloud_control._create_api_client",
            return_value=mock_api_client,
        ),
        patch(
            "custom_components.solis_cloud_control.create_inverter_info",
            return_value=undefined_inverter.info,
        ),
        patch(
            "custom_components.solis_cloud_control.create_inverter",
            return_value=undefined_inverter,
        ),
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    # check entity registration
    entity_registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(entity_registry, mock_config_entry.entry_id)
    assert len(entries) == 0


async def test_async_setup_entry_inverter_info_api_error(
    hass: HomeAssistant,
    mock_api_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    with (
        patch(
            "custom_components.solis_cloud_control._create_api_client",
            return_value=mock_api_client,
        ),
        patch(
            "custom_components.solis_cloud_control.create_inverter_info",
            side_effect=SolisCloudControlApiError("any error"),
        ),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, cast("SolisCloudControlConfigEntry", mock_config_entry))


@pytest.mark.parametrize("offline_at", ["inverter_details", "read_batch"])
async def test_offline_startup_retries_and_recovers(
    hass: HomeAssistant, mock_api_client: AsyncMock, mock_config_entry: MockConfigEntry, offline_at: str
) -> None:
    mock_api_client.inverter_details.return_value = {"energyStorageControl": "0"}
    mock_api_client.read_batch.return_value = {15: "110"}
    offline_endpoint = getattr(mock_api_client, offline_at)
    offline_endpoint.side_effect = SolisCloudControlApiError("The device is offline", response_code="B0072")

    with patch("custom_components.solis_cloud_control._create_api_client", return_value=mock_api_client):
        assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
        assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
        offline_endpoint.assert_awaited_once()

        offline_endpoint.side_effect = None
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
        await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    mock_api_client.control.assert_not_awaited()


async def test_offline_polling_recovers_without_replaying_control(
    hass: HomeAssistant,
    mock_api_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_api_client.inverter_details.return_value = {"energyStorageControl": "0", "model": "0101"}
    mock_api_client.read_batch.return_value = {15: "110"}
    caplog.set_level(logging.WARNING, logger="custom_components.solis_cloud_control")
    with patch("custom_components.solis_cloud_control._create_api_client", return_value=mock_api_client):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        unique_id = f"{mock_config_entry.entry_id}_power_limit"
        entity_id = ensure(registry.async_get_entity_id(Platform.NUMBER, DOMAIN, unique_id))
        assert float(get_state(hass, entity_id).state) == 110
        coordinator = mock_config_entry.runtime_data.coordinator

        offline = SolisCloudControlApiError("The device is offline", response_code="B0072")
        mock_api_client.control.side_effect = offline
        with pytest.raises(HomeAssistantError, match="offline"):
            await hass.services.async_call(
                Platform.NUMBER, "set_value", {"entity_id": entity_id, "value": 0}, blocking=True
            )
        assert get_state(hass, entity_id).state == STATE_UNAVAILABLE
        with pytest.raises(HomeAssistantError, match="offline"):
            await coordinator.control(15, "0")

        mock_api_client.read_batch.side_effect = offline
        mock_api_client.read_batch.reset_mock()
        caplog.clear()
        for _ in range(24):
            await coordinator.async_refresh()
        assert mock_api_client.read_batch.await_count == 24
        mock_api_client.read_batch.assert_awaited_with(
            "any_inverter_sn", mock_config_entry.runtime_data.inverter.read_batch_cids, max_retry_time=0
        )
        mock_api_client.inverter_details.assert_awaited_once()
        assert not caplog.records

        mock_api_client.read_batch.side_effect = None
        mock_api_client.read_batch.return_value = {15: "55"}
        await coordinator.async_refresh()
        assert float(get_state(hass, entity_id).state) == 55
        assert mock_api_client.inverter_details.await_count == 2
        mock_api_client.control.assert_awaited_once()

        mock_api_client.inverter_details.return_value = {"energyStorageControl": "0", "model": "0201"}
        mock_api_client.read_batch.side_effect = offline
        await coordinator.async_refresh()
        assert get_state(hass, entity_id).state == STATE_UNAVAILABLE
        mock_api_client.read_batch.side_effect = None
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert mock_config_entry.runtime_data.coordinator is not coordinator
        assert float(get_state(hass, entity_id).state) == 55
        assert registry.async_get_entity_id(Platform.NUMBER, DOMAIN, unique_id) == entity_id
        device = dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, "any_inverter_sn"), mock_config_entry.entry_id
        )
        assert ensure(device).model_id == "0201"
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get_entity_id(Platform.NUMBER, DOMAIN, unique_id) == entity_id
        mock_api_client.control.assert_awaited_once()


@pytest.mark.parametrize(
    "code,error_type",
    [("Z0001", ConfigEntryAuthFailed), ("B0115", ConfigEntryError)],
)
async def test_setup_reports_actionable_api_errors(
    hass: HomeAssistant,
    mock_api_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    code: str,
    error_type: type[Exception],
) -> None:
    mock_api_client.inverter_details.side_effect = SolisCloudControlApiError("API error", response_code=code)
    with (
        patch("custom_components.solis_cloud_control._create_api_client", return_value=mock_api_client),
        pytest.raises(error_type, match="Check"),
    ):
        await async_setup_entry(hass, cast("SolisCloudControlConfigEntry", mock_config_entry))


@pytest.mark.parametrize("rate_limited_at", ["inverter_details", "read_batch"])
async def test_setup_honors_rate_limit_across_retries(
    hass: HomeAssistant, mock_api_client: AsyncMock, mock_config_entry: MockConfigEntry, rate_limited_at: str
) -> None:
    mock_api_client.inverter_details.return_value = {"energyStorageControl": "0"}
    limited_endpoint = getattr(mock_api_client, rate_limited_at)
    limited_endpoint.side_effect = SolisCloudControlApiError("Too many requests", status_code=429, retry_after=600)

    with patch("custom_components.solis_cloud_control._create_api_client", return_value=mock_api_client):
        assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
        assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
        with pytest.raises(ConfigEntryNotReady, match="rate limit"):
            await async_setup_entry(hass, cast("SolisCloudControlConfigEntry", mock_config_entry))

    limited_endpoint.assert_awaited_once()
    await hass.config_entries.async_remove(mock_config_entry.entry_id)


async def test_setup_deadline_cancels_slow_request(
    hass: HomeAssistant, mock_api_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    cancelled = asyncio.Event()

    async def slow_request(*_args: object, **_kwargs: object) -> None:
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    mock_api_client.inverter_details.side_effect = slow_request
    with (
        patch("custom_components.solis_cloud_control._create_api_client", return_value=mock_api_client),
        patch("custom_components.solis_cloud_control._SETUP_TIMEOUT_SECONDS", 0),
        pytest.raises(ConfigEntryNotReady, match="Timed out"),
    ):
        await async_setup_entry(hass, cast("SolisCloudControlConfigEntry", mock_config_entry))
    assert cancelled.is_set()
    mock_api_client.read_batch.assert_not_awaited()


async def test_unload_cancels_pending_poll(
    hass: HomeAssistant,
    mock_api_client: AsyncMock,
    setup_integration: MockConfigEntry,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending_retry(*_args: object, **_kwargs: object) -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    mock_api_client.read_batch.side_effect = pending_retry
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
    async with asyncio.timeout(1):
        await started.wait()

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    assert cancelled.is_set()
    calls_before = mock_api_client.read_batch.await_count
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=1))
    await hass.async_block_till_done()
    assert mock_api_client.read_batch.await_count == calls_before


@pytest.fixture
def create_config_entry(hass: HomeAssistant) -> CreateConfigEntry:
    def _create(version: int, data: dict[str, str]) -> MockConfigEntry:
        config_entry = MockConfigEntry(domain=DOMAIN, version=version, data=data)
        config_entry.add_to_hass(hass)
        return config_entry

    return _create


async def test_migrate_config_entry_v1(hass: HomeAssistant, create_config_entry: CreateConfigEntry) -> None:
    config_entry = create_config_entry(
        version=1,
        data={
            CONF_API_KEY: "any api key",
            CONF_TOKEN: "any api token",
            CONF_INVERTER_SN: "any inverter sn",
        },
    )

    assert await async_migrate_entry(hass, cast("SolisCloudControlConfigEntry", config_entry))

    assert config_entry.version == 3
    assert dict(config_entry.data) == {
        CONF_API_KEY: "any api key",
        CONF_API_TOKEN: "any api token",
        CONF_INVERTER_SN: "any inverter sn",
    }


async def test_migrate_config_entry_v2_removes_grid_peak_shaving_switch(
    hass: HomeAssistant, create_config_entry: CreateConfigEntry
) -> None:
    config_entry = create_config_entry(
        version=2,
        data={
            CONF_API_KEY: "any api key",
            CONF_API_TOKEN: "any api token",
            CONF_INVERTER_SN: "any inverter sn",
        },
    )

    entity_registry = er.async_get(hass)
    grid_peak_shaving_unique_id = f"{config_entry.entry_id}_grid_peak_shaving"
    battery_reserve_unique_id = f"{config_entry.entry_id}_battery_reserve"
    for unique_id in (grid_peak_shaving_unique_id, battery_reserve_unique_id):
        entity_registry.async_get_or_create(
            Platform.SWITCH,
            DOMAIN,
            unique_id,
            config_entry=config_entry,
        )

    assert await async_migrate_entry(hass, cast("SolisCloudControlConfigEntry", config_entry))

    assert config_entry.version == 3
    assert entity_registry.async_get_entity_id(Platform.SWITCH, DOMAIN, grid_peak_shaving_unique_id) is None
    assert entity_registry.async_get_entity_id(Platform.SWITCH, DOMAIN, battery_reserve_unique_id) is not None


async def test_migrate_config_entry_v2_without_grid_peak_shaving_switch(
    hass: HomeAssistant, create_config_entry: CreateConfigEntry
) -> None:
    config_entry = create_config_entry(
        version=2,
        data={
            CONF_API_KEY: "any api key",
            CONF_API_TOKEN: "any api token",
            CONF_INVERTER_SN: "any inverter sn",
        },
    )

    assert await async_migrate_entry(hass, cast("SolisCloudControlConfigEntry", config_entry))

    assert config_entry.version == 3


async def test_migrate_config_entry_current_version(
    hass: HomeAssistant, create_config_entry: CreateConfigEntry
) -> None:
    config_entry = create_config_entry(
        version=3,
        data={
            CONF_API_KEY: "any api key",
            CONF_API_TOKEN: "any api token",
            CONF_INVERTER_SN: "any inverter sn",
        },
    )

    assert await async_migrate_entry(hass, cast("SolisCloudControlConfigEntry", config_entry))

    assert config_entry.version == 3
    assert dict(config_entry.data) == {
        CONF_API_KEY: "any api key",
        CONF_API_TOKEN: "any api token",
        CONF_INVERTER_SN: "any inverter sn",
    }


async def test_remove_config_entry_device(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    result = await async_remove_config_entry_device(
        hass,
        cast("SolisCloudControlConfigEntry", mock_config_entry),
        dr.DeviceEntry(config_entry_id=mock_config_entry.entry_id),
    )
    assert result is True
