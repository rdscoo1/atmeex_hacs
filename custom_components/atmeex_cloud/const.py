DOMAIN = "atmeex_cloud"
PLATFORMS = ["binary_sensor", "climate", "fan", "select", "sensor", "switch"]
BREEZER_MODES = [
    "supply_ventilation",  # 0
    "recirculation",       # 1
    "mixed_mode",          # 2
    "supply_valve",        # 3
]

HUMIDIFICATION_OPTIONS = ["off", "1", "2", "3"]

API_BASE_URL = "https://api.iot.atmeex.com"
INTEGRATION_VERSION = "0.9.5"
USER_AGENT = f"AtmeexCloudHomeAssistant/{INTEGRATION_VERSION}"
API_REQUEST_TIMEOUT_SEC = 20
API_AUTH_TIMEOUT_SEC = 20
RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SEC = 1.0
RETRY_MAX_DELAY_SEC = 8.0
TOKEN_REFRESH_BUFFER_SEC = 60

# Logbook event types (shared between __init__ and logbook modules)
EVENT_API_ERROR = "atmeex_cloud_api_error"
EVENT_DEVICE_UPDATED = "atmeex_cloud_device_updated"
WS_LOGBOOK_MIN_INTERVAL_SEC = 5.0

CONF_UPDATE_INTERVAL = "update_interval"
CONF_ENABLE_WEBSOCKET = "enable_websocket"
CONF_ENABLE_CO2 = "enable_co2"

# Auth method discriminator stored on the config entry.
# Existing entries without this key are email accounts (only path before phone login landed).
CONF_AUTH_METHOD = "auth_method"
CONF_PHONE = "phone"
CONF_PHONE_CODE = "phone_code"
AUTH_METHOD_EMAIL = "email"
AUTH_METHOD_PHONE = "phone"
DEFAULT_UPDATE_INTERVAL = 30
DEFAULT_ENABLE_WEBSOCKET = True
DEFAULT_ENABLE_CO2 = True
MIN_UPDATE_INTERVAL = 10
MAX_UPDATE_INTERVAL = 300
