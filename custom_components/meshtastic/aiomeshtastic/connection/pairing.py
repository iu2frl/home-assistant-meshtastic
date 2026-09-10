# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

"""
BlueZ pairing agent, so a PIN-protected Meshtastic node can be bonded from within the integration.

Background: Meshtastic firmware defaults to `bluetooth.mode = RANDOM_PIN` and requires a bonded,
encrypted link before the fromRadio/toRadio characteristics can be used. BlueZ delegates the
"what is the passkey?" question to whichever `org.bluez.Agent1` is registered as the *default
agent* on the system bus. Neither bleak (as of 3.x, whose `pair()` simply calls BlueZ's `Pair()`
and has no way to supply a passkey - see https://github.com/hbldh/bleak/issues/1434) nor Home
Assistant's Bluetooth stack registers such an agent. Without one, BlueZ has nobody to ask and
pairing fails, which is why bonding previously had to be done by hand with `bluetoothctl`.

This module registers a minimal agent that answers with a configured PIN, and marks the device
trusted so that later reconnects do not need an agent at all.

Everything here is best-effort: if the system D-Bus is not reachable (a Bluetooth proxy rather
than a local adapter, a container without the D-Bus socket mounted, a restrictive D-Bus policy),
the caller carries on unchanged and an already-bonded device keeps working.
"""

# NOTE: deliberately no `from __future__ import annotations` in this module. The agent's D-Bus
# signatures are declared as plain string annotations ("o", "u", ...) which dbus-fast reads at
# class-definition time. Under PEP 563 those become *quoted* strings that dbus-fast can only
# recover by evaluating them in the defining module's globals - which does not work here,
# because the dbus-fast names are imported lazily inside functions rather than at module level.
import asyncio
import contextlib
import functools
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

LOGGER = logging.getLogger(__package__)
_LOGGER = LOGGER.getChild("pairing")

BLUEZ_SERVICE = "org.bluez"
BLUEZ_ROOT_PATH = "/org/bluez"
AGENT_MANAGER_INTERFACE = "org.bluez.AgentManager1"
AGENT_INTERFACE = "org.bluez.Agent1"
DEVICE_INTERFACE = "org.bluez.Device1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

# "KeyboardDisplay" tells BlueZ we can both enter a passkey and show one. Meshtastic shows the
# PIN on the node's screen and expects the central to type it, which reaches us as
# `RequestPasskey`; declaring Display as well keeps us usable with NO_PIN and confirm-only flows.
AGENT_CAPABILITY = "KeyboardDisplay"

# Bonding involves user-paced steps on the node, so allow noticeably longer than a plain connect.
PAIR_TIMEOUT = 45.0


class PairingUnavailableError(Exception):
    """Raised when a BlueZ pairing agent cannot be set up on this system."""


def _import_dbus():  # noqa: ANN202
    """
    Import dbus_fast lazily.

    It ships with Home Assistant's `bluetooth` integration rather than with us, so it must not be
    a hard import: the TCP and serial connection types have to keep working without it.
    """
    try:
        from dbus_fast import BusType, Variant
        from dbus_fast.aio import MessageBus
        from dbus_fast.service import ServiceInterface, dbus_method
    except ImportError as e:  # pragma: no cover - depends on host packages
        msg = "dbus-fast is not available, cannot register a BlueZ pairing agent"
        raise PairingUnavailableError(msg) from e
    return BusType, Variant, MessageBus, ServiceInterface, dbus_method


def normalise_pin(pin: str | int | None) -> str | None:
    """
    Return the PIN as the digit string BlueZ expects, or None if there is nothing usable.

    Accepts an int so a PIN read straight out of the radio's `fixed_pin` config works, and
    tolerates the spaces/dashes people paste in from a device screen.
    """
    if pin is None:
        return None
    text = str(pin).strip().replace(" ", "").replace("-", "")
    if not text:
        return None
    if not text.isdigit():
        msg = "Bluetooth PIN must contain digits only"
        raise ValueError(msg)
    return text


