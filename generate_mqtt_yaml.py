from pathlib import Path


BURIED_FIELDS = [
    ("Battery", "battery", "bat", "voltage", "measurement", "V", "bat"),
    ("Temperature", "temperature", "temp", "temperature", "measurement", "°C", "temp"),
    ("Humidity", "humidity", "humi", "humidity", "measurement", "%", "humi"),
    ("pH", "ph", "ph", None, "measurement", "pH", "PH"),
    ("Sleep", "sleep", "sleep", None, "measurement", "s", "SLEEP"),
    ("Count", "count", "count", None, "total_increasing", None, "COUNT"),
    ("RSSI", "rssi", "rssi", "signal_strength", "measurement", "dBm", "rssi"),
    ("Last Seen", "last_seen", "last_seen", "timestamp", None, None, "ts_iso"),
]

ABOVE_GROUND_FIELDS = [
    ("Battery", "battery", "bat", "voltage", "measurement", "V", "bat"),
    ("Temperature", "temperature", "temp", "temperature", "measurement", "°C", "temp"),
    ("Humidity", "humidity", "humi", "humidity", "measurement", "%", "humi"),
    ("eCO2", "eco2", "eco2", "carbon_dioxide", "measurement", "ppm", "eco2"),
    ("Illuminance", "illuminance", "lux", "illuminance", "measurement", "lx", "lux"),
    ("Sleep", "sleep", "sleep", None, "measurement", "s", "SLEEP"),
    ("Count", "count", "count", None, "total_increasing", None, "COUNT"),
    ("RSSI", "rssi", "rssi", "signal_strength", "measurement", "dBm", "rssi"),
    ("Last Seen", "last_seen", "last_seen", "timestamp", None, None, "ts_iso"),
]

NODES = [
    ("Aqia01", "aqia01", "LoRa Sensor Node", BURIED_FIELDS),
    ("Aqua02", "aqua02", "LoRa Sensor Node", BURIED_FIELDS),
    ("Aqua03", "aqua03", "LoRa Sensor Node", BURIED_FIELDS),
    ("Aqua04", "aqua04", "LoRa Sensor Node", BURIED_FIELDS),
    ("Aqua05", "aqua05", "LoRa Sensor Node", BURIED_FIELDS),
    ("Aqua06", "aqua06", "LoRa Sensor Node", BURIED_FIELDS),
    ("Aqua07", "aqua07", "LoRa Sensor Node", BURIED_FIELDS),
    ("Aqua08", "aqua08", "LoRa Sensor Node", BURIED_FIELDS),
    ("Aqua09", "aqua09", "LoRa Sensor Node", BURIED_FIELDS),
    ("Aqua010", "aqua010", "LoRa Sensor Node", BURIED_FIELDS),
    ("Aqua011", "aqua011", "LoRa Sensor Node", BURIED_FIELDS),
    ("Aqua012", "aqua12", "LoRa Sensor Node", BURIED_FIELDS),
    ("Aqua013", "aqua013", "LoRa Sensor Node", BURIED_FIELDS),
    ("Aqua101", "aqua101", "LoRa Above Ground Sensor Node", ABOVE_GROUND_FIELDS),
]

GATEWAY_ID = "pi-zero2-gw01"
GATEWAY_STATE_TOPIC = f"lora/_gateway/{GATEWAY_ID}/state"
GATEWAY_ENTITY_PREFIX = "lora_gateway_pi_zero2_gw01_pi_zero2_gw01"
GATEWAY_BINARY_SENSORS = [
    ("MQTT Connected", "mqtt_connected", "mqtt_connected"),
    ("Serial OK", "serial_ok", "serial_ok"),
]
GATEWAY_SENSORS = [
    ("Heartbeat", "heartbeat", "heartbeat_iso", "timestamp", None, None),
    ("Uptime", "uptime", "uptime_s", None, "measurement", "s"),
    ("IP", "ip", "ip", None, None, None),
    ("Last Serial RX", "last_serial_rx", "last_serial_rx_iso", "timestamp", None, None),
    ("Nodes Seen", "nodes_seen", "nodes_seen", None, "measurement", None),
    ("Last Serial Error", "last_serial_error", "last_serial_error", None, None, None),
]


