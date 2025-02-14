# coding=utf-8

from __future__ import absolute_import
from __future__ import print_function

import os
import tempfile

import urllib3
import requests
from requests.adapters import HTTPAdapter
from requests_futures.sessions import FuturesSession
from urllib3.util.retry import Retry
import orjson as json
from packaging import version
from urllib.parse import urljoin

from toolz.dicttoolz import assoc

from plaidcloud.rpc.orjson import unsupported_object_json_encoder
from plaidcloud.rpc.remote.rpc_tools import PlainRPCCommon
from plaidcloud.rpc.remote.rpc_common import RPCError, WARNING_CODE

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

__author__ = 'Paul Morel'
__credits__ = ['Paul Morel', 'Adams Tower']
__maintainer__ = 'Adams Tower <adams.tower@tartansolutions.com>'
__copyright__ = '© Copyright 2019, Tartan Solutions, Inc'
__license__ = 'Apache 2.0'

STREAM_ENDPOINTS = {
    'analyze/query/download_csv',
    'analyze/query/download_dataframe',
}

download_folder = os.path.join(tempfile.gettempdir(), "plaid/download")

if not os.path.exists(download_folder):
    os.makedirs(download_folder)


def http_json_rpc(token=None, uri=None, verify_ssl=None, json_data=None, workspace=None, proxies=None,
                  fire_and_forget=False, check_allow_transmit=None):
    """
    Sends a json_rpc request over http.

    Returns:
        dict: The decoded response from the server.
    Args:
        token (str): oauth2 token
        uri (str): the server uri to connect to
        verify_ssl (bool): passed to requests. flag to check the server's certs, or not.
        json_data (json-encodable object): the payload to send
        workspace (int): workspace to connect to. If None, let the server connect to the default workspace for your user or token
        proxies (dict): Dictionary mapping protocol or protocol and hostname to the URL of the proxy.
        fire_and_forget (bool,optional): return from the method after the request is sent (not wait for response)
        check_allow_transmit (callable, optional): For use in retry, callable method to see if retries are still valid to send
    """
    def auth_header():
        if workspace:
            return "Bearer_{}_ws{}".format(token, workspace)
        else:
            return "Bearer_{}".format(token)

    def streamable():
        if json_data and json_data.get('method') in STREAM_ENDPOINTS:
            return True
        return False

    if token:
        headers = {
            "Authorization": auth_header(),
            "Content-Type": "application/json",
        }
    else:
        headers = {
            "Content-Type": "application/json",
        }
    payload = json.dumps(assoc(json_data, 'id', 0), default=unsupported_object_json_encoder, option=json.OPT_NAIVE_UTC | json.OPT_NON_STR_KEYS)

    def get_session():
        if fire_and_forget:
            return FuturesSession()
        return requests.sessions.Session()

    with get_session() as session:
        if streamable():
            retry = 0
        else:
            retry = RPCRetry(check_allow_transmit=check_allow_transmit)
        adapter = HTTPAdapter(max_retries=retry)
        session.mount('http://', adapter)
        session.mount('https://', adapter)

        if streamable():
            handle, file_name = tempfile.mkstemp(dir=download_folder, prefix="download_", suffix=".tmp")
            os.close(handle)  # Can't control the access mode, so close this one and open another.
            with open(file_name, 'wb') as tmp_file:
                with session.post(uri, headers=headers, data=payload, verify=verify_ssl, proxies=proxies,
                                  allow_redirects=False, stream=True) as response:
                    response.raise_for_status()
                    try:
                        result = response.json()
                        return result
                    except Exception:  # JSONDecodeError: Should be this, but which library? json or simplejson - depends what is installed
                        pass
                    for chunk in response.iter_content(chunk_size=None):
                        tmp_file.write(chunk)
            return file_name
        elif fire_and_forget:
            r_future = session.post(uri, headers=headers, data=payload, verify=verify_ssl, proxies=proxies,
                                    allow_redirects=False)

            # Adding a callback that will raise an exception if there was a problem with the request
            def on_request_complete(request_future):
                try:
                    response = request_future.result()
                    response.raise_for_status()
                except:
                    print(f'Exception for method {json_data.get("method")}')
                    raise

            r_future.add_done_callback(on_request_complete)
        else:
            try:
                response = session.post(uri, headers=headers, data=payload, verify=verify_ssl, proxies=proxies,
                                        allow_redirects=False)
                response.raise_for_status()
                result = response.json()
                return result
            except Exception as e:
                print(f'Exception for method {json_data.get("method")}')
                raise


class RPCRetry(Retry):
    def __init__(self, *args, check_allow_transmit=None, **kwargs):
        """

        Args:
            check_allow_transmit (callable, optional): Method to call to check if retries are still to be made
                This can be used to prevent retry of RPC methods once a workflow has been cancelled and the RPC fails
        """
        if version.parse(urllib3.__version__) >= version.parse("1.26.0"):
            kwargs.update(dict(
                allowed_methods=['POST'],
                status_forcelist=[500, 502, 504],
                backoff_factor=0.1,
            ))
        else:
            kwargs.update(dict(
                method_whitelist=['POST'],
                status_forcelist=[500, 502, 504],
                backoff_factor=0.1,
            ))
        if 'connect' not in kwargs:
            kwargs['connect'] = 5
        super(RPCRetry, self).__init__(*args, **kwargs)
        self.__check_allow_transmit = check_allow_transmit

    def new(self, **kw):
        kw.update(dict(check_allow_transmit=self.__check_allow_transmit))
        return super(RPCRetry, self).new(**kw)

    @property
    def allow_transmit(self):
        if self.__check_allow_transmit:
            return self.__check_allow_transmit()
        return True

    def increment(self, *args, **kwargs):
        if not self.allow_transmit:
            raise Exception('No more retries, RPC method has been cancelled')
        return super(RPCRetry, self).increment(*args, **kwargs)


class SimpleRPC(PlainRPCCommon):
    """Call remote rpc methods with a dot based interface, almost as if they
    were simply functions in modules.

    Example:
    rpc = SimpleRPC(token, uri=uri, verify_ssl=verify_ssl, workspace=workspace)
    scopes = rpc.identity.me.scopes()
    """
    def __init__(self, token, uri=None, verify_ssl=None, workspace=None, proxies=None, check_allow_transmit=None):
        verify_ssl = bool(verify_ssl)

        def call_rpc(method_path, params, fire_and_forget=False):
            response = http_json_rpc(
                token, urljoin(uri, method_path), verify_ssl,
                {
                    'jsonrpc': '2.0',
                    'method': method_path,
                    'params': params,
                },
                workspace=workspace,
                proxies=proxies,
                fire_and_forget=fire_and_forget,
                check_allow_transmit=check_allow_transmit
            )
            if response:
                if isinstance(response, str):
                    return response
                if response.get('ok'):
                    return response['result']
                else:
                    error = response['error']
                    if error.get('code') == WARNING_CODE:
                        raise Warning(error.get('message'))
                    else:
                        raise RPCError(
                            error['message'],
                            data=error.get('data'),
                            code=error.get('code'),
                        )

        super(SimpleRPC, self).__init__(call_rpc, check_allow_transmit)
