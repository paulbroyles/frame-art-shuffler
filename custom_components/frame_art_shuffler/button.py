"""Button entities for Frame Art Shuffler TV management."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import device_registry as dr

from .config_entry import get_tv_config, update_tv_config
from .const import DOMAIN, CONF_ENABLE_AUTO_SHUFFLE, CONF_LIGHT_SENSOR
from .frame_tv import tv_on, tv_off, set_art_mode, is_screen_on, delete_token, toggle_tv_orientation, get_tv_model_year, FrameArtError
from .shuffle import async_shuffle_tv
from .activity import log_activity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Frame Art button entities for a config entry."""
    # Read TV configs directly from entry (stored as dict with tv_id as key)
    tvs_dict = entry.data.get("tvs", {})
    
    entities: list[ButtonEntity] = []
    for tv_id, tv in tvs_dict.items():
        if not tv_id:
            continue

        # Always-created buttons
        entities.extend([
            FrameArtArtModeButton(hass, entry, tv_id),
            FrameArtShuffleButton(hass, entry, tv_id),
            FrameArtToggleOrientationButton(hass, entry, tv_id),
            FrameArtClearTokenButton(hass, entry, tv_id),
        ])

        # Auto-brightness buttons (only if light sensor configured)
        if tv.get(CONF_LIGHT_SENSOR):
            entities.extend([
                FrameArtCalibrateDarkButton(hass, entry, tv_id),
                FrameArtCalibrateBrightButton(hass, entry, tv_id),
                FrameArtTriggerBrightnessButton(hass, entry, tv_id),
            ])

        # Auto-motion buttons (only if motion sensors configured)
        if tv.get("motion_sensors"):
            entities.append(FrameArtTriggerMotionOffButton(hass, entry, tv_id))

    if entities:
        async_add_entities(entities)


class FrameArtRemoveTVButton(ButtonEntity):
    """Button entity to remove a TV."""

    _attr_has_entity_name = True
    _attr_name = "zzDANGER-DEL THIS TV"
    _attr_icon = "mdi:delete-alert"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tv_id: str,
    ) -> None:
        """Initialize the button entity."""
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry

        # Get TV name from config entry
        tv_config = get_tv_config(entry, tv_id)
        self._tv_name = tv_config.get("name", tv_id) if tv_config else tv_id
        self._tv_ip = tv_config.get("ip") if tv_config else None
        
        # Use tv_id as identifier (no home prefix)
        identifier = tv_id

        self._attr_unique_id = f"{tv_id}_remove"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identifier)},
            name=self._tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_press(self) -> None:
        """Handle the button press - remove this TV."""
        # First, try to clean up the token file
        if self._tv_ip:
            try:
                await self.hass.async_add_executor_job(delete_token, self._tv_ip)
                _LOGGER.info(f"Deleted token for {self._tv_name} ({self._tv_ip})")
            except Exception as err:
                _LOGGER.warning(f"Failed to delete token for {self._tv_name}: {err}")

        device_registry = dr.async_get(self.hass)
        
        # Find the device for this TV
        identifier = self._tv_id
        device = device_registry.async_get_device(identifiers={(DOMAIN, identifier)})
        
        if device:
            # Remove the device (this will trigger our device_removed listener)
            device_registry.async_remove_device(device.id)
            _LOGGER.info(f"Removed TV device: {self._tv_name}")
        
        # Also remove from config entry so it doesn't reappear on reload
        from .config_entry import remove_tv_config
        remove_tv_config(self.hass, self._entry, self._tv_id)
        _LOGGER.info(f"Removed TV {self._tv_name} from config entry")


