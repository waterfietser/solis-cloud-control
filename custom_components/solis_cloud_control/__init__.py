import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.const import CONF_API_KEY, CONF_API_TOKEN, Platform
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util.hass_dict import HassEntryKey

from custom_components.solis_cloud_control.api.solis_api import SolisCloudControlApiClient, SolisCloudControlApiError
from custom_components.solis_cloud_control.const import API_BASE_URL, CONF_INVERTER_SN, DOMAIN
from custom_components.solis_cloud_control.coordinator import SolisCloudControlCoordinator
from custom_components.solis_cloud_control.data import SolisCloudControlConfigEntry, SolisCloudControlData
from custom_components.solis_cloud_control.inverters.inverter_factory import create_inverter, create_inverter_info

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_SETUP_TIMEOUT_SECONDS = 10
_SETUP_RATE_LIMITS = HassEntryKey[float](f"{DOMAIN}.setup_rate_limits")
_PLATFORMS: list[Platform] = [
    Platform.DATETIME,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]


async def async_setup_entry(hass: HomeAssistant, config_entry: SolisCloudControlConfigEntry) -> bool:
    api_key = config_entry.data[CONF_API_KEY]
    api_token = config_entry.data[CONF_API_TOKEN]
    inverter_sn = config_entry.data[CONF_INVERTER_SN]
    rate_limits = hass.data.setdefault(_SETUP_RATE_LIMITS, {})
    if rate_limits.get(config_entry.entry_id, 0) > hass.loop.time():
        raise ConfigEntryNotReady("Waiting for the SolisCloud API rate limit to reset")
    rate_limits.pop(config_entry.entry_id, None)

    # create api client
    api_client = _create_api_client(hass, api_key, api_token)

    try:
        async with asyncio.timeout(_SETUP_TIMEOUT_SECONDS):
            inverter_info = await create_inverter_info(api_client, inverter_sn)
            inverter = create_inverter(inverter_info)
            coordinator = SolisCloudControlCoordinator(hass, config_entry, api_client, inverter)
            await coordinator.async_config_entry_first_refresh()
    except TimeoutError as error:
        raise ConfigEntryNotReady("Timed out reading inverter capabilities or settings from SolisCloud") from error
    except ConfigEntryNotReady as error:
        if isinstance(update_error := error.__cause__, UpdateFailed) and update_error.retry_after is not None:
            rate_limits[config_entry.entry_id] = hass.loop.time() + update_error.retry_after
        raise
    except SolisCloudControlApiError as error:
        if error.is_auth_error:
            raise ConfigEntryAuthFailed(f"Check the SolisCloud API key and token: {error}") from error
        if error.is_configuration_error:
            raise ConfigEntryError(f"Check the inverter and SolisCloud API configuration: {error}") from error
        if (retry_after := error.rate_limit_delay) is not None:
            rate_limits[config_entry.entry_id] = hass.loop.time() + retry_after
        raise ConfigEntryNotReady(f"Unable to read inverter info for '{inverter_sn}': {error}") from error

    # register the inverter in the device registry
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(config_entry.domain, inverter_sn)},
        manufacturer="Solis",
        name=f"Inverter Control {inverter_sn}",
        serial_number=inverter_sn,
        model_id=inverter_info.model or "Unknown",
        model=inverter_info.machine or "Unknown",
        sw_version=inverter_info.version or "Unknown",
    )

    # make coordinator available to integration
    config_entry.runtime_data = SolisCloudControlData(inverter, coordinator)

    # setup platforms, call async_setup for each entity
    await hass.config_entries.async_forward_entry_setups(config_entry, _PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: SolisCloudControlConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(config_entry, _PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, config_entry: SolisCloudControlConfigEntry) -> None:
    hass.data.setdefault(_SETUP_RATE_LIMITS, {}).pop(config_entry.entry_id, None)


async def async_migrate_entry(hass: HomeAssistant, config_entry: SolisCloudControlConfigEntry) -> bool:
    _LOGGER.debug("Migrating configuration from version %s", config_entry.version)

    version = config_entry.version

    if version == 1:
        from homeassistant.const import CONF_TOKEN

        new_data = {**config_entry.data}
        new_data[CONF_API_TOKEN] = new_data.pop(CONF_TOKEN)

        hass.config_entries.async_update_entry(config_entry, data=new_data, version=2)
        version = 2

    if version == 2:
        # Peak Shaving is now a base storage mode, so the standalone switch would conflict with mode selection
        entity_registry = er.async_get(hass)
        entity_id = entity_registry.async_get_entity_id(
            Platform.SWITCH, config_entry.domain, f"{config_entry.entry_id}_grid_peak_shaving"
        )
        if entity_id is not None:
            entity_registry.async_remove(entity_id)

        hass.config_entries.async_update_entry(config_entry, version=3)
        version = 3

    return version == 3


async def async_remove_config_entry_device(
    hass: HomeAssistant,  # noqa: ARG001
    config_entry: SolisCloudControlConfigEntry,  # noqa: ARG001
    device_entry: dr.DeviceEntry,  # noqa: ARG001
) -> bool:
    return True


def _create_api_client(hass: HomeAssistant, api_key: str, api_token: str) -> SolisCloudControlApiClient:
    session = aiohttp_client.async_get_clientsession(hass)
    return SolisCloudControlApiClient(API_BASE_URL, api_key, api_token, session)
