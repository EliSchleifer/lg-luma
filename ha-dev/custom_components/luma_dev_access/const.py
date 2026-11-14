"""Constants for Luma dev access."""

DOMAIN = "luma_dev_access"

# Entry data (set once by the config flow).
CONF_MANAGEMENT_KEY = "management_key"
CONF_TOKEN_HASH = "token_hash"
# Entry options (editable).
CONF_TV_HOST = "tv_host"
CONF_TV_PORT = "tv_port"
CONF_TUNNEL_HOSTNAME = "tunnel_hostname"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_CF_CLIENT_ID = "cloudflare_client_id"
CONF_CF_CLIENT_SECRET = "cloudflare_client_secret"
CONF_ROTATE_TOKEN = "rotate_token"
CONF_FORGET_HOST_KEY = "forget_host_key"

DEFAULT_TV_PORT = 22
DEFAULT_TUNNEL_HOSTNAME = "tv.elischleifer.com"

TOKEN_HEADER = "X-Luma-Dev-Token"

# Access lasts at most this long; a session may ask for less.
MAX_TTL = 8 * 3600
MIN_TTL = 5 * 60
# An unanswered request expires after this long.
PENDING_TIMEOUT = 10 * 60
# Requests per rolling hour, and how many may wait for an answer at once.
REQUESTS_PER_HOUR = 5
MAX_PENDING = 2
# Wrong tokens per rolling hour before the endpoints lock for an hour.
BAD_TOKENS_PER_HOUR = 10
LOCKOUT = 3600
# How long a result request may wait for the owner's answer.
MAX_WAIT = 30
# How often the TV's authorized_keys is swept for expired keys.
SWEEP_INTERVAL = 15 * 60

# Streaming search keys, read from the Luma integration's options.
LUMA_DOMAIN = "luma"
LUMA_STREAMING_OPTIONS = {
    "motn": "motn_api_key",
    "tmdb": "tmdb_read_access_token",
    "mdblist": "mdblist_api_key",
}

ACTION_APPROVE = "LUMA_DEV_APPROVE_"
ACTION_DENY = "LUMA_DEV_DENY_"
NOTIFICATION_EVENT = "mobile_app_notification_action"
