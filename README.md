# Atmeex Cloud Integration for Home Assistant

## Overview

Atmeex Cloud is a custom integration for [Home Assistant](https://www.home-assistant.io/) that connects your Atmeex (AirNanny) ventilation devices to the Home Assistant ecosystem.
It uses the official Atmeex Cloud REST API (https://api.iot.atmeex.com) to provide reliable control and monitoring of your brizers directly from Home Assistant dashboards and automations.

🧩 Originally based on the open-source integration developed by [[@anpavlov](https://github.com/anpavlov)], and extensively rewritten and expanded by Sergei Polunovskii to support modern Home Assistant releases and the current Atmeex API. Most recent improvements were contributed by Roman Khodukin, who fully refactored the integration to modern HA standards, added strict typing and unified runtime data architecture, implemented comprehensive diagnostics and fallback logic, created a complete automated test suite with 83% test coverage.
The integration is now significantly more robust, maintainable, and predictable.

## Features
* 	Auto-discovery of all devices linked to your Atmeex Cloud account.
* 	Power on/off control.
* 	Fan speed control (1–7).
* 	Operation modes: ventilation, recirculation, mixed, and fresh-air intake.
* 	Target temperature control (°C).
* 	Optional humidifier control (if supported by the device).
* 	Real-time sensors for temperature and humidity.
* 	Online/offline status displayed directly on the climate card.
* 	Clean asynchronous I/O using Home Assistant’s shared aiohttp client session.

## Installation

Option 1 — via HACS (recommended)
1. Open HACS → Integrations → Custom repositories.
2. Add this repository:

https://github.com/rdscoo1/atmeex_hacs

Choose **Integration** as the repository type.

3. Find Atmeex Cloud in HACS and click Install.
4. Restart Home Assistant.

Option 2 — manual installation
1. Copy the folder:

`custom_components/atmeex_cloud`

into your Home Assistant configuration directory:

`/config/custom_components/`

2. Restart Home Assistant.

## Configuration
1. Go to Settings → Devices & Services → Add Integration.
2. Search for Atmeex Cloud.
3. Enter your Atmeex account credentials (email and password).
4. After successful login, all connected devices will appear automatically.

The integration uses an internal update coordinator with a 30-second polling interval.

## Entities

Entity Type	Example	Description
**climate**	`climate.brizer_bedroom`	Main entity: on/off, fan, temperature, mode, humidifier
**sensor**	`sensor.brizer_bedroom_temperature`	Current room temperature
**sensor**	`sensor.brizer_bedroom_humidity`	Current humidity
**binary_sensor**	`binary_sensor.brizer_bedroom_online`	Online/offline status

## Humidifier Control

If your device supports a humidifier, a humidity slider will appear under the climate card.
It has four fixed stages, automatically snapping to the nearest level:

| Slider position | Mode    |
|-----------------|---------|
| 0%              | Off     |
| 33%             | Stage 1 |
| 66%             | Stage 2 |
| 100%            | Stage 3 |

Intermediate values (e.g. 25%, 80%) are automatically rounded to the nearest valid stage.

## Troubleshooting

Problem	Cause	Fix
1. Integration fails to load	Old or corrupted files →	Reinstall from HACS
2. Auth failed during setup	Wrong credentials →	Verify your Atmeex Cloud email and password
3. Temperature shows -100°C → API didn’t return room temperature	Wait for the next update or restart Home Assistant
4. Second brizer missing → API returned null for device condition	Fixed in recent releases

You can check detailed logs in:
Settings → System → Logs → custom_components.atmeex_cloud

## Development

### Local setup

`git clone https://github.com/rdscoo1/atmeex_hacs.git`
`cd atmeex_hacs`

All requests use Home Assistant’s shared async session (async_get_clientsession(hass)), ensuring clean resource management and no unclosed sessions.

### Test environment (for contributors)

This repository includes a small test suite based on pytest, pytest-asyncio and pytest-homeassistant-custom-component.

1. Create and activate a virtual environment

From the repository root:

macOS / Linux:

`python3 -m venv .venv`
`source .venv/bin/activate`


Windows (PowerShell):

`python -m venv .venv`
`.\.venv\Scripts\Activate.ps1`


After activation you should see (.venv) at the beginning of your shell prompt.

2. Install development dependencies

All test/development dependencies are listed in **requirements-dev.txt**. Install them into the virtual environment:

`pip install -r requirements-dev.txt`

3. Run the test suite

From the project root (with the virtual environment activated):

`pytest`

or with more verbose output:

`pytest -vv`

### 🧪 Tests & Coverage

All tests are located in the tests/ directory and currently cover:
* API client (api.py)
* Integration setup & DataUpdateCoordinator logic (__init__.py)
* Climate entity behaviour (climate.py)
* Select entities (select.py)
* Fan entity (fan.py)
* Config flow (config_flow.py)
* Diagnostics & diagnostic sensor (diagnostics.py, diagnostics_sensor.py)

The test suite is executed using pytest together with
pytest-homeassistant-custom-component, which provides a lightweight Home Assistant test environment.

### Releasing a new version
1. Update the "version" field in manifest.json.
2. Commit and push your changes.
3. Tag the new release:

`git tag -a v0.3.0 -m "Release 0.3.0"`
`git push --tags`

4. Create a GitHub Release (optionally auto-generate release notes).

## Credits
* 🧠 Development: [Roman Khodukin](https://github.com/rdscoo1)
* ⚙️ Original base integration: [(https://github.com/anpavlov)], [Sergei Polunovskii](https://github.com/pols1)
* 🌐 API & platform: [Atmeex / AirNanny Cloud](https://api.iot.atmeex.com/)
* 🧩 Framework: [Home Assistant](https://www.home-assistant.io/)

## License

Distributed under the [MIT License](https://github.com/pols1/atmeex_hacs/releases/edit/LICENSE).
See the LICENSE file for more details.
