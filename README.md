<!--
SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
SPDX-FileCopyrightText: 2025 Ylian Saint-Hilaire @ylianst

SPDX-License-Identifier: MIT
-->

# Patched Home-Assistant Meshtastic Integration

[![GitHub Release][releases-shield]][releases]
[![License][license-shield]](LICENSE)

[![hacs](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://hacs.xyz/docs/faq/custom_repositories)
[![HA Analytics](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fanalytics.home-assistant.io%2Fcustom_integrations.json&query=%24.meshtastic.total&style=for-the-badge&label=Active%20Installations&color=red)](https://buymeacoffee.com/broglep)


_Home Assistant Integration for [Meshtastic](https://www.meshtastic.org)._

Supported Features:
 * Add meshtastic devices as gateways to interact with the mesh
   * Supports TCP, Serial & Bluetooth connection (also works with [Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy.html))
   * Home Assistant Zeroconf, Bluetooth & USB-Serial Auto-Discovery 
   * Select which meshtastic nodes should be imported to home assistant
 * Meshtastic node metrics
 * Record received messages
 * Send messages (as direct message or on broadcast channel)
 * Record node position (as device tracker)
 * Device triggers & actions for automations
 * Various other service actions (e.g. request metrics, trace route)
 * Bundled meshtastic web client for manual interaction with gateway
 * MQTT client proxy support (forwards messages from radio to MQTT broker)

For more details, see check the [documentation](#documentation).

## Why the patch?

The original code at [meshtastic/home-assistant](https://github.com/meshtastic/home-assistant) sits abandoned since June 2026, with a lot of warnings and instabilities. I derived this repo from [Gooman-rus/home-assistant-meshtastic](https://github.com/Gooman-rus/home-assistant-meshtastic) which updated the serial library from the old deprecated one.

I will try to mantain this code working and possibly integrating more features.

## Credits

- [meshtastic/home-assistant](https://github.com/meshtastic/home-assistant): Original repository for the Home Assistant integration with Meshtastic.
- [Gooman-rus/home-assistant-meshtastic](https://github.com/Gooman-rus/home-assistant-meshtastic): Updated the serial library from the old deprecated one.

## Installation

> [!NOTE]
> At the moment, I am not interested in having fully automated integration support, manual installation is ok for now.

### Manual

1. Using the tool of choice open the directory (folder) for your HA configuration (where you find `configuration.yaml`).
2. If you do not have a `custom_components` directory (folder) there, you need to create it.
3. In the `custom_components` directory (folder) create a new folder called `homeassistant-meshtastic`.
4. Download _all_ the files from the `custom_components/meshtastic/` directory (folder) in this repository.
5. Place the files you downloaded in the new directory (folder) you created.
6. Restart Home Assistant
7. In the HA UI go to "Configuration" -> "Integrations" click "+" and search for "Meshtastic"
</details>

### Configuration

Once the plugin is installed, the device should be automatically discovered, follow the tutorial to complete the connection.

## Bluetooth pairing (PIN)

Meshtastic firmware ships with `bluetooth.mode = RANDOM_PIN`: the node shows a fresh 6-digit
passkey on its screen each time something tries to bond with it, and it will **not** serve the
`fromRadio` / `toRadio` characteristics over an unauthenticated link. If the node is not bonded,
the connection itself succeeds but every read fails, typically showing up in the log as
`org.bluez.Error.Failed: Failed to send read request` or a setup that never becomes available.

Enter the PIN in the **Bluetooth PIN** field when adding the gateway (or in *Reconfigure* on an
existing one). It is optional — leave it empty if the node uses `NO_PIN`, or if you already
bonded the node on the host.

<details>
<summary>How this works, and why it used to need <code>bluetoothctl</code></summary>

BlueZ never asks the *application* for a passkey. It asks whichever process has registered an
`org.bluez.Agent1` on the system D-Bus as the **default agent**. Neither bleak (its `pair()`
simply calls BlueZ's `Pair()`, see [bleak#1434](https://github.com/hbldh/bleak/issues/1434)) nor
Home Assistant's Bluetooth stack registers such an agent — so with a PIN-protected node BlueZ had
nobody to ask, and pairing could only be done by hand with `bluetoothctl`, which brings its own
agent.

This integration now registers a minimal pairing agent for the duration of the pairing, answers
BlueZ's `RequestPasskey` with your PIN, and then marks the device **trusted** (the equivalent of
`bluetoothctl trust`) so that later reconnects need no agent at all.

Two consequences worth knowing:

* **Close any interactive `bluetoothctl` session** before adding the gateway. Only one default
  agent can exist at a time; if `bluetoothctl` holds it, ours cannot take over and you will see
  `Could not become the default BlueZ pairing agent` in the log.
* **The system D-Bus must be reachable.** With an
  [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) there is no local
  BlueZ at all, and in a hand-rolled Docker setup the socket may not be mounted. In that case the
  integration logs `Cannot supply the bluetooth PIN automatically` and falls back to assuming the
  node is already bonded.

</details>

### Setup times out ("Failed to connect", or a config download that never finishes)

A Meshtastic node accepts **one client connection at a time**. If the phone app, the web client
or an interactive `bluetoothctl` session is connected, Home Assistant can open the BLE link but
the radio will not stream its configuration — setup then fails with a `TimeoutError` waiting for
the node to become ready. Disconnect any other client and retry.

Otherwise the download may simply be slow: the radio sends its whole node database, which on a
large mesh over BLE can take minutes. The integration reports progress while it waits, so the
log distinguishes the two cases:

```
Still downloading config from radio after 10s (0 packets, 0 node infos) - no packets received
    yet, another client may be holding the radio's single connection slot
Still downloading config from radio after 20s (734 packets, 210 node infos)
Config download complete in 41.2s (1180 packets, 337 node infos)
```

Rising counts mean it is working and just needs longer; counts stuck at zero mean the radio is
not talking to us. For the full picture, add this to `configuration.yaml` and restart:

```yaml
logger:
  logs:
    custom_components.meshtastic: debug
```

### What happens when Home Assistant restarts or is updated

Bonding keys live with **`bluetoothd`**, in `/var/lib/bluetooth/<adapter-mac>/<node-mac>/`, not
inside the integration or its config entry. So what survives depends on where `bluetoothd` runs:

| Install method | `bluetoothd` runs on | Bond survives a restart / update? |
| --- | --- | --- |
| Home Assistant OS | the host OS | **Yes** — the Core container is replaced, the host bond store is not |
| Supervised | the host OS | **Yes** |
| Container / Docker (host D-Bus mounted) | the host OS | **Yes** |
| Core in a venv | the host OS | **Yes** |
| A container running its own `bluetoothd` | inside that container | **No** — an image update discards `/var/lib/bluetooth`, so re-pair (mount a volume there to fix this) |

In the normal cases the PIN is therefore needed **once**. After that the bond and the trust flag
persist, and restarts reconnect unattended. The PIN is still kept in the config entry so that
re-pairing is automatic if the bond is ever lost.

A bond *does* get invalidated by: factory-resetting the node, changing its `bluetooth.mode` or
fixed PIN, `bluetoothctl remove`, moving to a different Bluetooth adapter, or clearing
`/var/lib/bluetooth`. With the PIN saved, the integration re-bonds by itself — except under
`RANDOM_PIN`, where the node invents a new passkey and you will have to read it off the screen
and update the setting. **If you want unattended recovery, set the node's Bluetooth mode to
`FIXED_PIN`** and configure that PIN here.

<details>
<summary>Checking and fixing pairing state by hand</summary>

The integration logs the actionable case explicitly (`... is not paired with BlueZ ...`). To
inspect or repair it yourself, on the machine where `bluetoothd` runs:

```bash
bluetoothctl info AA:BB:CC:DD:EE:FF   # look for "Paired: yes" and "Trusted: yes"
```

To start over:

```bash
bluetoothctl remove AA:BB:CC:DD:EE:FF   # drop the bond, then reload the integration
```

Pairing manually (the pre-existing workaround, still valid) — note that `trust` is what makes it
stick across restarts:

```bash
bluetoothctl
[bluetooth]# scan on
[bluetooth]# pair AA:BB:CC:DD:EE:FF     # enter the PIN from the node's screen
[bluetooth]# trust AA:BB:CC:DD:EE:FF
[bluetooth]# quit
```

Then leave the **Bluetooth PIN** field empty, since the bond already exists. Remember to quit
`bluetoothctl`, or its agent will block the integration's own.

</details>

# Documentation

## Supported Platforms
### Sensor / Binary Sensor
For nodes selected as part of the configuration, metrics are exposed as corresponding sensors.

Even though integration is classified as `local_polling` it predominantly operates as `local_push` and home assistant is notified
as soon as new state is available. Polling is only used as fallback should the push mechanism stop working.

### [Notification](https://www.home-assistant.io/integrations/notify/)
Using notification platform and the generated `notify.mesh_*` entities is the recommended way to send messages to the mesh when you don't need control over all the details 
(which gateway to use, acknowledgements, etc.)

Depending on your needs, all nodes from the gateway's node database as well as the channels can be made available as notification targets.


Note: Support is based on new [entity notification platform](https://developers.home-assistant.io/blog/2024/04/10/new-notify-entity-platform/), 
for an example on how to use it, see [official documentation](https://www.home-assistant.io/integrations/notify/#example-with-the-entity-platform-notify-action).

### [Automation](https://www.home-assistant.io/docs/automation/)

For nodes selected as part of the configuration, device triggers and device actions are available.
Prefer using device actions and triggers in your automation over using (service) actions.

#### Triggers
There are triggers when messages have been sent or have been received.
Gateway nodes offer more triggers than other nodes. You can further refine the triggers to only fire when a message was 
sent/received on a particular channel or as direct message.


#### Actions
 * Send Direct Message
 * Request Telemetry
 * Request Position

Note: Ensure you have a delay before the action if your automations are triggered by meshtastic device trigger,
otherwise you risk that your action triggered message is being dropped by meshtastic firmware because
it is still busy with receiving / sending other mesh messages.

#### Examples
<details>
<summary>Reply back after message received from predefined device on arbitrary channel (including direct message) </summary>

```yaml
- id: '1800000042000'
  alias: Ping Sample
  description: 'Reply back after message from device'
  triggers:
  - device_id: e3376b45b4912c27cffb46c58e4998e4
    domain: meshtastic
    type: message.sent
    trigger: device
  actions:
  - delay:
      seconds: 10
  - device_id: e3376b45b4912c27cffb46c58e4998e4
    domain: meshtastic
    type: send_message
    message: PONG {{ trigger.event.data.message }}
```

</details>

<details>
<summary>Echo incoming channel text messages from any node with gateway device trigger</summary>

```yaml
- id: '1735857524502'
  alias: Echo Channel Message
  description: ''
  triggers:
  - domain: meshtastic
    device_id: 16efde6990a6a09903153abb8624fe38
    type: channel_message.received
    entity_id: meshtastic.gateway_brig_channel_primary
    trigger: device
  conditions: []
  actions:
  - delay:
      seconds: 5
  - action: meshtastic.broadcast_channel_message
    metadata: {}
    data:
      ack: true
      channel: meshtastic.gateway_brig_channel_primary
      message: 'ECHO: {{ trigger.event.data.message }}'
  mode: single
```
</details>

<details>
<summary>Advanced: Handling incoming text messages from any node without notification platform and its entities</summary>

```yaml
- id: '1735852176270'
  alias: Echo on Channel Message (without Notify Platform)
  description: 'Only from gateway with node id 3771721320 and channel 0'
  triggers:
  - trigger: event
    event_type: meshtastic_api_text_message
    event_data:
      data:
        to:
          node:
          channel: 0
        gateway: 3771721320
  conditions: []
  actions:
  - delay:
      seconds: 5
  - action: meshtastic.send_text
    data:
      ack: true
      text: 'ECHO: {{ trigger.event.data.data.message }}'
      from: '{{ trigger.event.data.data.gateway }}'
      channel: '{{ trigger.event.data.data.to.channel }}'
  mode: single
- id: '1735852176271'
  alias: Echo on Direct Message (without Notify Platform)
  description: 'Only from gateway with node id 3771721320'
  triggers:
  - trigger: event
    event_type: meshtastic_api_text_message
    event_data:
      data:
        to:
          node: 3771721320
          channel:
        gateway: 3771721320
  conditions: []
  actions:
  - delay:
      seconds: 5
  - action: meshtastic.send_text
    data:
      ack: true
      text: 'ECHO: {{ trigger.event.data.data.message }}'
      from: '{{ trigger.event.data.data.gateway }}'
      to: '{{ trigger.event.data.data.from }}'
  mode: single
```

<details>
<summary>Automatic reply on specific channels</summary>

```yaml
alias: Risposta automatica canale Test
description: Rispondi con timestamp e hop count ai messaggi sul canale Test
triggers:
  - event_type: meshtastic_api_text_message
    event_data:
      channel_name: Test
    trigger: event
actions:
  - data:
      message: >
        🤖 Ricevuto!

        Timestamp: {{ as_timestamp(now()) |
        timestamp_custom('%Y-%m-%dT%H:%M:%S') }}

        Hop count: {{ trigger.event.data.hop_count }}
      destination: '{{ trigger.event.data.data.from }}'
      channel: '{{ trigger.event.data.data.to.channel }}'
    action: meshtastic.send_text
mode: single
```
</details>

If you don't want to use the recommend notification platform for sending messages (e.g. if you don't want to clutter your Home Assistant instance with potentially hundreds of notify mesh entities), 
you can still handle incoming text messages from any public node and reply to these messages. 
This is useful if to want to reply to incoming direct messages with a standard message, use a LLM or handle various commands with automations.

To do this, create a new Home Assistant automation that triggers on "Manual Events" and put `meshtastic_api_text_message` as the "Event Type". This will cause this automation to get triggerred on all incoming channel and direct messages. You will get events that include this information:

```yaml
trigger:
  event:
    event_type: meshtastic_api_text_message
    event_data:
      data:
        from: 1127918844
        to:
          node: null
          channel: 0
        gateway: 862525748
        message: Sample Message
```

From contains the node id of the sender of the message, to will have the node id of the gateway for direct messages, or a gateway channel id if the message is directed at the channel. 
Note that the channel id is dependent on the gateway node, so make sure you are using the proper gateway node when replying using that channel id. 

You can create conditions in the automation to filter out the incoming messages you want or you can directly filter in the trigger.
For example to filter out messages addressed to your gateway node, use this condition with your node id.

```
{{ trigger.event.data.data.to.node == 862525748 }}
```

To filter out messages addresses at the primary channel (Channel 0 is typically LONGFAST), use this condition:

```
{{ trigger.event.data.data.to.channel == 0 }}
```

You can also forward these messages as notifications to your phone, etc. For example:

```
Meshtastic message from ({{ trigger.event.data.data.from }}): {{ trigger.event.data.data.message }}
```

To reply to a text message in this situation, add a 2 second or more delay action and then an action called `Meshtastic 'Send Text'` to your automation. You need to add a short delay to make sure your Meshtastic device is idle before replying. Change the `Meshtastic 'Send Text'` action to edit in yaml and change the `to`, `from` and `text` values to something like his:

```
action: meshtastic.send_text
metadata: {}
data:
  ack: false
  from: "{{ trigger.event.data.data.gateway }}"
  to: "{{ trigger.event.data.data.from }}"
  text: "ECHO: {{ trigger.event.data.data.message }}"
```

In the example above, we echo back an incoming direct message.

</details>

### [Logbook](https://www.home-assistant.io/integrations/logbook/)

Direct messages or channel messages are recorded in the log book. 
Each gateway has an entity for direct messages and its channels, you can navigate to the device and select the entitiy
to see an extract of the logbook, or you can navigate to the logbook and filter for the desired entities there. 

Note: When logbook is not enabled, messages are not recorded.

### [Device Tracker](https://www.home-assistant.io/integrations/device_tracker/)

For nodes selected as part of the configuration, their position is exposed as a device tracker.
You can see the nodes on home assistant map accordingly and home assistant will report if 
node is at home or away.

### Services / [Actions](https://developers.home-assistant.io/blog/2024/07/16/service-actions/)

Use the available actions if you need more control compared to other methods to interact with
meshtastic devices. Certain actions need you to understand meshtastic details and are not recommended 
for the average user. 

## [Meshtastic Web Client](https://meshtastic.org/docs/software/web-client/)

In order to enable manual interaction with gateway nodes and because of the limitation of meshtastic firmware
that only allows one single connection, this integration offers a workaround by exposing a meshtastic client http api
for each configured gateway. The bundled meshtastic web client can then be connected
to this integration directly and the integration will act as a proxy.

This also has the added benefit that it allows you to connect to meshtastic devices via the web client
that are not connected via TCP (like serial or bluetooth) or don't support TCP at all (e.g. nRF based nodes).

**Security Note**: As a side effect, enabling this feature results in unauthenticated access to your gateway nodes for 
anyone that can reach your home assistant instance (because meshtastic does not support authentication on the http api).
Make sure to only use this feature if your home assistant instance is running in a trusted environment!

To access the web client, perform the following steps:

In Home Assistant:
1. Enable the feature in the integration configuration
2. Navigate to the "Meshtastic" menu item. If you don't see it, reload home assistant interface
3. Press the "Open" button of the desired gateway to launch the web client

Inside the Meshtastic Web Client:

4. Press "New Connection" - the correct hostname is already populated
5. Press "Connect"

## Contributions are welcome!

If you want to contribute to this please read the [Contribution guidelines](CONTRIBUTING.md)

***

[commits-shield]: https://img.shields.io/github/commit-activity/y/meshtastic/home-assistant.svg?style=for-the-badge
[commits]: https://github.com/meshtastic/home-assistant/commits/main
[license-shield]: https://img.shields.io/github/license/meshtastic/home-assistant.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/meshtastic/home-assistant.svg?style=for-the-badge
[releases]: https://github.com/meshtastic/home-assistant/releases