@functools.lru_cache(maxsize=1)
def _build_agent_class():  # noqa: ANN202
    """Define the agent class against the lazily imported dbus_fast base class."""
    _bus_type, _variant, _message_bus, service_interface, dbus_method = _import_dbus()

    class PairingAgent(service_interface):
        """Minimal `org.bluez.Agent1` that answers passkey requests with a fixed PIN."""

        def __init__(self, pin: str) -> None:
            super().__init__(AGENT_INTERFACE)
            self._pin = pin
            self._logger = _LOGGER.getChild("agent")
            # Set once BlueZ actually asks us something, so the caller can tell "we supplied the
            # PIN" apart from "BlueZ never consulted us" (i.e. some other agent answered).
            self.was_consulted = asyncio.Event()

        @dbus_method()
        def Release(self) -> None:  # noqa: N802
            self._logger.debug("Pairing agent released by BlueZ")

        @dbus_method()
        def RequestPasskey(self, device: "o") -> "u":  # noqa: F821, N802
            """BLE numeric passkey (0-999999). This is the Meshtastic RANDOM_PIN/FIXED_PIN path."""
            self._logger.debug("BlueZ requested passkey for %s", device)
            self.was_consulted.set()
            return int(self._pin)

        @dbus_method()
        def RequestPinCode(self, device: "o") -> "s":  # noqa: F821, N802
            """Legacy BR/EDR PIN. Meshtastic does not use it, but answering costs nothing."""
            self._logger.debug("BlueZ requested pin code for %s", device)
            self.was_consulted.set()
            return self._pin

        @dbus_method()
        def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # noqa: F821, N802
            """
            Numeric-comparison pairing: accept only when the passkey matches the configured PIN.

            Blindly confirming here would let any device that happens to be pairing at the same
            moment bond with the adapter, so a mismatch is refused.
            """
            self.was_consulted.set()
            if f"{passkey:06d}" != self._pin.zfill(6):
                self._logger.warning("Refusing pairing confirmation for %s: passkey mismatch", device)
                msg = "Passkey does not match the configured PIN"
                raise _rejected(msg)
            self._logger.debug("Confirmed passkey for %s", device)

        @dbus_method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:  # noqa: F821, N802
            self._logger.debug("BlueZ asked to display passkey %06d for %s (entered %s)", passkey, device, entered)

        @dbus_method()
        def DisplayPinCode(self, device: "o", pincode: "s") -> None:  # noqa: ARG002, F821, N802
            self._logger.debug("BlueZ asked to display pin code for %s", device)

        @dbus_method()
        def RequestAuthorization(self, device: "o") -> None:  # noqa: F821, N802
            self._logger.debug("Authorising pairing for %s", device)
            self.was_consulted.set()

        @dbus_method()
        def AuthorizeService(self, device: "o", uuid: "s") -> None:  # noqa: F821, N802
            self._logger.debug("Authorising service %s for %s", uuid, device)

        @dbus_method()
        def Cancel(self) -> None:  # noqa: N802
            self._logger.debug("BlueZ cancelled the pairing request")

    return PairingAgent


def _rejected(message: str) -> Exception:
    from dbus_fast import DBusError

    return DBusError("org.bluez.Error.Rejected", message)


def device_path(adapter: str, address: str) -> str:
    """Build the BlueZ object path for a device, e.g. /org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF."""
    return f"{BLUEZ_ROOT_PATH}/{adapter}/dev_{address.upper().replace(':', '_')}"


@asynccontextmanager
async def pairing_agent(pin: str) -> AsyncIterator[object]:
    """
    Register a default BlueZ pairing agent for the duration of the context.

    Raises `PairingUnavailableError` when no agent can be registered, so the caller can fall back
    to attempting an unauthenticated connection.
    """
    bus_type, _variant, message_bus, _si, _dm = _import_dbus()
    agent = _build_agent_class()(pin)
    # Unique per process so two Home Assistant instances (or two gateways) cannot collide.
    path = f"/org/meshtastic/ha/agent/{os.getpid()}_{id(agent):x}"

    try:
        bus = await message_bus(bus_type=bus_type.SYSTEM).connect()
    except Exception as e:
        msg = f"Could not connect to the system D-Bus: {e}"
        raise PairingUnavailableError(msg) from e

    # Setup only. Exceptions raised by the caller's `async with` body must NOT be caught here:
    # doing so reported a failed pair() as "Could not register a BlueZ pairing agent", which sent
    # debugging in entirely the wrong direction.
    registered = False
    manager = None
    try:
        bus.export(path, agent)
        introspection = await bus.introspect(BLUEZ_SERVICE, BLUEZ_ROOT_PATH)
        proxy = bus.get_proxy_object(BLUEZ_SERVICE, BLUEZ_ROOT_PATH, introspection)
        manager = proxy.get_interface(AGENT_MANAGER_INTERFACE)

        await manager.call_register_agent(path, AGENT_CAPABILITY)
        registered = True
        _LOGGER.debug("Registered BlueZ pairing agent at %s", path)

        # BlueZ routes the passkey question to the *default* agent, and bleak calls Pair() from
        # its own D-Bus connection rather than ours, so simply registering is not enough.
        try:
            await manager.call_request_default_agent(path)
        except Exception as e:  # noqa: BLE001
            # Typically another agent (an interactive `bluetoothctl`) already holds the default
            # slot. Pairing may still work if that agent answers, so this is not fatal.
            _LOGGER.warning(
                "Could not become the default BlueZ pairing agent (%s). "
                "If pairing fails, close any interactive bluetoothctl session and retry.",
                e,
            )
    except PairingUnavailableError:
        await _release_agent(bus, manager, path, agent, registered=registered)
        raise
    except Exception as e:
        await _release_agent(bus, manager, path, agent, registered=registered)
        msg = f"Could not register a BlueZ pairing agent: {e}"
        raise PairingUnavailableError(msg) from e

    try:
        yield agent
    finally:
        await _release_agent(bus, manager, path, agent, registered=registered)


