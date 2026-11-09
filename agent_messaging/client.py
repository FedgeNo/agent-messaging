import json
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from .config import client_path, read_config


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, config=None):
        self.config = read_config(config or client_path())
        parsed = urlsplit(self.config['url'])
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Invalid service URL')
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', '::1')):
            raise ValueError('Use HTTPS or a loopback SSH tunnel')
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(self, action, **params):
        params.setdefault('project', self.config.get('project', 'global'))
        payload = json.dumps({'action': action, 'params': params}, ensure_ascii=False).encode()
        request = Request(self.config['url'].rstrip('/') + '/v1/rpc', data=payload,
                          headers={'Authorization': 'Bearer ' + self.config['token'], 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=70) as response:
                result = json.load(response)
        except HTTPError as error:
            with error:
                try: message = json.load(error).get('error', 'Request failed')
                except (ValueError, OSError): message = 'Request failed: HTTP ' + str(error.code)
            raise RuntimeError(message) from None
        except URLError:
            raise RuntimeError('Messaging service is unavailable; no local fallback was written') from None
        return result
