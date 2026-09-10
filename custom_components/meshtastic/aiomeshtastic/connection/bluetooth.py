# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
# SPDX-FileCopyrightText: 2025 Hendrik @novag
#
# SPDX-License-Identifier: MIT

import asyncio
import struct
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import bleak
from bleak import BaseBleakClient, BleakClient, BleakGATTCharacteristic
from bleak_retry_connector import establish_connection
from google.protobuf import message

from ..protobuf import mesh_pb2  # noqa: TID252
from . import ClientApiConnection
from .errors import (
    ClientApiConnectionError,
    ClientApiNotConnectedError,
)
from .pairing import (
    PAIR_TIMEOUT,
    PairingUnavailableError,
    async_get_device_state,
    async_set_device_trusted,
    normalise_pin,
    pairing_agent,
)

if TYPE_CHECKING:
    from bleak.backends.service import BleakGATTService


# The fromNum characteristic carries a single little-endian uint32.
FROM_NUM_LENGTH = 4


class BluetoothConnectionError(ClientApiConnectionError):
    pass


class BluetoothConnectionServiceNotFoundError(BluetoothConnectionError):
    """The peer is connected but does not expose the Meshtastic GATT service."""

    def __init__(self) -> None:
        # Previously this did not derive from Exception at all, so `raise
        # BluetoothConnectionServiceNotFoundError` produced "TypeError: exceptions must derive
        # from BaseException" instead of the intended error.
        super().__init__("Bluetooth meshtastic service not found")


