from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.solis_cloud_control.api.solis_api import SolisCloudControlApiError
from custom_components.solis_cloud_control.coordinator import SolisCloudControlCoordinator, SolisCloudControlData
from tests.helpers import assert_control

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.solis_cloud_control.inverters.inverter import Inverter


@pytest.fixture
def coordinator(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_api_client: AsyncMock, any_inverter: Inverter
) -> SolisCloudControlCoordinator:
    return SolisCloudControlCoordinator(
        hass=hass,
        config_entry=mock_config_entry,
        api_client=mock_api_client,
        inverter=any_inverter,
    )


async def test_async_update_data(
    hass: HomeAssistant, coordinator: SolisCloudControlCoordinator, mock_api_client: AsyncMock, any_inverter: Inverter
) -> None:
    read_batch_cids = any_inverter.read_batch_cids
    read_cids = any_inverter.read_cids

    read_batch_data = {cid: f"value_{cid}" for cid in read_batch_cids}
    mock_api_client.read_batch.return_value = read_batch_data

    for read_cid in read_cids:
        mock_api_client.read.side_effect = lambda serial_number, read_cid, **kwargs: f"value_{read_cid}"  # noqa: ARG005

    data = await coordinator._async_update_data()

    mock_api_client.read_batch.assert_called_once_with(
        any_inverter.info.serial_number,
        read_batch_cids,
        max_retry_time=0,
    )

    for read_cid in read_cids:
        mock_api_client.read.assert_called_once_with(
            any_inverter.info.serial_number,
            read_cid,
            max_retry_time=0,
        )

    assert data == {cid: f"value_{cid}" for cid in any_inverter.all_cids}


async def test_async_update_data_api_error(
    hass: HomeAssistant, coordinator: SolisCloudControlCoordinator, mock_api_client: AsyncMock
) -> None:
    any_error = "any error"
    mock_api_client.read_batch.side_effect = SolisCloudControlApiError(any_error)

    with pytest.raises(UpdateFailed) as exc_info:
        await coordinator._async_update_data()

    assert str(exc_info.value) == any_error

    mock_api_client.read_batch.assert_called_once_with(
        coordinator._inverter.info.serial_number,
        coordinator._inverter.read_batch_cids,
        max_retry_time=0,
    )


async def test_offline_poll_checks_the_single_register_that_failed(
    coordinator: SolisCloudControlCoordinator, mock_api_client: AsyncMock, any_inverter: Inverter
) -> None:
    coordinator.data = SolisCloudControlData()
    mock_api_client.read_batch.return_value = {}
    mock_api_client.read.side_effect = SolisCloudControlApiError("The device is offline", response_code="B0072")

    assert await coordinator._async_update_data() == {}
    assert await coordinator._async_update_data() == {}

    mock_api_client.read_batch.assert_awaited_once()
    failed_cid = any_inverter.read_cids[0]
    mock_api_client.read.assert_awaited_with(any_inverter.info.serial_number, failed_cid, max_retry_time=0)
    assert mock_api_client.read.await_count == 2


@pytest.mark.parametrize("model_changed", [False, True])
async def test_offline_recovery_revalidates_after_batch_error(
    hass: HomeAssistant,
    coordinator: SolisCloudControlCoordinator,
    mock_api_client: AsyncMock,
    any_inverter: Inverter,
    model_changed: bool,
) -> None:
    coordinator.data = SolisCloudControlData()
    mock_api_client.read_batch.side_effect = SolisCloudControlApiError("Offline", response_code="B0072")
    assert await coordinator._async_update_data() == {}

    mock_api_client.read_batch.side_effect = SolisCloudControlApiError("Settings batch unavailable")
    info = replace(any_inverter.info, model="new") if model_changed else any_inverter.info
    with (
        patch("custom_components.solis_cloud_control.coordinator.create_inverter_info", return_value=info) as discover,
        patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
    ):
        if model_changed:
            assert await coordinator._async_update_data() == {}
            assert await coordinator._async_update_data() == {}
            reload_entry.assert_called_once()
        else:
            with pytest.raises(UpdateFailed, match="Settings batch unavailable"):
                await coordinator._async_update_data()
            reload_entry.assert_not_called()
        discover.assert_awaited_once()
    assert mock_api_client.read_batch.await_count == 2
    mock_api_client.control.assert_not_awaited()


