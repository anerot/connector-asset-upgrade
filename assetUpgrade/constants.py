"""
Constants and endpoint definitions for Moxa FortiSOAR Connector.
"""

# Connector Metadata
LOGGER_NAME = 'assetUpgrade-connector'

## Network Defaults
DEFAULT_PORT = "443"
DEFAULT_PROTOCOL = "HTTPS"
DEFAULT_HTTP_TIMEOUT = 30

# MOXA
## API Endpoints
MOXA_LOGIN_ENDPOINT = "/api/v1/auth/login"
MOXA_HEARTBEAT_ENDPOINT = "/api/v1/auth/heartbeat"
MOXA_PRESTART_ENDPOINT = "/api/v1/file/import/prestart"
MOXA_FIRMWARE_ENDPOINT = "/api/v1/file/import/http/system/firmware.rom"
MOXA_CONFIG_ENDPOINT = "/api/v1/file/export/http/cli/cli.conf"

MOXA_FIRMWARE_UPLOAD_TIMEOUT = 600
MOXA_HEARTBEAT_INTERVAL = 15

## SSH
BUFFER_SIZE = 4096

## OTHER