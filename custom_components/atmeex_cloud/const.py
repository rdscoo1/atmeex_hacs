DOMAIN = "atmeex_cloud"
PLATFORMS = ["binary_sensor", "climate", "fan", "select", "sensor"]
BRIZER_MODES = [
    "supply_ventilation",  # 0
    "recirculation",       # 1
    "mixed_mode",          # 2
    "supply_valve",        # 3
]

HUMIDIFICATION_OPTIONS = ["off", "1", "2", "3"]

API_BASE_URL = "https://api.iot.atmeex.com"
API_TIMEOUT_DEFAULT = 20
RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SEC = 1.0
RETRY_MAX_DELAY_SEC = 32.0  # CAP exponential backoff!
TOKEN_REFRESH_BUFFER_SEC = 60

CONF_UPDATE_INTERVAL = "update_interval"
DEFAULT_UPDATE_INTERVAL = 5
MIN_UPDATE_INTERVAL = 3
MAX_UPDATE_INTERVAL = 60