@pytest.mark.parametrize(
    ("code", "error_type"),
    [("Z0001", ConfigEntryAuthFailed), ("B0115", ConfigEntryError)],
)
async def test_first_refresh_reports_configuration_errors(
    coordinator: SolisCloudControlCoordinator, mock_api_client: AsyncMock, code: str, error_type: type[Exception]
) -> None:
    mock_api_client.read_batch.side_effect = SolisCloudControlApiError("API error", response_code=code)
    with pytest.raises(error_type, match="Check"):
        await coordinator._async_update_data()


@pytest.mark.parametrize("rate_limited_at", ["read_batch", "control"])
async def test_rate_limit_blocks_additional_requests(
    coordinator: SolisCloudControlCoordinator, mock_api_client: AsyncMock, rate_limited_at: str
) -> None:
    coordinator.data = SolisCloudControlData({15: "110"})
    limited_endpoint = getattr(mock_api_client, rate_limited_at)
    limited_endpoint.side_effect = SolisCloudControlApiError("Too many requests", status_code=429, retry_after=600)

    if rate_limited_at == "control":
        with (
            patch.object(coordinator, "async_request_refresh", AsyncMock()),
            pytest.raises(HomeAssistantError, match="Too many requests"),
        ):
            await coordinator.control(15, "0")
    else:
        with pytest.raises(UpdateFailed, match="Too many requests") as error:
            await coordinator._async_update_data()
        assert error.value.retry_after == 600

    with pytest.raises(UpdateFailed, match="rate limit") as error:
        await coordinator._async_update_data()
    assert error.value.retry_after is not None and 0 < error.value.retry_after <= 600
    with pytest.raises(HomeAssistantError, match="rate limit"):
        await coordinator.control(15, "0")

    assert mock_api_client.read_batch.await_count == (1 if rate_limited_at == "read_batch" else 0)
    assert mock_api_client.control.await_count == (1 if rate_limited_at == "control" else 0)


async def test_control_success(
    hass: HomeAssistant, coordinator: SolisCloudControlCoordinator, mock_api_client: AsyncMock, any_inverter: Inverter
) -> None:
    any_cid = 123
    any_value = "any_value"
    any_old_value = "any_old_value"
    any_initial_value = "initial_value"

    coordinator.data = SolisCloudControlData({any_cid: any_initial_value})

    mock_api_client.control.return_value = None

    with (
        patch.object(coordinator, "async_request_refresh", AsyncMock()) as mock_request_refresh,
        patch.object(coordinator, "async_set_updated_data", Mock()) as mock_set_updated_data,
    ):
        await coordinator.control(any_cid, any_value, any_old_value)

        mock_set_updated_data.assert_called_once_with({any_cid: any_value})
        assert_control(mock_api_client, any_inverter, any_cid, any_value, any_old_value)
        mock_request_refresh.assert_called_once()


async def test_control_success_without_data(
    hass: HomeAssistant, coordinator: SolisCloudControlCoordinator, mock_api_client: AsyncMock, any_inverter: Inverter
) -> None:
    any_cid = 123
    any_value = "any_value"
    any_old_value = "any_old_value"

    mock_api_client.control.return_value = None

    with (
        patch.object(coordinator, "async_request_refresh", AsyncMock()) as mock_request_refresh,
        patch.object(coordinator, "async_set_updated_data", Mock()) as mock_set_updated_data,
    ):
        await coordinator.control(any_cid, any_value, any_old_value)

        mock_set_updated_data.assert_not_called()
        assert_control(mock_api_client, any_inverter, any_cid, any_value, any_old_value)
        mock_request_refresh.assert_called_once()


async def test_control_api_error(
    hass: HomeAssistant, coordinator: SolisCloudControlCoordinator, mock_api_client: AsyncMock
) -> None:
    any_cid = 123
    any_value = "any_value"
    any_old_value = "any_old_value"
    any_initial_value = "initial_value"
    any_error = "any error"

    coordinator.data = SolisCloudControlData({any_cid: any_initial_value})

    mock_api_client.control.side_effect = SolisCloudControlApiError(any_error)

    with (
        patch.object(coordinator, "async_request_refresh", AsyncMock()) as mock_request_refresh,
        patch.object(coordinator, "async_set_updated_data", Mock()) as mock_set_updated_data,
    ):
        with pytest.raises(HomeAssistantError, match=any_error):
            await coordinator.control(any_cid, any_value, any_old_value)

        mock_set_updated_data.assert_not_called()
        assert_control(mock_api_client, coordinator._inverter, any_cid, any_value, any_old_value)
        mock_request_refresh.assert_called_once()
