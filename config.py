# ============================================================
#  shopaking - settings. Edit this file, then restart the server.
# ============================================================

SITE_NAME = "shopaking"
TAGLINE = "Fries and shawarma, delivered hot."

# --- Admin (CHANGE THIS) ---------------------------------------
ADMIN_PASSWORD = "changeme123"
SECRET_KEY = ""            # optional; leave empty to auto-generate

# --- Server ----------------------------------------------------
HOST = "0.0.0.0"
PORT = 20726               # the panel's SERVER_PORT overrides this if set

# --- Money & delivery ------------------------------------------
CURRENCY = "Rs"
DELIVERY_BASE_FEE = 50     # flat fee for every delivery
DELIVERY_PER_KM = 50       # added for every kilometre
MAX_DELIVERY_KM = 25       # orders farther than this are refused

# Your shop's coordinates. With these set, customers tap their spot on a free
# OpenStreetMap (no API key needed) and the distance is worked out for them.
# Get them: right-click your shop on any map and copy the two numbers.
# Leave both at 0 to charge only the flat DELIVERY_BASE_FEE (no map shown).
STORE_LAT = 0.0
STORE_LNG = 0.0
ROAD_FACTOR = 1.3          # straight-line km x this ~= road km

# --- Discord webhooks ------------------------------------------
# Server Settings > Integrations > Webhooks > New Webhook > Copy URL
DISCORD_ORDERS_WEBHOOK = ""
DISCORD_SUPPORT_WEBHOOK = ""   # leave empty to use the orders webhook

# --- Customer support (leave any empty to hide it) -------------
SUPPORT_WHATSAPP = ""      # digits only with country code, e.g. 923001234567
SUPPORT_PHONE = ""
SUPPORT_EMAIL = ""
SUPPORT_HOURS = "Every day, 12 pm to 12 am"

# --- Rider / delivery person (chat, voice notes, calls) ----------
RIDER_PASSWORD = "rider123"      # CHANGE THIS. The rider logs in at /rider
RIDER_NAME = "Your rider"        # name customers see in the chat
RIDER_PHONE = ""                 # e.g. +923001234567 - shows a Call button
