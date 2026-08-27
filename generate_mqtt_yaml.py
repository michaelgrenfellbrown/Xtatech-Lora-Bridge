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
    ("aqia01", "aqia01", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua02", "aqua02", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua03", "aqua03", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua04", "aqua04", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua05", "aqua05", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua06", "aqua06", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua07", "aqua07", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua08", "aqua08", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua09", "aqua09", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua010", "aqua010", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua011", "aqua011", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua012", "aqua12", "LoRa Sensor Node", BURIED_FIELDS),
    ("aqua013", "aqua013", "LoRa Sensor Node", BURIED_FIELDS),
    ("Aqua101", "aqua101", "LoRa Above Ground Sensor Node", ABOVE_GROUND_FIELDS),
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
        "sensor:",
    ]

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


if __name__ == "__main__":
    Path("mqtt.yaml").write_text(build_yaml(), encoding="utf-8")
