# -*- coding: utf-8 -*-

from __future__ import absolute_import
import base64
import logging
import threading
import ssl
import websocket
import requests
import time
import json
from . import exceptions
from . import application
from . import wake_on_lan
from .utils import LogIt, LogItWithReturn

logger = logging.getLogger('samsungctl')


URL_FORMAT = "ws://{}:{}/api/v2/channels/samsung.remote.control?name={}"
SSL_URL_FORMAT = "wss://{}:{}/api/v2/channels/samsung.remote.control?name={}"


class RemoteWebsocket(object):
    """Object for remote control connection."""

    @LogIt
    def __init__(self, config):
        self.config = config
        self._loop_event = threading.Event()
        self.receive_lock = threading.Lock()
        self._power_event = threading.Event()
        self.send_event = threading.Event()
        self._registered_callbacks = []
        self._thread = None
        self._mac_address = None
        self.sock = None
        self._running = False
        self.send_event = threading.Event()

    @property
    @LogItWithReturn
    def mac_address(self):
        if self._mac_address is None:
            _mac_address = wake_on_lan.get_mac_address(self.config.host)
            if _mac_address is None:
                _mac_address = ''

            self._mac_address = _mac_address

        return self._mac_address

    @property
    def has_ssl(self):
        try:
            response = requests.get(
                ' http://{0}:8001/api/v2/'.format(self.config.host),
                timeout=3
            )
            is_support = (
                json.loads(response.content)['device']['isSupport']
            )
            return json.loads(is_support)['TokenAuthSupport']
        except (ValueError, KeyError):
            return False
        except (requests.HTTPError, requests.exceptions.ConnectTimeout):
            return None

    @property
    @LogItWithReturn
    def power(self):
        if not self._running and self.config.paired:
            try:
                self.open()
                return True
            except RuntimeError:
                return False

        try:
            requests.get(
                ' http://{0}:8001/api/v2/'.format(self.config.host),
                timeout=3
            )
            return True
        except (requests.HTTPError, requests.exceptions.ConnectTimeout):
            return False

    @power.setter
    @LogIt
    def power(self, value):
        if not self._running and self.config.paired:
            try:
                self.open()
            except RuntimeError:
                pass

        if value and self.sock is None:
            if self.mac_address:
                count = 0
                wake_on_lan.send_wol(self.mac_address)
                self._power_event.wait(10)

                try:
                    self.open()
                except:
                    while not self._power_event.isSet() and count < 6:
                        wake_on_lan.send_wol(self.mac_address)
                        self._power_event.wait(2)
                        try:
                            self.open()
                            break
                        except:
                            count += 1

                    if count == 6:
                        logger.error(
                            'Unable to power on the TV, '
                            'check network connectivity'
                        )

        elif not value and self.sock is not None:
            with self.receive_lock:
                count = 0
                while (
                    not self._power_event.isSet() and
                    self.sock is not None and
                    count < 6
                ):
                    params = dict(
                        Cmd='Click',
                        DataOfCmd='KEY_POWER',
                        Option="false",
                        TypeOfRemote="SendRemoteKey"
                    )

                    logger.info("Sending control command: " + str(params))
                    self.send("ms.remote.control", **params)

                    params = dict(
                        Cmd='Click',
                        DataOfCmd='KEY_POWEROFF',
                        Option="false",
                        TypeOfRemote="SendRemoteKey"
                    )

                    logger.info("Sending control command: " + str(params))
                    self.send("ms.remote.control", **params)
                    self._power_event.wait(2.0)

                if count == 6:
                    logger.info('Unable to power off the TV')

    def loop(self):

        while not self._loop_event.isSet():
            try:
                data = self.sock.recv()
                if data:
                    self.on_message(data)
            except:
                self._loop_event.set()

        self._power_event.set()
        self.sock = None
        del self._registered_callbacks[:]
        logger.info('Websocket closed')
        self._loop_event.clear()
        self._thread = None

    @LogIt
    def open(self):
        with self.receive_lock:
            if not self.config.paired and not self.power:
                self.power = True
                if not self.power:
                    raise RuntimeError(
                        'Unable to pair with TV.. Is the TV off?!?'
                    )

            if self.sock is not None:
                self.close()

            if self.config.port == 8002 or self.has_ssl:
                self.config.port = 8002

                if self.config.token:
                    logger.debug('using saved token: ' + self.config.token)
                    token = "&token=" + self.config.token
                else:
                    token = ''

                sslopt = {"cert_reqs": ssl.CERT_NONE}
                url = SSL_URL_FORMAT.format(
                    self.config.host,
                    self.config.port,
                    self._serialize_string(self.config.name)
                ) + token
            else:
                self.config.port = 8001

                sslopt = {}
                url = URL_FORMAT.format(
                    self.config.host,
                    self.config.port,
                    self._serialize_string(self.config.name)
                )

            try:
                self.sock = websocket.create_connection(url, sslopt=sslopt)
            except:
                raise RuntimeError('Unable to connect to the TV')

            auth_event = threading.Event()

            def unauthorized_callback(_):
                auth_event.set()

                self.unregister_receive_callback(
                    auth_callback,
                    'event',
                    'ms.channel.connect'
                )

                if self.config.port == 8001:
                    logger.debug(
                        "Websocket connection failed. Trying ssl connection"
                    )
                    self.config.port = 8002
                    self.open()
                else:
                    self.close()
                    raise RuntimeError('Authentication denied')

            def auth_callback(data):
                if 'data' in data and 'token' in data["data"]:
                    self.config.token = data['data']["token"]

                    logger.debug('new token: ' + self.config.token)
                    if self.config.path:
                        self.config.save()

                logger.debug("Access granted.")
                auth_event.set()

                self.unregister_receive_callback(
                    unauthorized_callback,
                    'event',
                    'ms.channel.unauthorized'
                )

                if 'data' in data and 'token' in data["data"]:
                    self.config.token = data['data']["token"]
                    logger.debug('new token: ' + self.config.token)

                logger.debug("Access granted.")
                self.config.paired = True
                auth_event.set()

                self._power_event.set()

            self.register_receive_callback(
                auth_callback,
                'event',
                'ms.channel.connect'
            )

            self.register_receive_callback(
                unauthorized_callback,
                'event',
                'ms.channel.unauthorized'
            )

            self._thread = threading.Thread(target=self.loop)
            self._thread.start()

            if self.config.paired:
                auth_event.wait(5.0)
            else:
                auth_event.wait(30.0)

            if not auth_event.isSet():
                self.close()
                raise RuntimeError('Auth Failure')

            self._running = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @LogIt
    def close(self):
        """Close the connection."""
        if self.sock is not None:
            self._loop_event.set()
            self.sock.close()
            if self._thread is not None:
                self._thread.join(3.0)
            if self._thread is not None:
                raise RuntimeError('Loop thread did not properly terminate')

    @LogIt
    def send(self, method, **params):
        if self.sock is None:
            if method != 'ms.remote.control':
                if not self._running:
                    try:
                        self.open()

                        return self.send(method, **params)
                    except RuntimeError:
                        pass

            logger.info('Is the TV on???')
            return

        payload = dict(
            method=method,
            params=params
        )
        self.sock.send(json.dumps(payload))
        self.send_event.wait(0.2)

    @LogIt
    def control(self, key, cmd='Click'):
        """
        Send a control command.
        cmd can be one of the following
        'Click'
        'Press'
        'Release'
        """

        if not self._running:
            try:
                self.open()
            except RuntimeError:
                if key in ('KEY_POWERON', 'KEY_POWER'):
                    self.power = True
                    return
                else:
                    raise

        if key == 'KEY_POWER':
            if self.power:
                self.power = False
            else:
                self.power = True
            return

        if key == 'KEY_POWERON':
            self.power = True
            return

        with self.receive_lock:
            params = dict(
                Cmd=cmd,
                DataOfCmd=key,
                Option="false",
                TypeOfRemote="SendRemoteKey"
            )

            logger.info("Sending control command: " + str(params))
            self.send("ms.remote.control", **params)

    _key_interval = 0.5

    @LogItWithReturn
    def get_application(self, pattern):
        for app in self.applications:
            if pattern in (app.app_id, app.name):
                return app

    @property
    @LogItWithReturn
    def applications(self):
        eden_event = threading.Event()
        installed_event = threading.Event()

        eden_data = []
        installed_data = []

        @LogIt
        def eden_app_get(data):
            logger.debug('eden apps: ' + str(data))
            if 'data' in data:
                eden_data.extend(data['data']['data'])
            eden_event.set()

        @LogIt
        def installed_app_get(data):
            logger.debug('installed apps: ' + str(data))
            if 'data' in data:
                installed_data.extend(data['data']['data'])
            installed_event.set()

        self.register_receive_callback(
            eden_app_get,
            'event',
            'ed.edenApp.get'
        )
        self.register_receive_callback(
            installed_app_get,
            'event',
            'ed.installedApp.get'
        )

        for event in ['ed.edenApp.get', 'ed.installedApp.get']:
            params = dict(
                data='',
                event=event,
                to='host'
            )

            self.send('ms.channel.emit', **params)

        eden_event.wait(10.0)
        installed_event.wait(10.0)

        self.unregister_receive_callback(
            eden_app_get,
            'event',
            'ed.edenApp.get'
        )

        self.unregister_receive_callback(
            installed_app_get,
            'data',
            None
        )

        if not eden_event.isSet():
            logger.debug('ed.edenApp.get timed out')

        if not installed_event.isSet():
            logger.debug('ed.installedApp.get timed out')

        if eden_data and installed_data:
            updated_apps = []

            for eden_app in eden_data[:]:
                for installed_app in installed_data[:]:
                    if eden_app['appId'] == installed_app['appId']:
                        installed_data.remove(installed_app)
                        eden_data.remove(eden_app)
                        eden_app.update(installed_app)
                        updated_apps += [eden_app]
                        break
        else:
            updated_apps = []

        updated_apps += eden_data + installed_data

        for app in updated_apps[:]:
            updated_apps.remove(app)
            updated_apps += [application.Application(self, **app)]

        logger.debug('applications returned: ' + str(updated_apps))

        return updated_apps

    @LogIt
    def register_receive_callback(self, callback, key, data):
        self._registered_callbacks += [[callback, key, data]]

    @LogIt
    def unregister_receive_callback(self, callback, key, data):
        if [callback, key, data] in self._registered_callbacks:
            self._registered_callbacks.remove([callback, key, data])

    @LogIt
    def on_message(self, message):
        response = json.loads(message)
        logger.debug('incoming message: ' + message)

        for callback, key, data in self._registered_callbacks[:]:
            if key in response and (data is None or response[key] == data):
                callback(response)
                self._registered_callbacks.remove([callback, key, data])
                break

    @LogIt
    def start_voice_recognition(self):
        """Activates voice recognition."""
        with self.receive_lock:
            event = threading.Event()

            def voice_callback(_):
                event.set()

            self.register_receive_callback(
                voice_callback,
                'event',
                'ms.voiceApp.standby'
            )

            params = dict(
                Cmd='Press',
                DataOfCmd='KEY_BT_VOICE',
                Option="false",
                TypeOfRemote="SendRemoteKey"
            )

            logger.info("Sending control command: " + str(params))
            self.send("ms.remote.control", **params)

            event.wait(2.0)
            self.unregister_receive_callback(
                voice_callback,
                'event',
                'ms.voiceApp.standby'
            )

            if not event.isSet():
                logger.debug('ms.voiceApp.standby timed out')

    @LogIt
    def stop_voice_recognition(self):
        """Activates voice recognition."""

        with self.receive_lock:
            event = threading.Event()

            def voice_callback(_):
                event.set()

            self.register_receive_callback(
                voice_callback,
                'event',
                'ms.voiceApp.hide'
            )

            params = dict(
                Cmd='Release',
                DataOfCmd='KEY_BT_VOICE',
                Option="false",
                TypeOfRemote="SendRemoteKey"
            )

            logger.info("Sending control command: " + str(params))
            self.send("ms.remote.control", **params)

            event.wait(2.0)
            self.unregister_receive_callback(
                voice_callback,
                'event',
                'ms.voiceApp.hide'
            )
            if not event.isSet():
                logger.debug('ms.voiceApp.hide timed out')

    @staticmethod
    def _serialize_string(string):
        if isinstance(string, str):
            string = str.encode(string)

        return base64.b64encode(string).decode("utf-8")

    @property
    @LogItWithReturn
    def mouse(self):
        return Mouse(self)


