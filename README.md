# Cogeco Sagemcom exporter

Prometheus exporter for a Cogeco Sagemcom cable modem at its bridge-mode
management address. It exposes:

- `cogeco_sagemcom_gui_up`: whether the management interface is reachable.
- `cogeco_sagemcom_gui_request_duration_seconds`: management-interface
  responsiveness.
- `cogeco_sagemcom_internet_probe_up`: TCP reachability to public endpoints
  from the exporter host.
- `cogeco_sagemcom_internet_probe_duration_seconds`: TCP connection latency.
- DOCSIS channel metrics (on by default): lock, frequency, power, downstream
  SNR, codeword counts, registration state, and modem uptime.
- Ethernet LAN ports: link up/speed and byte/packet/error counters.
- System sensors: memory, CPU/load, temperature, reboot count, thermal throttle.

The service never calls reboot, reset, configuration, or write methods.

## Run

1. Keep the existing `.env` local. It is ignored by Git. Use `MODEM_USERNAME`
   (not `USERNAME`) so macOS/zsh does not override it with the local account
   name.
2. Start the exporter:

   ```sh
   docker compose up --build -d
   ```

3. Configure Prometheus to scrape `http://<exporter-host>:9488/metrics`.

`/healthz` only verifies that the exporter process is running. Use
`cogeco_sagemcom_docsis_scrape_up` (or `cogeco_sagemcom_api_up` when
`CHECK_API_LOGIN=true`) for modem/API health.

Set `CHECK_API_LOGIN=true` only while validating JSON-RPC authentication. It
creates an extra short-lived GUI session per scrape.

Set `COLLECT_DOCSIS=false` to fall back to the low-impact GUI reachability and
internet-path monitor. When enabled (default), one session per scrape collects
downstream/upstream channel tables. An uptime decrease is a reliable
modem-restart signal.

## Firmware notes

The modem exposes Sagemcom's authenticated JSON-RPC endpoint at
`/cgi/json-req`. Cogeco's firmware redirects the documented F3896 REST URLs
(`/rest/v1/...`) back to the GUI, so the off-the-shelf Ziggo F3896 exporter is
not compatible as-is.

Login `session-options.nss` must be the GUI's namespace object list
(`gtw` + `tr181` URIs). Passing the option string `"gtw:tr181"` still returns a
session, but every `getValue` then fails with `XMO_UNKNOWN_PATH_ERR`. DOCSIS
paths live under the `gtw` namespace on this firmware.

## Grafana

Dashboard and alert rules live in `Grafana/`. Deploy to Grafana Cloud
(Network folder):

```sh
export GRAFANA_ORG_SLUG=<org-slug>
export GRAFANA_API_KEY=...   # or put the key in ~/.tokens/grafana-<org-slug>/grafana-api.key
./Grafana/deploy.sh
```

This posts to `https://<org-slug>.grafana.net`.

## Scrape

Point Prometheus or Grafana Alloy at `http://<exporter-host>:9488/metrics`
(example Alloy job name: `custom/cogeco_sagemcom_exporter`).

Treat a failed public TCP probe as a symptom, not proof of an ISP outage:
individual public services can filter or rate-limit connections.