def build_yaml() -> str:
    lines = [
        "# Manual Home Assistant MQTT entities for Xtatech LoRa Bridge.",
        "# The bridge does not update this file, and MQTT discovery should stay disabled.",
        "#",
        "# Put this file in your Home Assistant config folder and include it from",
        "# configuration.yaml like this:",
        "#",
        "# mqtt: !include mqtt.yaml",
        "#",
        "# State topics match the bridge default:",
        "#   lora/<node_id>/state",
        "",
        "binary_sensor:",
    ]

    append_gateway_binary_sensors(lines)
    lines.extend([
        "",
        "sensor:",
    ])
    append_gateway_sensors(lines)
    lines.append("")

    for node_index, (node, entity_node, model, fields) in enumerate(NODES):
        node_slug = node.lower()
        entity_slug = entity_node.lower()
        prefix = f"lora_{entity_slug}_{entity_slug}"
        anchor = f"device_{node_slug}"
        if node_index:
            lines.append("")

        for field_index, (label, object_suffix, unique_suffix, devcls, stcls, unit, json_key) in enumerate(fields):
            lines.append(f'  - name: "{node} {label}"')
            lines.append(f'    unique_id: "lora_{node_slug}_{unique_suffix}"')
            lines.append(f'    default_entity_id: "sensor.{prefix}_{object_suffix}"')
            lines.append(f'    state_topic: "lora/{node}/state"')
            lines.append(f'    value_template: "{{{{ value_json.{json_key} | default(None) }}}}"')
            lines.append("    force_update: true")
            if devcls:
                lines.append(f"    device_class: {devcls}")
            if stcls:
                lines.append(f"    state_class: {stcls}")
            if unit:
                lines.append(f'    unit_of_measurement: "{unit}"')

            if field_index == 0:
                lines.append(f"    device: &{anchor}")
                lines.append("      identifiers:")
                lines.append(f'        - "lora_{node_slug}"')
                lines.append(f'      name: "LoRa {node}"')
                lines.append('      manufacturer: "Xtatech"')
                lines.append(f'      model: "{model}"')
            else:
                lines.append(f"    device: *{anchor}")

    return "\n".join(lines) + "\n"


def append_gateway_device(lines: list[str]) -> None:
    lines.append("    device: &device_gateway_pi_zero2_gw01")
    lines.append("      identifiers:")
    lines.append(f'        - "lora_gateway_{GATEWAY_ID}"')
    lines.append(f'      name: "LoRa Gateway {GATEWAY_ID}"')
    lines.append('      manufacturer: "Xtatech"')
    lines.append('      model: "Raspberry Pi Gateway"')


def append_gateway_binary_sensors(lines: list[str]) -> None:
    for index, (label, suffix, json_key) in enumerate(GATEWAY_BINARY_SENSORS):
        lines.append(f'  - name: "{GATEWAY_ID} {label}"')
        lines.append(f'    unique_id: "lora_gateway_{GATEWAY_ID}_{suffix}"')
        lines.append(f'    default_entity_id: "binary_sensor.{GATEWAY_ENTITY_PREFIX}_{suffix}"')
        lines.append(f'    state_topic: "{GATEWAY_STATE_TOPIC}"')
        lines.append(f'    value_template: "{{{{ \'ON\' if value_json.{json_key} | default(false) else \'OFF\' }}}}"')
        if index == 0:
            append_gateway_device(lines)
        else:
            lines.append("    device: *device_gateway_pi_zero2_gw01")


def append_gateway_sensors(lines: list[str]) -> None:
    for label, suffix, json_key, devcls, stcls, unit in GATEWAY_SENSORS:
        lines.append(f'  - name: "{GATEWAY_ID} {label}"')
        lines.append(f'    unique_id: "lora_gateway_{GATEWAY_ID}_{suffix}"')
        lines.append(f'    default_entity_id: "sensor.{GATEWAY_ENTITY_PREFIX}_{suffix}"')
        lines.append(f'    state_topic: "{GATEWAY_STATE_TOPIC}"')
        lines.append(f'    value_template: "{{{{ value_json.{json_key} | default(None) }}}}"')
        if devcls:
            lines.append(f"    device_class: {devcls}")
        if stcls:
            lines.append(f"    state_class: {stcls}")
        if unit:
            lines.append(f'    unit_of_measurement: "{unit}"')
        lines.append("    device: *device_gateway_pi_zero2_gw01")


if __name__ == "__main__":
    Path("mqtt.yaml").write_text(build_yaml(), encoding="utf-8")
