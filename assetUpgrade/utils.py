""" Copyright start
  Copyright (C) 2008 - 2025 Fortinet Inc.
  All rights reserved.
  FORTINET CONFIDENTIAL & FORTINET PROPRIETARY SOURCE CODE
  Copyright end """

import ipaddress
from connectors.core.connector import get_logger, ConnectorError
from .constants import LOGGER_NAME

logger = get_logger(LOGGER_NAME)

def _get_list_from_str_or_list(params, parameter, is_ip=False):
    try:
        parameter_list = params.get(parameter)
        if parameter_list:
            if isinstance(parameter_list, str):
                parameter_list = list(map(lambda x: x.strip(' '), parameter_list.split(",")))
            elif isinstance(parameter_list, list):
                parameter_list = parameter_list
            elif isinstance(parameter_list, int):
                return [parameter_list]
            if is_ip:
                for ip in parameter_list:
                    if ' ' in ip:
                        tmp_ip = ip.split(' ')
                        if len(tmp_ip) == 2:
                            try:
                                ipaddress.ip_network(tmp_ip[0], False)
                                ipaddress.ip_network(tmp_ip[1], False)
                            except Exception as Err:
                                logger.error(str(Err))
                                raise ConnectorError(str(Err))
                    else:
                        try:
                            ipaddress.ip_network(ip, False)
                        except Exception as Err:
                            logger.error(str(Err))
                            raise ConnectorError(str(Err))
            return parameter_list
        else:
            return []
    except Exception as Err:
        raise ConnectorError(Err)