class BluetoothConnection(ClientApiConnection):
    BTM_SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
    BTM_CHARACTERISTIC_FROM_RADIO_UUID = "2c55e69e-4993-11ed-b878-0242ac120002"
    BTM_CHARACTERISTIC_TO_RADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"
    BTM_CHARACTERISTIC_FROM_NUM_UUID = "ed9da18c-a800-4f66-a670-aa7547e34453"
    BTM_CHARACTERISTIC_LOG_UUID = "5a3d6e49-06e6-4423-9944-e9de8cdf9547"

    def __init__(  # noqa: PLR0913
        self,
        ble_address: str,
        ble_device: Any | None = None,
        bleak_client_backend: type[BaseBleakClient] | None = None,
        connect_timeout: float = 10.0,
        ble_device_provider: Callable[[], Any | None] | None = None,
        pin: str | None = None,
    ) -> None:
        super().__init__()
        self._ble_address = ble_address
        self._ble_device = ble_device
        self._ble_device_provider = ble_device_provider
        self._pin = normalise_pin(pin)
        self._bleak_client_backend = bleak_client_backend
        self._connect_timeout = connect_timeout
        self._bleak_client: BleakClient | None = None
        self._ble_meshtastic_service: BleakGATTService | None = None
        self._ble_from_radio: BleakGATTCharacteristic | None = None
        self._ble_to_radio: BleakGATTCharacteristic | None = None
        self._ble_from_num: BleakGATTCharacteristic | None = None
        self._ble_log: BleakGATTCharacteristic | None = None
        self._write_lock = asyncio.Lock()
        self._last_packet_number = None
        self._force_read_event = asyncio.Event()
        # Set after a GATT failure so the next connect rediscovers services instead of trusting
        # BlueZ's cache, whose handles may be stale.
        self._force_fresh_services = False

    def _resolve_ble_device(self) -> Any | None:
        """
        Look up the current BLEDevice for our address.

        A BLEDevice is only valid for as long as the adapter keeps its backing connection state.
        After a Home Assistant restart, a Bluetooth adapter reset, or the node re-advertising, a
        device object captured earlier is stale and connecting through it fails with obscure
        BlueZ errors, so re-resolve on every attempt and only fall back to the cached object.
        """
        if self._ble_device_provider is not None:
            with suppress(Exception):
                device = self._ble_device_provider()
                if device is not None:
                    self._ble_device = device
                    return device
            self._logger.debug("Could not resolve current BLE device for %s", self._ble_address)
        return self._ble_device

    async def _connect(self) -> None:
        # Drop any previous client so a failed attempt can never leave a stale one behind for
        # `is_connected` to report on.
        self._bleak_client = None
        started = asyncio.get_running_loop().time()

        def elapsed() -> float:
            return asyncio.get_running_loop().time() - started

        ble_device = self._resolve_ble_device()
        self._logger.info(
            "Connecting to bluetooth device %s (%s)",
            self._ble_address,
            "via Home Assistant's bluetooth stack" if ble_device is not None else "by address",
        )
        if self._force_fresh_services:
            self._logger.info(
                "Rediscovering GATT services for %s after the previous connection failed", self._ble_address
            )
        if ble_device is not None:
            self._bleak_client = await establish_connection(
                client_class=BleakClient,
                device=ble_device,
                name=self._ble_address,
                max_attempts=3,
                # Re-resolved between the retry attempts too, not just before the first one.
                ble_device_callback=self._resolve_ble_device,
                # Cached services make reconnects fast, but a cache holding stale handles yields
                # an instant "Failed to send read request" on every read. After such a failure,
                # rediscover rather than trusting the cache again.
                use_services_cache=not self._force_fresh_services,
            )
        else:
            self._bleak_client = BleakClient(
                self._ble_address, timeout=self._connect_timeout, backend=self._bleak_client_backend
            )
            await self._bleak_client.connect()

        self._logger.info("Bluetooth link to %s established after %.1fs", self._ble_address, elapsed())

        await self._ensure_paired()

        self._ble_meshtastic_service = self._bleak_client.services[BluetoothConnection.BTM_SERVICE_UUID]

        if self._ble_meshtastic_service is None:
            # The peer answered but exposes no Meshtastic service: a non-Meshtastic device at
            # this address, or service discovery came back incomplete.
            self._logger.warning(
                "Device %s does not expose the meshtastic GATT service (%s)",
                self._ble_address,
                BluetoothConnection.BTM_SERVICE_UUID,
            )
            raise BluetoothConnectionServiceNotFoundError

        self._logger.info("Meshtastic GATT service ready on %s after %.1fs", self._ble_address, elapsed())
        # Only cleared once we have a usable service handle, so a failed attempt keeps forcing
        # rediscovery on the next try.
        self._force_fresh_services = False

        self._ble_from_radio = self._ble_meshtastic_service.get_characteristic(
            BluetoothConnection.BTM_CHARACTERISTIC_FROM_RADIO_UUID
        )
        self._ble_to_radio = self._ble_meshtastic_service.get_characteristic(
            BluetoothConnection.BTM_CHARACTERISTIC_TO_RADIO_UUID
        )
        self._ble_from_num = self._ble_meshtastic_service.get_characteristic(
            BluetoothConnection.BTM_CHARACTERISTIC_FROM_NUM_UUID
        )
        self._ble_log = self._ble_meshtastic_service.get_characteristic(BluetoothConnection.BTM_CHARACTERISTIC_LOG_UUID)

    async def _ensure_paired(self) -> None:
        """
        Bond with the node, supplying the configured PIN if there is one.

        Meshtastic firmware defaults to `bluetooth.mode = RANDOM_PIN` and will not serve the
        fromRadio/toRadio characteristics over an unauthenticated link. bleak cannot supply a
        passkey itself (https://github.com/hbldh/bleak/issues/1434), so a PIN means standing up a
        BlueZ pairing agent for the duration of the pairing. `pair()` is a no-op once BlueZ has a
        bond on file, so this is cheap on every reconnect after the first.
        """
        if self._pin:
            try:
                async with pairing_agent(self._pin) as agent:
                    await asyncio.wait_for(self._bleak_client.pair(), timeout=PAIR_TIMEOUT)
                    if agent.was_consulted.is_set():
                        self._logger.info("Bonded with %s using the configured PIN", self._ble_address)
            except PairingUnavailableError as e:
                # No system D-Bus (Bluetooth proxy, or a container without it mounted). An
                # already-bonded node still works, so carry on and let the caller find out.
                self._logger.warning(
                    "Cannot supply the bluetooth PIN automatically (%s). "
                    "If this node is not bonded yet, pair it once from the host with "
                    "'bluetoothctl' - see the integration documentation.",
                    e,
                )
            except Exception:  # noqa: BLE001
                self._logger.warning("Pairing with PIN failed for %s", self._ble_address, exc_info=True)
            else:
                # The equivalent of 'bluetoothctl trust': lets the node reconnect later without
                # any agent being registered, which is what survives a Home Assistant restart.
                await async_set_device_trusted(self._ble_address)
                return

        # No PIN configured, or the PIN path did not get us bonded. Pairing may still be
        # unnecessary (bluetooth.mode = NO_PIN) or already done, so attempt it best-effort
        # exactly as before.
        try:
            await asyncio.wait_for(self._bleak_client.pair(), timeout=PAIR_TIMEOUT)
        except:  # noqa: E722
            self._logger.debug("Pairing failed", exc_info=True)

        await self._warn_if_not_bonded()

    async def _warn_if_not_bonded(self) -> None:
        """
        Say so plainly when BlueZ has no bond, instead of leaving the user with GATT errors.

        Without a bond the node accepts the connection but refuses the fromRadio/toRadio
        characteristics, which surfaces later as opaque "Failed to send read request" style
        errors rather than anything pointing at pairing.
        """
        state = await async_get_device_state(self._ble_address)
        if state is None or state.get("Paired"):
            return

        if self._pin:
            self._logger.warning(
                "Node %s is still not paired with BlueZ after trying the configured PIN. "
                "Check that the PIN matches the one shown on the node's screen "
                "(bluetooth.mode = RANDOM_PIN generates a new one) and that no interactive "
                "bluetoothctl session is holding the pairing agent.",
                self._ble_address,
            )
        else:
            self._logger.warning(
                "Node %s is not paired with BlueZ and no bluetooth PIN is configured. "
                "Add the node's PIN in the integration options, or set the node's "
                "bluetooth.mode to NO_PIN.",
                self._ble_address,
            )

    async def _disconnect(self) -> None:
        client = self._bleak_client
        self._bleak_client = None
        if client is None:
            return
        try:
            await asyncio.wait_for(client.disconnect(), timeout=self._connect_timeout)
        except:  # noqa: E722
            self._logger.debug("Disconnecting failed", exc_info=True)

    @property
    def is_connected(self) -> bool:
        # Reported before the first connect and after a failed one, so it has to tolerate the
        # client being absent rather than raising AttributeError into the reconnect loop.
        return self._bleak_client is not None and self._bleak_client.is_connected

    async def _handle_notify_wait(  # noqa: PLR0913
        self,
        packet_num_queue: asyncio.Queue,
        force_read_event: asyncio.Event,
        notify_timeout_duration: int,
        notify_timeout_count: int,
        max_notify_timeouts_before_restart: int,
        restart_notify_func: callable,
    ) -> tuple[bool, int]:
        """Wait for packet notification or force read event."""
        wait_notify = asyncio.create_task(packet_num_queue.get(), name="wait_notify")
        wait_force_read = asyncio.create_task(force_read_event.wait(), name="wait_force_read")

        done, pending = await asyncio.wait(
            {wait_notify, wait_force_read},
            timeout=notify_timeout_duration,
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Ensure pending tasks are cancelled before proceeding
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        continue_active_read = False
        if wait_force_read in done:
            self._logger.debug("Force read event received. Continuing loop for active read.")
            force_read_event.clear()
            notify_timeout_count = 0  # Reset timeout counter
            continue_active_read = True
        elif wait_notify in done:
            self._logger.debug("Packet notification received. Will attempt read.")
            _ = wait_notify.result()
            notify_timeout_count = 0  # Reset timeout counter
        else:  # Timeout occurred
            notify_timeout_count += 1
            if notify_timeout_count > max_notify_timeouts_before_restart:
                self._logger.debug(
                    "No bluetooth notification for %d times after %ds timeout, restarting notifications",
                    notify_timeout_count,
                    max_notify_timeouts_before_restart,
                )
                notify_timeout_count = 0
                await restart_notify_func()
            # continue with active read
            continue_active_read = True

        return continue_active_read, notify_timeout_count

    async def _packet_stream(self) -> AsyncGenerator[mesh_pb2.FromRadio, Any]:  # noqa: PLR0915
        if not self.is_connected:
            return
        # Bound to a local: a concurrent disconnect clears the attribute, and the stream must fail
        # with a connection error rather than an AttributeError on None.
        client = self._bleak_client
        packet_num_queue = asyncio.Queue()
        force_read_event = self._force_read_event

        def notification_handler(_: BleakGATTCharacteristic, data: bytearray) -> None:
            if len(data) != FROM_NUM_LENGTH:
                self._logger.debug("Ignoring unexpected fromNum notification of %d bytes", len(data))
                return
            nums = struct.unpack("<I", data)
            num = nums[0]

            if num != self._last_packet_number:
                self._last_packet_number = num
                self._logger.debug("New packet available: %s", num)
                packet_num_queue.put_nowait(num)
            else:
                self._logger.debug("Duplicate packet notification: %s", num)

        try:

            async def start_notify() -> None:
                await asyncio.wait_for(client.start_notify(self._ble_from_num, notification_handler), timeout=30)

            async def stop_notify() -> None:
                await asyncio.wait_for(client.stop_notify(self._ble_from_num), timeout=30)

            async def restart_notify() -> None:
                try:
                    with suppress(Exception):
                        await stop_notify()
                    await start_notify()
                except:  # noqa: E722
                    self._logger.debug("Restart notify failed", exc_info=True)

            await start_notify()

            notify_timeout_count = 0
            notify_timeout_duration = 300
            max_notify_timeouts_before_restart = 2
            while True:
                packet = await client.read_gatt_char(self._ble_from_radio)
                if not isinstance(packet, bytes):
                    packet = bytes(packet)
                if packet == b"":
                    # no more packets available, waiting for notification or force_read event.
                    # if we do not receive bluetooth notifications for an extended period of time, this could be an
                    # indication of issue with bluetooth stack, so we try to do an active read. This will either trigger
                    # an error or help resume sending of data by the firmware. If this happens too often, we try to
                    # re-start notifications.
                    continue_active_read, notify_timeout_count = await self._handle_notify_wait(
                        packet_num_queue,
                        force_read_event,
                        notify_timeout_duration,
                        notify_timeout_count,
                        max_notify_timeouts_before_restart,
                        restart_notify,
                    )
                    if continue_active_read:
                        continue

                elif notify_timeout_count > 0:
                    self._logger.debug(
                        "Read returned packet after ble notify timeout, maybe notifications from device have stopped"
                    )

                from_radio = mesh_pb2.FromRadio()
                try:
                    from_radio.ParseFromString(packet)
                    self._logger.debug("Parsed packet: %s", self._protobuf_log(from_radio))
                    yield from_radio
                except message.DecodeError:
                    self._logger.warning("Error while parsing FromRadio bytes %s", packet, exc_info=True)
        except bleak.BleakError as e:
            # The characteristic handles we hold do not work on this link, so do not reuse
            # BlueZ's cached GATT table on the next attempt.
            self._force_fresh_services = True
            self._logger.warning(
                "Bluetooth read from %s failed (%s: %s); will rediscover services on reconnect",
                self._ble_address,
                type(e).__name__,
                e,
            )
            raise BluetoothConnectionError from e
        finally:
            # The client may already be gone (disconnected concurrently), and BlueZ happily
            # raises on stop_notify for a dropped link — neither should mask the real error.
            with suppress(Exception):
                if client.is_connected:
                    await client.stop_notify(self._ble_from_num)

    async def _send_packet(self, data: bytes) -> bool:
        if not self.is_connected:
            raise ClientApiNotConnectedError

        # Check if this packet requires a forced read
        try:
            to_radio = mesh_pb2.ToRadio()
            to_radio.ParseFromString(data)
            if to_radio.HasField("want_config_id"):
                self._logger.debug("want_config_id detected, setting force read event.")
                self._force_read_event.set()
        except message.DecodeError:
            self._logger.warning("Could not parse ToRadio packet in _send_packet to check for want_config_id.")

        async with self._write_lock:
            try:
                await self._bleak_client.write_gatt_char(self._ble_to_radio, data)
            except bleak.BleakError:
                self._logger.debug("Failed to send data", exc_info=True)
                return False
            else:
                return True