class FrameArtArtModeButton(ButtonEntity):
    """Button entity to switch TV to art mode.

    Smart button: if the TV is reachable, sends art mode command directly.
    If unreachable and MAC is available, sends WoL first then art mode.
    """

    _attr_has_entity_name = True
    _attr_name = "Art Mode"
    _attr_icon = "mdi:palette"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tv_id: str,
    ) -> None:
        """Initialize the button entity."""
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry

        # Get TV config
        tv_config = get_tv_config(entry, tv_id)
        if tv_config:
            self._tv_name = tv_config.get("name", tv_id)
            self._tv_ip = tv_config.get("ip")
            self._tv_mac = tv_config.get("mac")
        else:
            self._tv_name = tv_id
            self._tv_ip = None
            self._tv_mac = None

        self._attr_unique_id = f"{tv_id}_art_mode"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=self._tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_press(self) -> None:
        """Handle the button press - switch TV to art mode, waking if needed."""
        if not self._tv_ip:
            _LOGGER.error(f"Cannot switch {self._tv_name} to art mode: missing IP address in config")
            return

        try:
            data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
            client = data.get("art_clients", {}).get(self._tv_id)
            if client is None:
                raise FrameArtError(f"No art client for {self._tv_name}")

            # Check if screen is on before connecting.  When the TV is in "screen
            # off, art mode standby" (e.g. turned off by Apple TV CEC), the art
            # WebSocket is still reachable and set_artmode("on") is a no-op — the
            # screen stays dark.  We must wake it first via WoL.
            screen_is_on = await is_screen_on(self._tv_ip, timeout=3)
            if not screen_is_on:
                if not self._tv_mac:
                    raise FrameArtError(
                        f"{self._tv_name} screen is off and no MAC address configured for Wake-on-LAN"
                    )
                _LOGGER.info(f"{self._tv_name} screen is off, sending Wake-on-LAN...")

                # Register a wakeup callback before sending WoL.  If the TV sends a
                # 'wakeup' D2D event after booting (on a reconnected art channel), the
                # background probe's reconnect will deliver it and we can react instantly.
                # The callback is on the same _art object across reconnects, so it
                # survives the connection dying during reboot.
                wakeup_event = asyncio.Event()

                async def _on_wakeup(event, response):
                    wakeup_event.set()

                client.set_wakeup_callback(_on_wakeup)

                # tv_on() handles both deep sleep (two-packet WoL) and network-awake
                # standby (REST responds immediately → second WoL fires right away).
                await tv_on(self._tv_ip, self._tv_mac, client=client)

                # After WoL the TV reboots fully (~30-40s).  Poll every 4s; also
                # react immediately if the wakeup event fires via the background probe.
                _LOGGER.info(f"{self._tv_name} WoL sent, waiting for TV to boot (up to 60s)...")
                deadline = asyncio.get_event_loop().time() + 60
                last_err = None
                try:
                    while asyncio.get_event_loop().time() < deadline:
                        # Wait up to 4s for a wakeup event, then try anyway
                        remaining = deadline - asyncio.get_event_loop().time()
                        try:
                            await asyncio.wait_for(wakeup_event.wait(), timeout=min(4, remaining))
                            _LOGGER.info(f"{self._tv_name} wakeup event received — setting art mode")
                        except asyncio.TimeoutError:
                            pass
                        try:
                            await client.ensure_connected(timeout=8)
                            await set_art_mode(client)
                            _LOGGER.info(f"Switched {self._tv_name} to art mode (after WoL)")
                            log_activity(
                                self.hass, self._entry.entry_id, self._tv_id,
                                "screen_on",
                                "Art Mode",
                            )
                            return
                        except Exception as err:
                            last_err = err
                            wakeup_event.clear()
                            _LOGGER.debug(f"{self._tv_name} not ready yet ({err}), retrying...")
                finally:
                    client.set_wakeup_callback(None)

                raise FrameArtError(
                    f"{self._tv_name} did not become ready within 60s after WoL: {last_err}"
                )

            # TV screen was already on — just connect and switch to art mode
            try:
                await client.ensure_connected(timeout=8)
            except Exception as conn_err:
                raise FrameArtError(f"Could not connect to art channel on {self._tv_name}: {conn_err}") from conn_err

            await set_art_mode(client)
            _LOGGER.info(f"Switched {self._tv_name} to art mode")
            log_activity(
                self.hass, self._entry.entry_id, self._tv_id,
                "screen_on",
                "Art Mode",
            )
        except FrameArtError as err:
            _LOGGER.error(f"Failed to switch {self._tv_name} to art mode: {err}")


