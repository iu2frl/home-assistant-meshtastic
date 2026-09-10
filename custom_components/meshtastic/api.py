# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Self

import google
from google.protobuf.json_format import MessageToDict
from homeassistant.exceptions import IntegrationError

from .aiomeshtastic import (
    BluetoothConnection as AioBluetoothConnection,
)
from .aiomeshtastic import (
    MeshInterface,
)
from .aiomeshtastic import (
    MeshInterface as AioMeshInterface,
)
from .aiomeshtastic import (
    SerialConnection as AioSerialConnection,
)
from .aiomeshtastic import (
    TcpConnection as AioTcpConnection,
)
from .aiomeshtastic.errors import MeshRoutingError, MeshtasticError
from .aiomeshtastic.protobuf import portnums_pb2
from .const import (
    CONF_CONNECTION_BLUETOOTH_ADDRESS,
    CONF_CONNECTION_BLUETOOTH_PIN,
    CONF_CONNECTION_SERIAL_PORT,
    CONF_CONNECTION_TCP_HOST,
    CONF_CONNECTION_TCP_PORT,
    CONF_CONNECTION_TYPE,
    DOMAIN,
    LOGGER,
    ConnectionType,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping, MutableMapping
    from types import MappingProxyType, TracebackType

    from google.protobuf.message import Message
    from homeassistant.core import HomeAssistant

    from .aiomeshtastic.interface import MeshNode, TelemetryType
    from .aiomeshtastic.packet import Packet

_LOGGER = LOGGER.getChild(__name__.rpartition(".")[2])


EVENT_MESHTASTIC_API_BASE = f"{DOMAIN}_api"
EVENT_MESHTASTIC_API_NODE_UPDATED = EVENT_MESHTASTIC_API_BASE + "_node_updated"
EVENT_MESHTASTIC_API_TELEMETRY = EVENT_MESHTASTIC_API_BASE + "_telemetry"
EVENT_MESHTASTIC_API_PACKET = EVENT_MESHTASTIC_API_BASE + "_packet"
EVENT_MESHTASTIC_API_TEXT_MESSAGE = EVENT_MESHTASTIC_API_BASE + "_text_message"
EVENT_MESHTASTIC_API_POSITION = EVENT_MESHTASTIC_API_BASE + "_position"

ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_EVENT_MESHTASTIC_API_NODE = "node"
ATTR_EVENT_MESHTASTIC_API_DATA = "data"
ATTR_EVENT_MESHTASTIC_API_TELEMETRY_TYPE = "telemetry_type"
ATTR_EVENT_MESHTASTIC_API_NODE_INFO = "node_info"


class EventMeshtasticApiTelemetryType(StrEnum):
    DEVICE_METRICS = "device_metrics"
    LOCAL_STATS = "local_stats"
    ENVIRONMENT_METRICS = "environment_metrics"
    POWER_METRICS = "power_metrics"


class MeshtasticApiClientError(IntegrationError):
    """Exception to indicate a general API error."""


class MeshtasticApiClientCommunicationError(
    MeshtasticApiClientError,
):
    """Exception to indicate a communication error."""


class MeshtasticApiClientConfigError(MeshtasticApiClientCommunicationError):
    """
    The transport opened, but the radio never delivered its configuration.

    Worth distinguishing from a plain communication error: the link is fine, so the remedies are
    different (another client holding the radio's single connection slot, a weak link, or a node
    that needs a restart) and the user should be told which of the two happened.
    """


class MeshtasticApiClient:
    # Time budget for opening the transport (BLE/TCP/serial handshake).
    CONNECT_TIMEOUT = 30
    # Time budget for the initial config download from the radio, which streams the whole node
    # database. Default sized for `async_setup_entry`, where an unbounded wait blocks Home
    # Assistant startup and a failure is retried via ConfigEntryNotReady anyway. A large mesh
    # over BLE can legitimately take minutes, so callers that can afford to wait - notably the
    # config flow, where the user is watching a spinner - pass a larger `config_timeout`.
    CONFIG_TIMEOUT = 120
    # Budget for the config flow. A flow step runs inside an HTTP request, and reverse proxies
    # and tunnels in front of Home Assistant commonly sever that request somewhere around
    # 60-120s. When they do, the step is cancelled and the frontend shows its own generic
    # "unknown error" instead of anything we wrote - so this has to stay *below* that ceiling
    # for our own diagnosis to be what the user actually sees. Downloads that genuinely need
    # longer want `async_show_progress`, which is not bound by the request lifetime at all.
    CONFIG_FLOW_CONFIG_TIMEOUT = 75
    # Time budget for callers that need the config to be present before they can answer.
    READY_TIMEOUT = 30
    # Time budget for tearing everything down; Home Assistant's shutdown window is finite.
    DISCONNECT_TIMEOUT = 10
    # At or below this RSSI a BLE link still connects but struggles to sustain a bulk transfer.
    WEAK_RSSI_DBM = -80

    def __init__(
        self,
        data: MappingProxyType[str, Any],
        hass: HomeAssistant,
        config_entry_id: str | None,
        *,
        no_nodes: bool = False,
        config_timeout: float | None = None,
    ) -> None:
        self._logger = LOGGER.getChild(self.__class__.__name__)
        self._connected = asyncio.Event()
        self._hass = hass
        self._config_entry_id = config_entry_id
        self._config_timeout = self.CONFIG_TIMEOUT if config_timeout is None else config_timeout
        self._ble_address: str | None = None

        connection_type = data[CONF_CONNECTION_TYPE]
        # Kept for log messages, so a connect failure says which transport it was using.
        self._connection_type = connection_type

        if connection_type == ConnectionType.TCP.value:
            connection = AioTcpConnection(host=data[CONF_CONNECTION_TCP_HOST], port=data[CONF_CONNECTION_TCP_PORT])
        elif connection_type == ConnectionType.BLUETOOTH.value:
            ble_address = data[CONF_CONNECTION_BLUETOOTH_ADDRESS]
            self._ble_address = ble_address
            connection = AioBluetoothConnection(
                ble_address=ble_address,
                # Resolved lazily on every (re)connect: a BLEDevice captured once at setup time
                # goes stale when the adapter resets or the device re-advertises, which is why a
                # gateway that paired fine could never be reconnected to after a restart.
                ble_device_provider=self._make_ble_device_provider(ble_address),
                pin=data.get(CONF_CONNECTION_BLUETOOTH_PIN),
            )
        elif connection_type == ConnectionType.SERIAL.value:
            connection = AioSerialConnection(device=data[CONF_CONNECTION_SERIAL_PORT])
        else:
            msg = f"Unsupported connection type {connection_type}"
            raise ValueError(msg)

        self._interface = AioMeshInterface(
            connection=connection, no_nodes=no_nodes, heartbeat_interval=timedelta(minutes=5)
        )
        self._packet_processor: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task] = set()

        self._interface.add_packet_app_listener(
            packet_type=portnums_pb2.PortNum.NODEINFO_APP, callback=self._on_node_info, as_dict=True
        )
        self._interface.add_packet_app_listener(
            packet_type=portnums_pb2.PortNum.TEXT_MESSAGE_APP, callback=self._on_text_message, as_packet=True
        )
        self._interface.add_packet_app_listener(
            packet_type=portnums_pb2.PortNum.TELEMETRY_APP, callback=self._on_telemetry, as_dict=True
        )
        self._interface.add_packet_app_listener(
            packet_type=portnums_pb2.PortNum.POSITION_APP, callback=self._on_position, as_dict=True
        )

    def _make_ble_device_provider(self, ble_address: str) -> Callable[[], Any]:
        def provider() -> Any:
            if not self._hass:
                return None
            from homeassistant.components.bluetooth import async_ble_device_from_address

            return async_ble_device_from_address(self._hass, ble_address, connectable=True)

        return provider

    def _log_bluetooth_signal(self) -> None:
        """
        Report the node's signal strength before attempting the config download.

        A weak link still connects and bonds perfectly well, but cannot sustain the throughput
        needed to stream a large node database - which surfaces only as a timeout, or as opaque
        BlueZ read errors, with nothing pointing at radio conditions. Saying it up front turns
        that into something the user can act on.
        """
        if self._ble_address is None or not self._hass:
            return
        # Purely advisory, and it runs before anything else in connect(), so nothing in here may
        # be allowed to fail the connection.
        try:
            from homeassistant.components.bluetooth import async_last_service_info

            service_info = async_last_service_info(self._hass, self._ble_address, connectable=True)
            rssi = getattr(service_info, "rssi", None) if service_info is not None else None
            if rssi is None:
                return

            if rssi <= self.WEAK_RSSI_DBM:
                self._logger.warning(
                    "Bluetooth signal for %s is weak (%d dBm). The initial config download streams "
                    "the whole node database and may be slow or time out at this signal level. "
                    "Consider moving the node or the adapter closer, or using a bluetooth proxy.",
                    self._ble_address,
                    rssi,
                )
            else:
                self._logger.debug("Bluetooth signal for %s is %d dBm", self._ble_address, rssi)
        except Exception:  # noqa: BLE001
            self._logger.debug("Could not read bluetooth signal strength", exc_info=True)

    async def _open_transport(self, started: float) -> None:
        """Open the underlying transport, reporting which stage failed and after how long."""
        loop = asyncio.get_running_loop()
        self._logger.info(
            "Connecting to meshtastic device over %s (transport timeout %.0fs)",
            self._connection_type,
            self.CONNECT_TIMEOUT,
        )
        try:
            await asyncio.wait_for(self._interface.start(), timeout=self.CONNECT_TIMEOUT)
        except asyncio.CancelledError:
            self._logger.warning("Connect cancelled while opening the transport after %.1fs", loop.time() - started)
            await self._stop_interface_quietly()
            raise
        except TimeoutError as e:
            self._logger.warning("Opening the transport timed out after %.0fs", self.CONNECT_TIMEOUT)
            await self._stop_interface_quietly()
            raise MeshtasticApiClientCommunicationError from e
        except Exception as e:
            self._logger.warning(
                "Opening the transport failed after %.1fs: %s: %s", loop.time() - started, type(e).__name__, e
            )
            await self._stop_interface_quietly()
            raise MeshtasticApiClientCommunicationError from e

    async def _download_config(self, connected_at: float) -> None:
        """Wait for the radio to finish streaming its configuration."""
        loop = asyncio.get_running_loop()
        try:
            ready = await asyncio.wait_for(self._interface.connected_node_ready(), timeout=self._config_timeout)
            exception = None
        except asyncio.CancelledError:
            # Home Assistant cancels setup / config flow steps on shutdown and on timeout. Tearing
            # the interface down here is what keeps the radio from being left in a half-open state
            # that the next connection attempt cannot recover from.
            self._logger.warning(
                "Connect cancelled while downloading the radio config after %.1fs", loop.time() - connected_at
            )
            await self._stop_interface_quietly()
            raise
        except TimeoutError as e:
            self._logger.warning(
                "Radio config did not complete within %.0fs - see the 'Still downloading config' "
                "lines above to tell a slow transfer from a silent radio",
                self._config_timeout,
            )
            ready = False
            exception = e
        except Exception as e:  # noqa: BLE001
            self._logger.warning("Radio config failed: %s: %s", type(e).__name__, e)
            ready = False
            exception = e

        if not ready:
            await self._stop_interface_quietly()
            if exception:
                raise MeshtasticApiClientConfigError from exception
            raise MeshtasticApiClientConfigError

    async def connect(self) -> None:
        # Each stage is announced with its own timing, so a failure says which stage it failed
        # in. Previously a connect that died anywhere in here produced a single line at most.
        loop = asyncio.get_running_loop()
        started = loop.time()
        self._log_bluetooth_signal()

        await self._open_transport(started)

        connected_at = loop.time()
        self._logger.info(
            "Transport ready after %.1fs, waiting up to %.0fs for the radio config",
            connected_at - started,
            self._config_timeout,
        )
        await self._download_config(connected_at)

        self._logger.info(
            "Connected to meshtastic device in %.1fs (%d nodes known)",
            loop.time() - started,
            len(self._interface.nodes()),
        )

        self._packet_processor = asyncio.create_task(self._process_meshtastic_packet())

        async def send_time() -> None:
            await asyncio.sleep(1)
            try:
                await self._interface.send_time()
                await self._interface.write_timezone_if_needed()
            except:  # noqa: E722
                self._logger.debug("Send time failed", exc_info=True)

        self._add_background_task(send_time())

    async def _stop_interface_quietly(self) -> None:
        """
        Tear the interface down without letting failures mask the original error.

        The teardown runs in its own task and is shielded, because the common way to get here is
        our caller being cancelled: awaiting directly would raise `CancelledError` before
        `stop()` ever ran, and the radio would be left with a half-open connection that the next
        connect attempt cannot recover from.
        """
        task = asyncio.ensure_future(self._interface.stop())
        # Consume any failure so a cancelled caller does not leave an unretrieved exception behind.
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            self._logger.debug("Cancelled while stopping interface, teardown continues in background")
            raise
        except Exception:  # noqa: BLE001
            self._logger.debug("Failed to stop interface", exc_info=True)

    async def _await_ready(self) -> bool:
        """Wait (bounded) for the radio config to be available."""
        try:
            return await asyncio.wait_for(self._interface.connected_node_ready(), timeout=self.READY_TIMEOUT)
        except TimeoutError:
            self._logger.debug("Timed out waiting for connected node to become ready")
            return False

    async def disconnect(self) -> None:
        # Stop the interface first: it closes the packet stream listeners, which lets our own
        # packet processor finish its `async for` normally instead of being cancelled while
        # suspended inside an async generator.
        stop_error: Exception | None = None
        try:
            await self._interface.stop()
        except Exception as e:  # noqa: BLE001
            stop_error = e

        # Previously these were left running: the processor was cancelled but never awaited, and
        # `send_time` was not cancelled at all, which Home Assistant reported at shutdown as
        # "Task was destroyed but it is pending".
        pending = [t for t in (self._packet_processor, *self._background_tasks) if t is not None and not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=self.DISCONNECT_TIMEOUT)
        self._packet_processor = None
        self._background_tasks.clear()

        if stop_error is not None:
            raise MeshtasticApiClientCommunicationError from stop_error

    async def async_get_channels(self) -> list[Mapping[str, Any]]:
        if not await self._await_ready():
            return []
        return [self._message_to_dict(c) for c in self._interface.connected_node_channels() or []]

    async def async_get_node_local_config(self) -> dict:
        if not await self._await_ready():
            return {}
        return self._message_to_dict(self._interface.connected_node_local_config())

    async def async_get_node_module_config(self) -> dict:
        if not await self._await_ready():
            return {}
        return self._message_to_dict(self._interface.connected_node_module_config())

    async def async_get_own_node(self) -> Mapping[str, Any]:
        if not await self._await_ready():
            return {}
        return self.get_own_node()

    def get_own_node(self) -> Mapping[str, Any]:
        return self._interface.connected_node() or {}

    def get_node_info(self, node_id: int) -> MeshNode | None:
        return self._interface.find_node(node_id=node_id)

    async def async_get_all_nodes(self) -> Mapping[int, Mapping[str, Any]]:
        await self._await_ready()
        return {node_id: self._transform_node_info(node_info) for node_id, node_info in self._interface.nodes().items()}

    def _transform_node_info(self, node_info: Mapping[str, Any]) -> Mapping[str, Any]:
        transformed = deepcopy(node_info)
        if "position" in transformed:
            self._modify_position(transformed["position"])

        return transformed

    async def send_text(
        self,
        text: str,
        destination_id: int | str = MeshInterface.BROADCAST_ADDR,
        *,
        want_ack: bool = False,
        channel_index: int | None = None,
    ) -> bool:
        try:
            await asyncio.wait_for(
                self._interface.send_text_message(
                    text,
                    destination=destination_id,
                    want_ack=want_ack,
                    channel_index=channel_index,
                ),
                timeout=30,
            )
        except TimeoutError:
            return False
        except Exception as e:
            raise MeshtasticApiClientError from e
        else:
            return True

    @property
    def metadata(self) -> Mapping[str, Any]:
        metadata = self._interface.connected_node_metadata()
        return MessageToDict(metadata) if metadata is not None else {}

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.disconnect()

    def _build_event_data(self, node_id: int, data: Mapping[str, Any]) -> MutableMapping[str, Any]:
        return {
            ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID: self._config_entry_id,
            ATTR_EVENT_MESHTASTIC_API_NODE: node_id,
            ATTR_EVENT_MESHTASTIC_API_DATA: data,
        }

    async def _on_node_info(self, node: MeshNode, info: dict[str, Any]) -> None:
        event_data = self._build_event_data(node.id, info)
        position = event_data.get(ATTR_EVENT_MESHTASTIC_API_DATA, {}).get("position", {})
        if position:
            self._modify_position(position)

        self._hass.bus.async_fire(EVENT_MESHTASTIC_API_NODE_UPDATED, event_data)

    async def _on_text_message(self, node: MeshNode, packet: Packet) -> None:
        if packet.to_id == MeshInterface.BROADCAST_NUM:
            to_channel = packet.channel_index
            to_node = None
        else:
            to_channel = None
            to_node = packet.to_id

        event_data = self._build_event_data(
            node.id,
            {
                "from": packet.from_id,
                "to": {"node": to_node, "channel": to_channel},
                "gateway": self.get_own_node()["num"],
                "message": packet.app_payload,
                "snr": packet.rx_snr,
                "rssi": packet.rx_rssi,
            },
        )

        # Recupera il nome del canale
        channels = self._interface.connected_node_channels()
        channel_info = None
        if isinstance(channels, list):
            for c in channels:
                if hasattr(c, "get") and c.get("index") == packet.channel_index:
                    channel_info = c
                    break
        channel_name = channel_info.get("name") if channel_info else None

        event_data.update({
            "hop_count": packet.mesh_packet.hop_limit,
            "channel_id": packet.channel_index,
            "channel_name": channel_name
        })
        event_data["message_id"] = packet.mesh_packet.id
        self._hass.bus.async_fire(EVENT_MESHTASTIC_API_TEXT_MESSAGE, event_data)

    async def _on_telemetry(self, node: MeshNode, telemetry: dict[str, Any]) -> None:
        device_metrics = telemetry.get("deviceMetrics")
        local_stats = telemetry.get("localStats")
        environment_metrics = telemetry.get("environmentMetrics")
        power_metrics = telemetry.get("powerMetrics")

        node_info = {"name": node.long_name}
        if device_metrics:
            event_data = self._build_event_data(node.id, device_metrics)
            event_data[ATTR_EVENT_MESHTASTIC_API_NODE_INFO] = node_info
            event_data[ATTR_EVENT_MESHTASTIC_API_TELEMETRY_TYPE] = EventMeshtasticApiTelemetryType.DEVICE_METRICS
            self._hass.bus.async_fire(EVENT_MESHTASTIC_API_TELEMETRY, event_data)

        if local_stats:
            event_data = self._build_event_data(node.id, local_stats)
            event_data[ATTR_EVENT_MESHTASTIC_API_NODE_INFO] = node_info
            event_data[ATTR_EVENT_MESHTASTIC_API_TELEMETRY_TYPE] = EventMeshtasticApiTelemetryType.LOCAL_STATS
            self._hass.bus.async_fire(EVENT_MESHTASTIC_API_TELEMETRY, event_data)

        if environment_metrics:
            event_data = self._build_event_data(node.id, environment_metrics)
            event_data[ATTR_EVENT_MESHTASTIC_API_NODE_INFO] = node_info
            event_data[ATTR_EVENT_MESHTASTIC_API_TELEMETRY_TYPE] = EventMeshtasticApiTelemetryType.ENVIRONMENT_METRICS
            self._hass.bus.async_fire(EVENT_MESHTASTIC_API_TELEMETRY, event_data)

        if power_metrics:
            event_data = self._build_event_data(node.id, power_metrics)
            event_data[ATTR_EVENT_MESHTASTIC_API_NODE_INFO] = node_info
            event_data[ATTR_EVENT_MESHTASTIC_API_TELEMETRY_TYPE] = EventMeshtasticApiTelemetryType.POWER_METRICS
            self._hass.bus.async_fire(EVENT_MESHTASTIC_API_TELEMETRY, event_data)

    async def _on_position(self, node: MeshNode, position: dict[str, Any]) -> None:
        self._modify_position(position)

        event_data = self._build_event_data(node.id, position)
        node_info = {"name": node.long_name}
        event_data[ATTR_EVENT_MESHTASTIC_API_NODE_INFO] = node_info
        self._hass.bus.async_fire(EVENT_MESHTASTIC_API_POSITION, event_data)

    def _modify_position(self, position: dict[str, Any]) -> None:
        if "latitudeI" in position:
            position["latitude"] = float(position["latitudeI"] * 10**-7)
        if "longitudeI" in position:
            position["longitude"] = float(position["longitudeI"] * 10**-7)

    async def _process_meshtastic_packet(self) -> None:
        async for packet in self._interface.packet_stream():
            try:
                packet_clone = google.protobuf.json_format.MessageToDict(packet)
                node_id = packet_clone["from"]
                self._hass.bus.async_fire(EVENT_MESHTASTIC_API_PACKET, self._build_event_data(node_id, packet_clone))
            except:  # noqa: E722
                self._logger.warning("Failed to process packet %s", packet, exc_info=True)

    def _add_background_task(self, coro: Coroutine[Any, Any, None], name: str | None = None) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _message_to_dict(self, message: Message) -> Mapping[str, Any]:
        try:
            return MessageToDict(message, always_print_fields_with_no_presence=True)
        except TypeError:
            # older protobuf version
            return MessageToDict(message, including_default_value_fields=True)

    async def request_telemetry(self, node: int, telemetry_type: TelemetryType) -> Mapping[str, Any]:
        try:
            response = await self._interface.request_telemetry(node, telemetry_type=telemetry_type)
            return self._message_to_dict(response)
        except MeshRoutingError as e:
            msg = f"No response for {telemetry_type}"
            raise MeshtasticApiClientError(msg) from e
        except MeshtasticError as e:
            raise MeshtasticApiClientError(str(e)) from e

    async def request_position(self, node: int) -> Mapping[str, Any]:
        try:
            response = await self._interface.request_position(node)
            return self._message_to_dict(response)
        except MeshtasticError as e:
            raise MeshtasticApiClientError(str(e)) from e

    async def request_traceroute(self, node: int) -> Mapping[str, Any]:
        try:
            response = await self._interface.request_traceroute(node)
            return self._message_to_dict(response)
        except MeshtasticError as e:
            raise MeshtasticApiClientError(str(e)) from e
