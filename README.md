# Atmeex Cloud Integration for Home Assistant

[![HACS Validation](https://github.com/rdscoo1/atmeex_hacs/actions/workflows/validate.yml/badge.svg)](https://github.com/rdscoo1/atmeex_hacs/actions/workflows/validate.yml)
[![GitHub Release](https://img.shields.io/github/v/release/rdscoo1/atmeex_hacs)](https://github.com/rdscoo1/atmeex_hacs/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Overview

Atmeex Cloud is a custom integration for [Home Assistant](https://www.home-assistant.io/) that connects your **Atmeex (AirNanny)** ventilation devices to the Home Assistant ecosystem. It uses the official Atmeex Cloud REST API to provide reliable control and monitoring of your brizers directly from Home Assistant dashboards and automations.

> 🧩 Originally based on the open-source integration by [@anpavlov](https://github.com/anpavlov), extensively rewritten by [Sergei Polunovskii](https://github.com/pols1), and fully refactored to modern HA standards by [Roman Khodukin](https://github.com/rdscoo1) with race-condition protection, comprehensive diagnostics, and 73+ automated tests.

## Features

### Device Control
- **Auto-discovery** of all devices linked to your Atmeex Cloud account
- **Power on/off** control
- **Fan speed** control (7 discrete levels, 1–7)
- **Operation modes**: supply ventilation, recirculation, mixed mode, supply valve
- **Target temperature** control (10–30°C)
- **Humidifier** control (if supported by device) with 4 stages

### Monitoring
- **Room temperature** sensor
- **Room humidity** sensor
- **Online/offline status** as dedicated binary sensor
- **Diagnostics** sensor with API statistics

### Reliability
- **Race condition protection** — rapid fan speed changes won't regress to stale values
- **Re-authentication flow** — automatic prompt when credentials expire
- **Configurable polling interval** (3–60 seconds)
- **Robust error handling** with automatic retries

### Automation Support
- Custom services: `set_breezer_mode`, `set_humidifier_stage`
- Full entity state exposure for triggers and conditions
- Works with scripts, automations, and voice assistants

## Installation

### Option 1 — via HACS (Recommended)

1. Open **HACS** → **Integrations** → **⋮** (menu) → **Custom repositories**
2. Add repository URL: `https://github.com/rdscoo1/atmeex_hacs`
3. Select **Integration** as the category
4. Click **Add**, then find **Atmeex Cloud** and click **Install**
5. Restart Home Assistant

### Option 2 — Manual Installation

1. Download the latest release from [GitHub Releases](https://github.com/rdscoo1/atmeex_hacs/releases)
2. Copy `custom_components/atmeex_cloud` to your `/config/custom_components/` directory
3. Restart Home Assistant

## Configuration

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for **Atmeex Cloud**
3. Enter your Atmeex account credentials (email and password)
4. All connected devices will appear automatically

### Options

After setup, you can configure:
- **Update interval** (3–60 seconds) — how often to poll the Atmeex Cloud API

## Entities

Each device creates the following entities:

| Platform | Entity ID Example | Description |
|----------|-------------------|-------------|
| `climate` | `climate.bedroom_breezer` | Main control: power, temperature, fan speed, modes |
| `fan` | `fan.bedroom_breezer_fan` | Fan speed as percentage (0–100%) |
| `select` | `select.bedroom_breezer_humidification` | Humidifier stage selector |
| `select` | `select.bedroom_breezer_breezer_mode` | Operation mode selector |
| `binary_sensor` | `binary_sensor.bedroom_breezer_online` | Device connectivity status |
| `sensor` | `sensor.atmeex_diagnostics` | API statistics and health |

## Humidifier Control

If your device supports a humidifier, use the humidity slider or select entity:

| Stage | Description |
|-------|-------------|
| Off | Humidifier disabled |
| Stage 1 | Low humidity output |
| Stage 2 | Medium humidity output |
| Stage 3 | Maximum humidity output |

## Automation Examples

### 1. Turn on Breezer When CO2 is High

```yaml
automation:
  - alias: "Ventilation: High CO2"
    description: "Turn on breezer at high speed when CO2 exceeds threshold"
    trigger:
      - platform: numeric_state
        entity_id: sensor.living_room_co2
        above: 1000
    action:
      - service: climate.set_hvac_mode
        target:
          entity_id: climate.living_room_breezer
        data:
          hvac_mode: fan_only
      - service: climate.set_fan_mode
        target:
          entity_id: climate.living_room_breezer
        data:
          fan_mode: "7"
```

### 2. Night Mode Schedule

```yaml
automation:
  - alias: "Ventilation: Night Mode"
    description: "Reduce fan speed and enable recirculation at night"
    trigger:
      - platform: time
        at: "23:00:00"
    action:
      - service: climate.set_fan_mode
        target:
          entity_id: climate.bedroom_breezer
        data:
          fan_mode: "2"
      - service: select.select_option
        target:
          entity_id: select.bedroom_breezer_breezer_mode
        data:
          option: "рециркуляция"

  - alias: "Ventilation: Morning Mode"
    description: "Increase ventilation in the morning"
    trigger:
      - platform: time
        at: "07:00:00"
    action:
      - service: climate.set_fan_mode
        target:
          entity_id: climate.bedroom_breezer
        data:
          fan_mode: "4"
      - service: select.select_option
        target:
          entity_id: select.bedroom_breezer_breezer_mode
        data:
          option: "приточная вентиляция"
```

### 3. Temperature-Based Control

```yaml
automation:
  - alias: "Ventilation: Cool Down Room"
    description: "Increase ventilation when room is too warm"
    trigger:
      - platform: numeric_state
        entity_id: climate.bedroom_breezer
        attribute: current_temperature
        above: 26
    condition:
      - condition: numeric_state
        entity_id: sensor.outdoor_temperature
        below: 24
    action:
      - service: climate.set_fan_mode
        target:
          entity_id: climate.bedroom_breezer
        data:
          fan_mode: "6"
      - service: select.select_option
        target:
          entity_id: select.bedroom_breezer_breezer_mode
        data:
          option: "приточная вентиляция"
```

### 4. Humidity Control

```yaml
automation:
  - alias: "Humidity: Enable Humidifier"
    description: "Turn on humidifier when humidity drops"
    trigger:
      - platform: numeric_state
        entity_id: climate.bedroom_breezer
        attribute: current_humidity
        below: 40
    action:
      - service: select.select_option
        target:
          entity_id: select.bedroom_breezer_humidification
        data:
          option: "2"

  - alias: "Humidity: Disable Humidifier"
    description: "Turn off humidifier when humidity is sufficient"
    trigger:
      - platform: numeric_state
        entity_id: climate.bedroom_breezer
        attribute: current_humidity
        above: 55
    action:
      - service: select.select_option
        target:
          entity_id: select.bedroom_breezer_humidification
        data:
          option: "off"
```

### 5. Turn Off When Away

```yaml
automation:
  - alias: "Ventilation: Away Mode"
    description: "Turn off breezer when nobody is home"
    trigger:
      - platform: state
        entity_id: group.family
        to: "not_home"
        for:
          minutes: 30
    action:
      - service: climate.set_hvac_mode
        target:
          entity_id: 
            - climate.bedroom_breezer
            - climate.living_room_breezer
        data:
          hvac_mode: "off"

  - alias: "Ventilation: Home Mode"
    description: "Turn on breezer when someone arrives"
    trigger:
      - platform: state
        entity_id: group.family
        to: "home"
    action:
      - service: climate.set_hvac_mode
        target:
          entity_id:
            - climate.bedroom_breezer
            - climate.living_room_breezer
        data:
          hvac_mode: fan_only
      - service: climate.set_fan_mode
        target:
          entity_id:
            - climate.bedroom_breezer
            - climate.living_room_breezer
        data:
          fan_mode: "3"
```

### 6. Voice Assistant Script

```yaml
script:
  boost_ventilation:
    alias: "Boost Ventilation"
    description: "Maximize ventilation for 30 minutes"
    sequence:
      - service: climate.set_fan_mode
        target:
          entity_id: climate.living_room_breezer
        data:
          fan_mode: "7"
      - delay:
          minutes: 30
      - service: climate.set_fan_mode
        target:
          entity_id: climate.living_room_breezer
        data:
          fan_mode: "3"
```

## Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| Integration fails to load | Old or corrupted files | Reinstall from HACS |
| Auth failed during setup | Wrong credentials | Verify your Atmeex Cloud email and password |
| Temperature shows -100°C | API didn't return data | Wait for next update or restart HA |
| Device shows unavailable | Device offline or API issue | Check device connectivity |
| Fan speed reverts | Race condition (fixed in v0.5+) | Update to latest version |

### Enable Debug Logging

Add to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.atmeex_cloud: debug
```

View logs at: **Settings** → **System** → **Logs** → filter by `atmeex_cloud`

## Development

### Local Setup

```bash
git clone https://github.com/rdscoo1/atmeex_hacs.git
cd atmeex_hacs
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

### Running Tests

```bash
pytest              # Quick run
pytest -vv          # Verbose output
pytest --cov        # With coverage report
```

### Test Coverage

The test suite includes **73+ tests** covering:

| Module | Coverage |
|--------|----------|
| `api.py` | API client, authentication, error handling |
| `__init__.py` | Setup, coordinator, race protection |
| `climate.py` | Climate entity, all HVAC operations |
| `fan.py` | Fan entity, speed control |
| `select.py` | Select entities for modes |
| `config_flow.py` | Config and options flow |
| `binary_sensor.py` | Online status sensor |

### CI/CD

This repository uses GitHub Actions for:
- **pytest** — automated testing on Python 3.11 and 3.12
- **HACS validation** — ensures HACS compatibility
- **hassfest** — validates Home Assistant manifest

### Releasing

1. Update `version` in `manifest.json`
2. Commit and push changes
3. Create a tag:
   ```bash
   git tag -a v0.6.0 -m "Release 0.6.0"
   git push --tags
   ```
4. Create a GitHub Release

## Credits

| Role | Contributor |
|------|-------------|
| Lead Developer | [Roman Khodukin](https://github.com/rdscoo1) |
| Original Integration | [@anpavlov](https://github.com/anpavlov) |
| Major Rewrite | [Sergei Polunovskii](https://github.com/pols1) |
| API Platform | [Atmeex / AirNanny](https://atmeex.com/) |

## License

This project is licensed under the [MIT License](LICENSE).