class FrameArtShuffleButton(ButtonEntity):
    """Button entity to shuffle to a random image without waking the screen."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:shuffle-variant"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tv_id: str,
    ) -> None:
        """Initialize the shuffle button entity."""
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry

        # Get TV config
        tv_config = get_tv_config(entry, tv_id)
        if tv_config:
            self._tv_name = tv_config.get("name", tv_id)
            self._tv_ip = tv_config.get("ip")
        else:
            self._tv_name = tv_id
            self._tv_ip = None

        self._attr_name = f"{self._tv_name} Shuffle"
        self._attr_unique_id = f"{tv_id}_shuffle"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=self._tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_press(self) -> None:
        """Handle the button press - shuffle without waking the screen.

        Always silent (screen_on=False). If auto-shuffle is enabled,
        restarts the timer so next auto-shuffle is a full interval from now.
        """
        log_activity(self.hass, self._entry.entry_id, self._tv_id, f"Shuffle button pressed for {self._tv_name}")
        _LOGGER.info(f"Shuffle button pressed for {self._tv_name}")

        await async_shuffle_tv(self.hass, self._entry, self._tv_id, reason="button", screen_on=False)

        # Restart auto-shuffle timer if enabled (so next auto-shuffle is a full interval away)
        tv_config = get_tv_config(self._entry, self._tv_id)
        if tv_config and tv_config.get(CONF_ENABLE_AUTO_SHUFFLE, False):
            data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
            if data:
                start_auto_shuffle_timer = data.get("start_auto_shuffle_timer")
                if start_auto_shuffle_timer:
                    start_auto_shuffle_timer(self._tv_id)


class FrameArtClearTokenButton(ButtonEntity):
    """Button entity to clear the saved token for a TV."""

    _attr_has_entity_name = True
    _attr_name = "Clear Token"
    _attr_icon = "mdi:key-remove"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tv_id: str,
    ) -> None:
        """Initialize the button entity."""
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry

        # Get TV config
        tv_config = get_tv_config(entry, tv_id)
        if tv_config:
            self._tv_name = tv_config.get("name", tv_id)
            self._tv_ip = tv_config.get("ip")
        else:
            self._tv_name = tv_id
            self._tv_ip = None

        self._attr_unique_id = f"{tv_id}_clear_token"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=self._tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_press(self) -> None:
        """Handle the button press - delete the token file."""
        if not self._tv_ip:
            _LOGGER.error(f"Cannot clear token for {self._tv_name}: missing IP address in config")
            return

        try:
            await self.hass.async_add_executor_job(delete_token, self._tv_ip)
            _LOGGER.info(f"Cleared token for {self._tv_name}")
        except FrameArtError as err:
            _LOGGER.error(f"Failed to clear token for {self._tv_name}: {err}")


class FrameArtCalibrateDarkButton(ButtonEntity):
    """Button entity to calibrate min lux (set to current sensor value)."""

    _attr_has_entity_name = True
    _attr_name = "Auto-Bright Calibrate Dark"
    _attr_icon = "mdi:brightness-5"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tv_id: str,
    ) -> None:
        """Initialize the button entity."""
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry

        tv_config = get_tv_config(entry, tv_id)
        self._tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_unique_id = f"{tv_id}_calibrate_dark"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=self._tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_press(self) -> None:
        """Handle the button press - set min_lux to current sensor value."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            _LOGGER.error(f"Cannot calibrate {self._tv_name}: TV config not found")
            return

        light_sensor = tv_config.get("light_sensor")
        if not light_sensor:
            _LOGGER.warning(f"Cannot calibrate {self._tv_name}: no light sensor configured")
            return

        state = self.hass.states.get(light_sensor)
        if not state or state.state in ("unknown", "unavailable"):
            _LOGGER.warning(f"Cannot calibrate {self._tv_name}: sensor {light_sensor} is unavailable")
            return

        try:
            current_lux = float(state.state)
        except ValueError:
            _LOGGER.warning(f"Cannot calibrate {self._tv_name}: sensor value '{state.state}' is not a number")
            return

        update_tv_config(
            self.hass,
            self._entry,
            self._tv_id,
            {"min_lux": int(current_lux)},
        )
        _LOGGER.info(f"Calibrated min_lux for {self._tv_name} to {int(current_lux)}")


