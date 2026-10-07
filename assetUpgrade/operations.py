import os
from connectors.core.connector import get_logger, ConnectorError
from connectors.cyops_utilities.builtins import download_file_from_cyops, upload_file_to_cyops
from .constants import LOGGER_NAME

import time
from integrations.crudhub import make_request
from .utils import _get_list_from_str_or_list
from .constants import BUFFER_SIZE

import paramiko

logger = get_logger(LOGGER_NAME)


def check_health(config):
    """Validate connector configuration"""
    try:
        return True
    except Exception as Err:
        logger.error(f"Health check failed: {str(Err)}")
        raise ConnectorError(str(Err))

def moxa_login_check(config, params):
    """Return HTTP authentication response from Moxa device."""
    from .connector import MoxaClient
    try:
        client = MoxaClient(config, params)
        return client.login()
    except Exception as Err:
        logger.error(f"Login failed: {str(Err)}")
        raise ConnectorError(str(Err))

def moxa_upgrade(config, params):
    """Download attachment from CyOps vault and upload firmware to Moxa device."""
    from .connector import MoxaClient

    file_iri = params.get('file_iri')
    file_path = None

    try:
        file_metadata = download_file_from_cyops(file_iri)
        file_path = "/tmp/" + file_metadata.get('cyops_file_path')
        logger.info(f"file_path: {file_path}")

        client = MoxaClient(config, params)
        result = client.upload_firmware(file_path)

        return {
            "status": "success",
            "result": result
        }
    except Exception as Err:
        raise ConnectorError(str(Err))
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

def moxa_backup_config(config, params):
    from .connector import MoxaClient

    try:
        client = MoxaClient(config, params)
        result = client.backup_config(params)
        
        if params.get("is_running_config") == "Running":
            description = "Moxa Running configuration file"
        else:
            description = "Moxa Startup configuration file"
        
        if params.get("include_default_config"):
            description = description + " with Default configuration."
        else:
            description = description + " without Default configuration."
       
        attachment = upload_file_to_cyops(result.get("file_path"), result.get("file_name"), True, result.get("file_name"), description)
        
        if result.get("file_path") and os.path.exists(result.get("file_path")):
            os.remove(result.get("file_path"))

        return {
            "status": "success",
            "result": attachment
        }
    except Exception as Err:
        raise ConnectorError(str(Err))

def delete_local_file(config, params):
    """Safely deletes a file only if it is located inside the allowed directory

    or any of its subdirectories.
    """
    allowed_dir = "/tmp"
    file_path = params.get("filepath")
    try:
        if not file_path:
            raise ValueError("File path is missing.")

        # Resolve absolute paths to eliminate symlinks and '..' traversal tricks
        base_directory = os.path.realpath(allowed_dir)
        target_file = os.path.realpath(file_path)

        # 1. Verify that the target path resides strictly inside the allowed directory
        # os.path.commonpath checks if base_directory is the common prefix among both paths
        common_prefix = os.path.commonpath([base_directory, target_file])
        is_within_bounds = common_prefix == base_directory

        if not is_within_bounds:
            raise ValueError(
                f"Access denied: '{target_file}' is outside the restricted directory '{base_directory}'."
            )

        # 2. Check that the target exists and is a regular file (not a directory)
        if not os.path.isfile(target_file):
            raise FileNotFoundError(
                f"Target '{target_file}' does not exist or is not a regular file."
            )

        # 3. Perform file deletion
        os.remove(target_file)
        return "Success"

    except Exception as Err:
        raise ConnectorError(str(Err))

def _prepare_ssh_client(config, params):
    try:
        # extract host info
        host = config.get('host').strip('/')
        host = host.split('//')
        if len(host) == 2:
            host = host[1]
        else:
            host = host[0]
        port = params.get('port')
        username = config.get('username')
        password = config.get('password')
        rsa_key = None
        client = paramiko.client.SSHClient()
        client.set_missing_host_key_policy(paramiko.client.AutoAddPolicy())
        client.load_system_host_keys()
        client.connect(host, port=port, username=username, password=password, pkey=rsa_key,
                       allow_agent=False, look_for_keys=False)
        return client
    except Exception as Err:
        raise ConnectorError(str(Err))

def ssh_execute_command(config, params):
    config['host']=params.get('host')
    config['port']=params.get('port',22)
    config['timeout']=params.get('timeout',5)    
    def execute_single_command(command):
        """Helper function to execute a single command and handle errors"""
        logger.debug(f"Executing cmd: {command}")
        streams = client.exec_command(cmd, timeout=params.get('timeout'), get_pty=True)
        stdin, stdout, stderr = streams
        error_str = stderr.read().decode('utf-8')
        if error_str:
            logger.error(f"Failed '{command}' command execution with error [{error_str}]. "
                         f"Commands executed successfully [{cmd_output}]")
            raise ConnectorError(
                f"Failed command execution with error [{error_str}]. Refer log file for more details")
        _result = stdout.read().decode('utf-8').strip()
        return _result

    client = _prepare_ssh_client(config, params)
    try:
        
        cmd_list = _get_list_from_str_or_list(params, 'cmd_list')
        if not cmd_list:
            return []
        cmd_output = []
        is_interactive = params.get('interactive', False)

        if is_interactive:
            # Interactive mode using invoke_shell
            channel = client.invoke_shell()
            wait_time = params.get("wait_time",2)
            try:
                for cmd in cmd_list:
                    if "<username>" in cmd:
                        cmd = cmd.replace("<username>",config.get("username"))
                    if "<password>" in cmd:
                        cmd = cmd.replace("<password>",config.get("password"))
                    logger.info(f"cmd: {cmd}")
                    channel.send(cmd + '\n')
                    time.sleep(wait_time)
                    output = ''
                    while channel.recv_ready():
                        output += channel.recv(BUFFER_SIZE).decode('utf-8')
                    output_lines = [line.strip() for line in output.splitlines() if line.strip()]
                    cmd_output.append({"command": cmd, "output": output_lines})
            finally:
                channel.close()
        else:
            # Non-interactive command execution
            for cmd in cmd_list:
                result = execute_single_command(cmd)
                cmd_output.append({"command": cmd, "output": result.split("\r\n")})
                if "Command fail." in result:
                    logger.error(f"Command failed to execute: {cmd}")
                    raise ConnectorError(f"Command failed to execute: {cmd}")
        return cmd_output

    except Exception as Err:
        logger.error(str(Err))
        raise ConnectorError(str(Err))

    finally:
        client.close()

operations = {
    'moxa_login_check': moxa_login_check,
    'moxa_upgrade': moxa_upgrade,
    'moxa_backup_config': moxa_backup_config,
    'ssh_execute_command': ssh_execute_command,
    'delete_local_file': delete_local_file
}