async def _release_agent(bus: object, manager: object, path: str, agent: object, *, registered: bool) -> None:
    """Unregister the agent and drop the bus connection, ignoring teardown failures."""
    if registered and manager is not None:
        with contextlib.suppress(Exception):
            await manager.call_unregister_agent(path)
    with contextlib.suppress(Exception):
        bus.unexport(path, agent)
    with contextlib.suppress(Exception):
        bus.disconnect()


@asynccontextmanager
async def _system_bus() -> AsyncIterator[object]:
    """Connect to the system bus for the duration of the context."""
    bus_type, _variant, message_bus, _si, _dm = _import_dbus()
    bus = await message_bus(bus_type=bus_type.SYSTEM).connect()
    try:
        yield bus
    finally:
        with contextlib.suppress(Exception):
            bus.disconnect()


async def _find_device(bus: object, address: str) -> tuple[str, dict] | None:
    """
    Locate a device known to BlueZ by address, returning its object path and properties.

    Asking the ObjectManager rather than assuming an adapter name means this keeps working on
    hosts with more than one adapter, or where the Meshtastic node is not on hci0.
    """
    introspection = await bus.introspect(BLUEZ_SERVICE, "/")
    proxy = bus.get_proxy_object(BLUEZ_SERVICE, "/", introspection)
    object_manager = proxy.get_interface("org.freedesktop.DBus.ObjectManager")
    wanted = address.upper()
    for path, interfaces in (await object_manager.call_get_managed_objects()).items():
        device = interfaces.get(DEVICE_INTERFACE)
        if device is None:
            continue
        found = device.get("Address")
        if found is not None and str(found.value).upper() == wanted:
            return path, device
    return None


async def async_get_device_state(address: str) -> dict[str, bool] | None:
    """
    Report BlueZ's bonding state for a device, or None if it cannot be determined.

    Used for diagnostics: "Paired" is the bond that has to survive restarts, and "Trusted" is
    what lets the device reconnect without a pairing agent being present.
    """
    try:
        async with _system_bus() as bus:
            found = await _find_device(bus, address)
            if found is None:
                return None
            _path, device = found
            return {
                key: bool(device[key].value) if key in device else False
                for key in ("Paired", "Bonded", "Trusted", "Connected")
            }
    except PairingUnavailableError:
        return None
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not read bluetooth state for %s", address, exc_info=True)
        return None


async def async_set_device_trusted(address: str) -> bool:
    """
    Mark a bonded device trusted, the equivalent of `bluetoothctl trust`.

    A trusted device may reconnect and use its services without an agent being present, which is
    what lets a bond survive a Home Assistant restart unattended. Returns True on success.
    """
    try:
        _bus_type, variant, _message_bus, _si, _dm = _import_dbus()
        async with _system_bus() as bus:
            found = await _find_device(bus, address)
            if found is None:
                _LOGGER.debug("Device %s is not known to BlueZ, cannot set trust", address)
                return False
            path, device = found
            if "Trusted" in device and device["Trusted"].value:
                _LOGGER.debug("Device %s is already trusted", address)
                return True

            introspection = await bus.introspect(BLUEZ_SERVICE, path)
            proxy = bus.get_proxy_object(BLUEZ_SERVICE, path, introspection)
            properties = proxy.get_interface(PROPERTIES_INTERFACE)
            await properties.call_set(DEVICE_INTERFACE, "Trusted", variant("b", True))  # noqa: FBT003
    except PairingUnavailableError:
        return False
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not mark %s as trusted", address, exc_info=True)
        return False
    else:
        _LOGGER.info("Marked bluetooth device %s as trusted for unattended reconnects", address)
        return True