class FrameArtCalibrateBrightButton(ButtonEntity):
    """Button entity to calibrate max lux (set to current sensor value)."""

    _attr_has_entity_name = True
    _attr_name = "Auto-Bright Calibrate Bright"
    _attr_icon = "mdi:brightness-7"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tv_id: str,
    ) -> None:
        """Initialize the button entity."""
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry

        tv_config = get_tv_config(entry, tv_id)
        self._tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_unique_id = f"{tv_id}_calibrate_bright"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=self._tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_press(self) -> None:
        """Handle the button press - set max_lux to current sensor value."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            _LOGGER.error(f"Cannot calibrate {self._tv_name}: TV config not found")
            return

        light_sensor = tv_config.get("light_sensor")
        if not light_sensor:
            _LOGGER.warning(f"Cannot calibrate {self._tv_name}: no light sensor configured")
            return

        state = self.hass.states.get(light_sensor)
        if not state or state.state in ("unknown", "unavailable"):
            _LOGGER.warning(f"Cannot calibrate {self._tv_name}: sensor {light_sensor} is unavailable")
            return

        try:
            current_lux = float(state.state)
        except ValueError:
            _LOGGER.warning(f"Cannot calibrate {self._tv_name}: sensor value '{state.state}' is not a number")
            return

        update_tv_config(
            self.hass,
            self._entry,
            self._tv_id,
            {"max_lux": int(current_lux)},
        )
        _LOGGER.info(f"Calibrated max_lux for {self._tv_name} to {int(current_lux)}")


class FrameArtTriggerBrightnessButton(ButtonEntity):
    """Button entity to trigger auto brightness adjustment now."""

    _attr_has_entity_name = True
    _attr_name = "Auto-Bright Trigger Now"
    _attr_icon = "mdi:brightness-auto"
    # No entity_category = shows in Controls section

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tv_id: str,
    ) -> None:
        """Initialize the button entity."""
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry

        tv_config = get_tv_config(entry, tv_id)
        self._tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_unique_id = f"{tv_id}_trigger_brightness"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=self._tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_press(self) -> None:
        """Handle the button press - trigger auto brightness adjustment."""
        # Get the helper function from hass.data
        data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if not data:
            _LOGGER.error(f"Cannot trigger auto brightness for {self._tv_name}: integration data not found")
            return

        async_adjust_tv_brightness = data.get("async_adjust_tv_brightness")
        if not async_adjust_tv_brightness:
            _LOGGER.error(f"Cannot trigger auto brightness for {self._tv_name}: brightness function not found")
            return

        # Pass restart_timer=True to reset the per-TV timer
        success = await async_adjust_tv_brightness(self._tv_id, restart_timer=True)
        if success:
            _LOGGER.info(f"Triggered auto brightness adjustment for {self._tv_name}")
        else:
            _LOGGER.warning(f"Auto brightness adjustment failed for {self._tv_name}")


class FrameArtTriggerMotionOffButton(ButtonEntity):
    """Button entity to trigger auto motion off (turn TV off) now."""

    _attr_has_entity_name = True
    _attr_name = "Auto-Motion Off Now"
    _attr_icon = "mdi:television-off"
    # No entity_category = shows in Controls section

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tv_id: str,
    ) -> None:
        """Initialize the button entity."""
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry

        tv_config = get_tv_config(entry, tv_id)
        self._tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_unique_id = f"{tv_id}_trigger_motion_off"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=self._tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_press(self) -> None:
        """Handle the button press - turn TV off and cancel motion timer."""
        from datetime import datetime, timezone

        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            _LOGGER.error(f"Cannot trigger motion off for {self._tv_name}: TV config not found")
            return

        ip = tv_config.get("ip")
        if not ip:
            _LOGGER.error(f"Cannot trigger motion off for {self._tv_name}: no IP address")
            return

        # Cancel any pending off timer
        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if data:
            # Clear the scheduled off time
            motion_off_times = data.get("motion_off_times", {})
            if self._tv_id in motion_off_times:
                del motion_off_times[self._tv_id]

        # Turn off the TV
        try:
            _LOGGER.info(f"Auto motion trigger: Turning off {self._tv_name} ({ip})")
            await tv_off(ip)
            _LOGGER.info(f"Auto motion trigger: {self._tv_name} turned off successfully")
            log_activity(
                self._hass, self._entry.entry_id, self._tv_id,
                "motion_off",
                "Turned off (button pressed)",
            )
        except Exception as err:
            _LOGGER.warning(f"Auto motion trigger: Failed to turn off {self._tv_name}: {err}")
            log_activity(
                self._hass, self._entry.entry_id, self._tv_id,
                "error",
                f"Turn off failed: {err}",
            )


class FrameArtToggleOrientationButton(ButtonEntity):
    """Button entity to toggle TV orientation (portrait/landscape)."""

    _attr_has_entity_name = True
    _attr_name = "Toggle Orientation"
    _attr_icon = "mdi:screen-rotation"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tv_id: str,
    ) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry

        tv_config = get_tv_config(entry, tv_id)
        if tv_config:
            self._tv_name = tv_config.get("name", tv_id)
            self._tv_ip = tv_config.get("ip")
        else:
            self._tv_name = tv_id
            self._tv_ip = None

        self._attr_unique_id = f"{tv_id}_toggle_orientation"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=self._tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_press(self) -> None:
        """Handle the button press - toggle TV orientation."""
        if not self._tv_ip:
            _LOGGER.error(f"Cannot toggle orientation for {self._tv_name}: missing IP address")
            return

        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        tv_model_years = data.get("tv_model_years", {})
        year = tv_model_years.get(self._tv_id)
        if year is None:
            year = await get_tv_model_year(self._tv_ip)
            if year is not None:
                tv_model_years[self._tv_id] = year
        is_2024 = year is not None and year >= 24

        try:
            await toggle_tv_orientation(self._tv_ip, is_2024=is_2024)
            _LOGGER.info(f"Toggled orientation for {self._tv_name} (model year: {year})")
        except Exception as err:
            _LOGGER.error(f"Failed to toggle orientation for {self._tv_name}: {err}")
