import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, HomeAssistantError
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from custom_components.solis_cloud_control.api.solis_api import SolisCloudControlApiClient, SolisCloudControlApiError
from custom_components.solis_cloud_control.inverters.inverter_factory import create_inverter_info

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from custom_components.solis_cloud_control.inverters.inverter import Inverter

_LOGGER = logging.getLogger(__name__)

_COORDINATOR_NAME = "Solis Cloud Control"

_UPDATE_INTERVAL = timedelta(minutes=5)

_REQUEST_REFRESH_COOLDOWN_SECONDS = 10

_UPDATE_RETRY_TIME_SECONDS = 45


class SolisCloudControlData(dict[int, str | None]):
    pass


class SolisCloudControlCoordinator(DataUpdateCoordinator[SolisCloudControlData]):
    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        api_client: SolisCloudControlApiClient,
        inverter: Inverter,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=_COORDINATOR_NAME,
            config_entry=config_entry,
            update_interval=_UPDATE_INTERVAL,
            request_refresh_debouncer=Debouncer(
                hass,
                _LOGGER,
                cooldown=_REQUEST_REFRESH_COOLDOWN_SECONDS,
                immediate=False,
            ),
        )
        self._api_client = api_client
        self._inverter = inverter
        self._offline = False
        self._offline_read_cid: int | None = None
        self._reload_pending = False
        self._rate_limited_until = 0.0

    async def _async_update_data(self) -> SolisCloudControlData:
        inverter_sn = self._inverter.info.serial_number
        if self._reload_pending:
            return SolisCloudControlData()
        if (retry_after := self._rate_limited_until - self.hass.loop.time()) > 0:
            raise UpdateFailed("Waiting for the SolisCloud API rate limit to reset", retry_after=retry_after)
        was_offline = self._offline
        retry_time = _UPDATE_RETRY_TIME_SECONDS if self.data is not None and not was_offline else 0
        failed_read_cid = self._offline_read_cid
        try:
            try:
                if failed_read_cid is not None:
                    await self._api_client.read(inverter_sn, failed_read_cid, max_retry_time=0)
                    failed_read_cid = None
                results = await self._api_client.read_batch(
                    inverter_sn, self._inverter.read_batch_cids, max_retry_time=retry_time
                )
            except SolisCloudControlApiError as error:
                if (
                    was_offline
                    and not error.is_offline
                    and not error.is_auth_error
                    and error.read_retry_delay() is None
                ):
                    # The old batch may no longer work if the inverter's capabilities changed.
                    if await self._async_reload_if_changed(inverter_sn):
                        return SolisCloudControlData()
                raise

            if was_offline and await self._async_reload_if_changed(inverter_sn):
                return SolisCloudControlData()

            for read_cid in self._inverter.read_cids:
                failed_read_cid = read_cid
                results[read_cid] = await self._api_client.read(inverter_sn, read_cid, max_retry_time=retry_time)

            if was_offline:
                _LOGGER.debug("Solis inverter is online again")
                self._offline = False
                self._offline_read_cid = None
            elif self._offline:
                # A control request reported the inverter offline during the refresh.
                return SolisCloudControlData()
            data = SolisCloudControlData({cid: results.get(cid) for cid in self._inverter.all_cids})
            _LOGGER.debug("Data read from API: %s", data)
            return data
        except SolisCloudControlApiError as error:
            if error.is_offline and self.data is not None:
                self._mark_offline(failed_read_cid)
                return SolisCloudControlData()
            if error.is_auth_error:
                raise ConfigEntryAuthFailed(f"Check the SolisCloud API key and token: {error}") from error
            if self.data is None and error.is_configuration_error:
                raise ConfigEntryError(f"Check the inverter and SolisCloud API configuration: {error}") from error
            if (retry_after := error.rate_limit_delay) is not None:
                self._rate_limited_until = self.hass.loop.time() + retry_after
            raise UpdateFailed(error, retry_after=retry_after) from error

    async def _async_reload_if_changed(self, inverter_sn: str) -> bool:
        info = await create_inverter_info(self._api_client, inverter_sn)
        if info == self._inverter.info:
            return False
        assert self.config_entry is not None
        self._reload_pending = True
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        return True

    def _mark_offline(self, read_cid: int | None = None) -> None:
        self._offline_read_cid = read_cid
        if not self._offline:
            _LOGGER.debug("Solis inverter is offline")
            self._offline = True

    async def control(
        self,
        cid: int,
        value: str,
        old_value: str | None = None,
    ) -> None:
        if self._offline:
            raise HomeAssistantError("The Solis inverter is offline")
        if self._rate_limited_until > self.hass.loop.time():
            raise HomeAssistantError("Waiting for the SolisCloud API rate limit to reset")
        try:
            inverter_sn = self._inverter.info.serial_number
            await self._api_client.control(inverter_sn, cid, value, old_value)
        except SolisCloudControlApiError as error:
            if error.is_offline:
                self._mark_offline()
                self.async_set_updated_data(SolisCloudControlData())
            else:
                if (retry_after := error.rate_limit_delay) is not None:
                    self._rate_limited_until = self.hass.loop.time() + retry_after
                await self.async_request_refresh()
            raise HomeAssistantError(f"SolisCloud could not confirm the control command: {error}") from error
        else:
            if self.data and not self._offline and self.last_update_success:
                new_data = SolisCloudControlData(self.data)
                new_data[cid] = value
                self.async_set_updated_data(new_data)
            await self.async_request_refresh()
