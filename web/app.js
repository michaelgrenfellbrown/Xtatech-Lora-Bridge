/* Xtatech LoRa Bridge — shared page chrome (header health pills, version). */
(function () {
  function setPill(id, state, text) {
    var el = document.getElementById(id);
    if (!el) return;
    el.className = 'pill ' + (state || '');
    var label = el.querySelector('[data-pill-text]');
    if (label && text) label.textContent = text;
  }

  async function refreshHealth() {
    try {
      var r = await fetch('/api/status', { cache: 'no-store' });
      if (!r.ok) throw new Error(String(r.status));
      var j = await r.json();
      var mqtt = j.mqtt || {};
      var rt = j.runtime || {};
      setPill('xt_health_mqtt', mqtt.connected ? 'ok' : 'bad', mqtt.connected ? 'MQTT' : 'MQTT offline');
      var age = rt.last_serial_rx_ts ? (Date.now() / 1000 - Number(rt.last_serial_rx_ts)) : null;
      var serialState = !rt.serial_ok ? 'bad' : (age !== null && age > 900 ? 'warn' : 'ok');
      setPill('xt_health_serial', serialState, rt.serial_ok ? 'Serial' : 'Serial down');
      var mqttEl = document.getElementById('xt_health_mqtt');
      if (mqttEl) mqttEl.title = mqtt.connected ? 'Connected to ' + mqtt.host + ':' + mqtt.port : 'Disconnected';
      var serEl = document.getElementById('xt_health_serial');
      if (serEl) serEl.title = rt.active_serial_port ? 'Active port ' + rt.active_serial_port : (rt.last_serial_error || 'No serial port');
    } catch (e) {
      setPill('xt_health_mqtt', '', 'MQTT');
      setPill('xt_health_serial', '', 'Serial');
    }
  }

  async function loadVersion() {
    try {
      var r = await fetch('/api/version', { cache: 'no-store' });
      var j = await r.json();
      document.querySelectorAll('[data-app-version]').forEach(function (el) {
        el.textContent = j.display || ('v' + j.version);
      });
    } catch (e) {}
  }

  document.addEventListener('DOMContentLoaded', function () {
    refreshHealth();
    loadVersion();
    setInterval(refreshHealth, 10000);
  });
})();