class Mouse(object):

    @LogIt
    def __init__(self, remote):
        self._remote = remote
        self._is_running = False
        self._commands = []
        self._ime_start_event = threading.Event()
        self._ime_update_event = threading.Event()
        self._touch_enable_event = threading.Event()
        self._send_event = threading.Event()

    @property
    @LogItWithReturn
    def is_running(self):
        return self._is_running

    @LogIt
    def clear(self):
        if not self.is_running:
            del self._commands[:]

    @LogIt
    def _send(self, cmd, **kwargs):
        """Send a control command."""

        if not self._remote.connection:
            raise exceptions.ConnectionClosed()

        if not self.is_running:
            params = {
                "Cmd": cmd,
                "TypeOfRemote": "ProcessMouseDevice"
            }
            params.update(kwargs)

            payload = json.dumps({
                "method": "ms.remote.control",
                "params": params
            })

            self._commands += [payload]

    @LogIt
    def left_click(self):
        self._send('LeftClick')

    @LogIt
    def right_click(self):
        self._send('RightClick')

    @LogIt
    def move(self, x, y):
        position = dict(
            x=x,
            y=y,
            Time=str(time.time())
        )

        self._send('Move', Position=position)

    @LogIt
    def add_wait(self, wait):
        if self._is_running:
            self._commands += [wait]

    @LogIt
    def stop(self):
        if self.is_running:
            self._send_event.set()
            self._ime_start_event.set()
            self._ime_update_event.set()
            self._touch_enable_event.set()

    @LogIt
    def run(self):
        if self._remote.sock is None:
            logger.error('Is the TV on??')
            return

        if not self.is_running:
            self._send_event.clear()
            self._ime_start_event.clear()
            self._ime_update_event.clear()
            self._touch_enable_event.clear()

            self._is_running = True

            with self._remote.receive_lock:

                @LogIt
                def ime_start(_):
                    self._ime_start_event.set()

                @LogIt
                def ime_update(_):
                    self._ime_update_event.set()

                @LogIt
                def touch_enable(_):
                    self._touch_enable_event.set()

                self._remote.register_receive_callback(
                    ime_start,
                    'event',
                    'ms.remote.imeStart'
                )

                self._remote.register_receive_callback(
                    ime_update,
                    'event',
                    'ms.remote.imeUpdate'
                )

                self._remote.register_receive_callback(
                    touch_enable,
                    'event',
                    'ms.remote.touchEnable'
                )

                for payload in self._commands:
                    if isinstance(payload, (float, int)):
                        self._send_event.wait(payload)
                        if self._send_event.isSet():
                            self._is_running = False
                            return
                    else:
                        logger.info(
                            "Sending mouse control command: " + str(payload)
                        )
                        self._remote.sock.send(payload)

                self._ime_start_event.wait(len(self._commands))
                self._ime_update_event.wait(len(self._commands))
                self._touch_enable_event.wait(len(self._commands))

                self._remote.unregister_receive_callback(
                    ime_start,
                    'event',
                    'ms.remote.imeStart'
                )

                self._remote.unregister_receive_callback(
                    ime_update,
                    'event',
                    'ms.remote.imeUpdate'
                )

                self._remote.unregister_receive_callback(
                    touch_enable,
                    'event',
                    'ms.remote.touchEnable'
                )

                self._is_running = False
