import os
import time
import json
import threading
import urllib3
import requests
from connectors.core.connector import Connector, get_logger, ConnectorError
from .constants import *
from .operations import operations, check_health, ssh_execute_command, _prepare_ssh_client, delete_local_file

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = get_logger(LOGGER_NAME)


class MoxaClient:
    """HTTP Client for interacting with Moxa device REST API."""

    def __init__(self, config, params):
        server_address = params.get('server_address', '').strip('/')
        port = params.get('port', DEFAULT_PORT)
        protocol = params.get('protocol', DEFAULT_PROTOCOL).lower()

        self.server_url = f"{protocol}://{server_address}:{port}"
        self.username = config.get('username')
        self.password = config.get('password')

        self.session = requests.Session()
        self.session.verify = False

    def login(self):
        """Authenticate, retrieve access token, set headers and return full HTTP response body."""
        url = f"{self.server_url}{MOXA_LOGIN_ENDPOINT}"
        payload = {
            "username": self.username,
            "password": self.password
        }

        try:
            response = self.session.post(url, json=payload, timeout=DEFAULT_HTTP_TIMEOUT)
            response.raise_for_status()
            data = response.json()

            token = data.get("access_token")
            if not token:
                raise Exception("Token 'access_token' missing from login response.")

            # Set raw token in Authorization header as expected by Moxa REST API
            self.session.headers.update({
                "Authorization": token
            })
            logger.info("Successfully connected to Moxa device.")
            return data

        except Exception as Err:
            raise ConnectorError(str(Err))

    def backup_config(self, params):
        """Export/backup device CLI configuration file."""
        self.login()
        url = f"{self.server_url}{MOXA_CONFIG_ENDPOINT}"

        is_running_config = False
        if params.get("is_running_config") == "Running":
            is_running_config = True
        
        password= ""
        if params.get("password"):
            password=params.get("password")

        payload = {
            "file_parameter": {
                "is_running_config": is_running_config,
                "include_default_config": params.get("include_default_config", True),
                "sign_config": params.get("sign_config", False),
                "password": password
            }
        }

        headers = {
            "Accept": "application/octet-stream, application/json, */*",
            "Referer": f"{self.server_url}/"
        }

        try:
            logger.info("Initiating configuration backup export...")
            response = self.session.post(
                url,
                json=payload,
                headers=headers,
                verify=False,
                timeout=DEFAULT_HTTP_TIMEOUT
            )
            response.raise_for_status()

            # Dynamic filename generation (Format: IP_moxa_config_YYYYMMDDHHMM.ini)
            timestamp = time.strftime("%Y%m%d%H%M")
            clean_host = self.server_url.split("//")[-1].split(":")[0]
            filename = f"{clean_host}_moxa_config_{timestamp}.ini"

            # Retrieve SHA-256 checksum if provided in response headers
            sha256_checksum = response.headers.get("sha256sum", "")

            # Save file to /tmp
            file_path = os.path.join("/tmp", filename)
            with open(file_path, "wb") as f:
                f.write(response.content)

            logger.info(f"Configuration exported successfully to {file_path}")

            return {
                "file_path": file_path,
                "file_name": filename,
                "file_size": len(response.content),
                "sha256": sha256_checksum,
                "raw_config": response.text
            }

        except Exception as Err:
            logger.error(f"Failed to export configuration: {str(Err)}")
            raise ConnectorError(str(Err))

    def heartbeat(self):
        """Send a single heartbeat ping using an isolated connection to prevent socket collisions."""
        url = f"{self.server_url}{MOXA_HEARTBEAT_ENDPOINT}"
        headers = {
            "Authorization": self.session.headers.get("Authorization", ""),
            "Referer": f"{self.server_url}/"
        }
        try:
            res = requests.post(
                url,
                headers=headers,
                verify=False,
                timeout=5
            )
            logger.info(f"Heartbeat status: {res.status_code}")
            return res.status_code
        except requests.exceptions.Timeout:
            logger.warning("Heartbeat ping timed out (device likely rebooting).")
            return "TIMEOUT"
        except requests.exceptions.ConnectionError:
            logger.warning("Heartbeat connection error (device offline/rebooting).")
            return "CONNECTION_ERROR"
        except Exception as Err:
            logger.warning(f"Heartbeat ping failed: {str(Err)}")
            return "ERROR"

    def upload_firmware(self, file_path, timeout=MOXA_FIRMWARE_UPLOAD_TIMEOUT):
        """Upload firmware asynchronously and detect success through reboot heartbeat signatures."""
        self.login()

        # Initialize heartbeat tracking state
        stop_heartbeat = threading.Event()

        # Shared state to track observed lifecycle via post-upload heartbeats
        upload_state = {
            "timeout_seen": False,
            "unauthorized_seen": False,
            "upload_error": None
        }

        def _monitored_heartbeat_loop(stop_event, interval=MOXA_HEARTBEAT_INTERVAL):
            """Background worker thread to pulse heartbeat and track reboot sequence."""
            logger.info("Starting background heartbeat loop...")
            while not stop_event.is_set():
                status = self.heartbeat()

                # Track switch state transition (Loss of connectivity -> Device reboot)
                if status in ("TIMEOUT", "CONNECTION_ERROR"):
                    upload_state["timeout_seen"] = True
                elif status == 401:
                    # Switch is back online; session was cleared by reboot
                    if upload_state["timeout_seen"]:
                        upload_state["unauthorized_seen"] = True

                if stop_event.wait(interval):
                    break

        heartbeat_thread = threading.Thread(
            target=_monitored_heartbeat_loop,
            args=(stop_heartbeat, MOXA_HEARTBEAT_INTERVAL),
            daemon=True
        )

        try:
            # -------------------------------------------------------------
            # Step 1: Prestart initialization
            # -------------------------------------------------------------
            prestart_url = f"{self.server_url}{MOXA_PRESTART_ENDPOINT}"
            logger.info("Initiating firmware import prestart...")

            prestart_res = self.session.post(
                prestart_url,
                verify=False,
                timeout=DEFAULT_HTTP_TIMEOUT
            )
            prestart_res.raise_for_status()

            if prestart_res.json().get("import") != "success":
                raise ConnectorError(f"Prestart failed: {prestart_res.text}")

            logger.info("Prestart successful. Starting background heartbeat & file transfer...")

            # Démarrage du heartbeat avant le transfert
            heartbeat_thread.start()

            # -------------------------------------------------------------
            # Step 2: Multipart file upload (.rom) dans un thread séparé
            # -------------------------------------------------------------
            upload_url = f"{self.server_url}{MOXA_FIRMWARE_ENDPOINT}"
            filename = os.path.basename(file_path)

            headers = {
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{self.server_url}/"
            }

            data_payload = {
                'request': json.dumps({"file_parameter": {}})
            }

            def _upload_worker():
                try:
                    with open(file_path, 'rb') as f:
                        files = {'file': (filename, f, 'application/octet-stream')}
                        
                        session_headers = dict(self.session.headers)
                        session_headers.pop("Content-Type", None)
                        session_headers.pop("content-type", None)

                        response = requests.post(
                            upload_url,
                            headers={**session_headers, **headers},
                            files=files,
                            data=data_payload,
                            verify=False,
                            timeout=MOXA_FIRMWARE_UPLOAD_TIMEOUT,
                            allow_redirects=False
                        )

                        if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
                            redirect_target = response.headers.get("Location")
                            logger.error(f"Moxa redirection detected ({response.status_code}) to: {redirect_target}")
                            upload_state["upload_error"] = f"Transfer interrupted by switch (HTTP {response.status_code})."
                            return

                        response.raise_for_status()

                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, urllib3.exceptions.ProtocolError):
                    # Comportement attendu : le Moxa ferme brutalement la connexion pour redémarrer
                    logger.info("Connection closed/timed out by Moxa during POST upload (expected reboot behavior).")
                except Exception as err:
                    logger.error(f"Firmware upload POST failed unexpectedly: {str(err)}")
                    upload_state["upload_error"] = str(err)

            upload_thread = threading.Thread(target=_upload_worker, daemon=True)
            upload_thread.start()

            # -------------------------------------------------------------
            # Step 3: Surveillance réactive du cycle de reboot via Heartbeat
            # -------------------------------------------------------------
            logger.info(f"Waiting {timeout}s max for reboot sequence confirmation via heartbeats...")
            max_wait_time = timeout
            start_time = time.time()

            while (time.time() - start_time) < max_wait_time:
                # 1. Interruption immédiate si l'upload a échoué hors-reboot
                if upload_state["upload_error"]:
                    raise ConnectorError(f"Firmware upload failed: {upload_state['upload_error']}")

                # 2. Détection du cycle complet de reboot (Perte de connexion -> HTTP 401)
                if upload_state["timeout_seen"] and upload_state["unauthorized_seen"]:
                    logger.info("Firmware update successfully detected via heartbeat reboot cycle (Timeout -> 401).")
                    
                    # Tentative de ré-authentification pour confirmer le bon fonctionnement
                    try:
                        self.login()
                        logger.info("Re-authentication successful. Device is online and ready.")
                    except Exception as err:
                        logger.warning(f"Device rebooted (401 received), but initial login retry failed: {err}")

                    return {
                        "status": "success",
                        "message": "Firmware uploaded and device rebooted successfully."
                    }

                time.sleep(3)

            # Fallback si le cycle n'est que partiellement détecté dans le délai imparti
            if upload_state["timeout_seen"]:
                logger.warning("Reboot timeout observed, but device did not return with HTTP 401 within time limit.")
                return {
                    "status": "success",
                    "message": "Firmware upload initiated and device rebooted (unconfirmed post-boot state)."
                }

            raise ConnectorError("Firmware upload failed: Device did not exhibit expected reboot behavior.")

        except Exception as Err:
            logger.error(f"Firmware upload failed: {str(Err)}")
            raise ConnectorError(str(Err))

        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=2)


class MoxaConnector(Connector):
    """FortiSOAR Connector wrapper for Moxa API."""

    def execute(self, config, operation, params, **kwargs):
        try:
            action = operations.get(operation)
            if not action:
                raise ConnectorError(f"Operation {operation} not supported.")
            return action(config, params)
        except Exception as Err:
            raise ConnectorError(str(Err))

class SSH(Connector):

    def execute(self, config, operation, operation_params, **kwargs):
        operation = operations.get(operation)
        return operation(config, operation_params)

    def check_health(self, config):
        pass

