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
    ("aqia01", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua02", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua03", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua04", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua05", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua06", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua07", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua08", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua09", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua010", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua011", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua12", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua013", "LoRa Sensor Node", BURIED_FIELDS),
    ("Aqua101", "LoRa Above Ground Sensor Node", ABOVE_GROUND_FIELDS),
]

GATEWAY_ID = "pi-zero2-gw01"
GATEWAY_STATE_TOPIC = f"lora/_gateway/{GATEWAY_ID}/state"
GATEWAY_ENTITY_PREFIX = "lora_gateway_pi_zero2_gw01_pi_zero2_gw01"
GATEWAY_SENSORS = [
    ("Heartbeat", "heartbeat", "heartbeat_iso", "timestamp", None, None),
    ("Uptime", "uptime", "uptime_s", None, "measurement", "s"),
    ("Nodes Seen", "nodes_seen", "nodes_seen", None, "measurement", None),
]
GATEWAY_BINARY_SENSORS = [
    ("MQTT Connected", "mqtt_connected", "mqtt_connected"),
    ("Serial OK", "serial_ok", "serial_ok"),
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
    ]

    lines.extend(build_gateway_binary_sensors())
    lines.append("")
    lines.append("sensor:")
    lines.extend(build_gateway_sensors())

    for node_index, (node, model, fields) in enumerate(NODES):
        node_lower = node.lower()
        prefix = f"lora_{node_lower}_{node_lower}"
        anchor = f"device_{node_lower}"
        lines.append("")

        for field_index, (label, object_suffix, unique_suffix, devcls, stcls, unit, json_key) in enumerate(fields):
            lines.append(f'  - name: "{node} {label}"')
            lines.append(f'    unique_id: "lora_{node_lower}_{unique_suffix}"')
            lines.append(f'    default_entity_id: "sensor.{prefix}_{object_suffix}"')
            lines.append(f'    state_topic: "lora/{node}/state"')
            lines.append(f'    value_template: "{{{{ value_json.{json_key} | default(None) }}}}"')
            if devcls:
                lines.append(f"    device_class: {devcls}")
            if stcls:
                lines.append(f"    state_class: {stcls}")
            if unit:
                lines.append(f'    unit_of_measurement: "{unit}"')

            if field_index == 0:
                lines.append(f"    device: &{anchor}")
                lines.append("      identifiers:")
                lines.append(f'        - "lora_{node_lower}"')
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


def build_gateway_binary_sensors() -> list[str]:
    lines = [
        "binary_sensor:",
    ]
    for index, (label, suffix, json_key) in enumerate(GATEWAY_BINARY_SENSORS):
        lines.append(f'  - name: "{GATEWAY_ID} {label}"')
        lines.append(f'    unique_id: "lora_gateway_{GATEWAY_ID}_{suffix}"')
        lines.append(f'    default_entity_id: "binary_sensor.{GATEWAY_ENTITY_PREFIX}_{suffix}"')
        lines.append(f'    state_topic: "{GATEWAY_STATE_TOPIC}"')
        lines.append(f'    value_template: "{{{{ iif(value_json.{json_key}, \'ON\', \'OFF\') }}}}"')
        if index == 0:
            append_gateway_device(lines)
        else:
            lines.append("    device: *device_gateway_pi_zero2_gw01")

    return lines


def build_gateway_sensors() -> list[str]:
    lines = []
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

    return lines


if __name__ == "__main__":
    Path("mqtt.yaml").write_text(build_yaml(), encoding="utf-8")
