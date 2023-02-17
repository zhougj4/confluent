# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2015-2019 Lenovo
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


# This plugin provides an ssh implementation comforming to the 'console'
# specification.  consoleserver or shellserver would be equally likely
# to use this.

import confluent.exceptions as cexc
import confluent.interface.console as conapi
import confluent.log as log
import confluent.util as util
import pyghmi.exceptions as pygexc
import pyghmi.redfish.command as rcmd
import eventlet
import eventlet.green.ssl as ssl
try:
    websocket = eventlet.import_patched('websocket')
    wso = websocket.WebSocket
except Exception:
    wso = object

def get_conn_params(node, configdata):
    if 'secret.hardwaremanagementuser' in configdata:
        username = configdata['secret.hardwaremanagementuser']['value']
    else:
        username = 'USERID'
    if 'secret.hardwaremanagementpassword' in configdata:
        passphrase = configdata['secret.hardwaremanagementpassword']['value']
    else:
        passphrase = 'PASSW0RD'  # for lack of a better guess
    if 'hardwaremanagement.manager' in configdata:
        bmc = configdata['hardwaremanagement.manager']['value']
    else:
        bmc = node
    bmc = bmc.split('/', 1)[0]
    return {
        'username': username,
        'passphrase': passphrase,
        'bmc': bmc,
    }
_configattributes = ('secret.hardwaremanagementuser',
                     'secret.hardwaremanagementpassword',
                     'hardwaremanagement.manager')

class WrappedWebSocket(wso):

    def set_verify_callback(self, callback):
        self._certverify = callback

    def connect(self, url, **options):
        add_tls = url.startswith('wss://')
        if add_tls:
            hostname, port, resource, _ = websocket._url.parse_url(url)
            if hostname[0] != '[' and ':' in hostname:
                hostname = '[{0}]'.format(hostname)
            if resource[0] != '/':
                resource = '/{0}'.format(resource)
            url = 'ws://{0}:443{1}'.format(hostname,resource)
        else:
            return super(WrappedWebSocket, self).connect(url, **options)
        self.sock_opt.timeout = options.get('timeout', self.sock_opt.timeout)
        self.sock, addrs = websocket._http.connect(url, self.sock_opt, websocket._http.proxy_info(**options),
                                           options.pop('socket', None))
        self.sock = ssl.wrap_socket(self.sock, cert_reqs=ssl.CERT_NONE)
        # The above is supersedeed by the _certverify, which provides
        # known-hosts style cert validaiton
        bincert = self.sock.getpeercert(binary_form=True)
        if not self._certverify(bincert):
            raise pygexc.UnrecognizedCertificate('Unknown certificate', bincert)
        try:
            self.handshake_response = websocket._handshake.handshake(self.sock, *addrs, **options)
            if self.handshake_response.status in websocket._handshake.SUPPORTED_REDIRECT_STATUSES:
                options['redirect_limit'] = options.pop('redirect_limit', 3) - 1
                if options['redirect_limit'] < 0:
                     raise Exception('Redirect limit hit')
                url = self.handshake_response.headers['location']
                self.sock.close()
                return self.connect(url, **options)
            self.connected = True
        except:
            if self.sock:
                self.sock.close()
                self.sock = None
            raise


         



class TsmConsole(conapi.Console):

    def __init__(self, node, config):
        self.node = node
        self.ws = None
        configdata = config.get_node_attributes([node], _configattributes, decrypt=True)
        connparams = get_conn_params(node, configdata[node])
        self.username = connparams['username']
        self.password = connparams['passphrase']
        self.bmc = connparams['bmc']
        self.origbmc = connparams['bmc']
        if ':' in self.bmc:
            self.bmc = '[{0}]'.format(self.bmc)
        self.datacallback = None
        self.nodeconfig = config
        self.connected = False


    def recvdata(self):
        while self.connected:
            pendingdata = self.ws.recv()
            if pendingdata == '':
                self.datacallback(conapi.ConsoleEvent.Disconnect)
                return
            self.datacallback(pendingdata)

    def connect(self, callback):
        self.datacallback = callback
        rc = rcmd.Command(self.origbmc, self.username,
                          self.password,
                          verifycallback=lambda x: True)
        wc = rc.oem.wc
        bmc = self.bmc
        if '%' in self.bmc:
            prefix = self.bmc.split('%')[0]
            bmc = prefix + ']'
        self.ws = WrappedWebSocket(host=bmc)
        kv = util.TLSCertVerifier(
            self.nodeconfig, self.node, 'pubkeys.tls_hardwaremanager').verify_cert
        self.ws.set_verify_callback(kv)
        self.ws.connect('wss://{0}/sol?CSRFTOKEN={1}'.format(self.bmc, rc.oem.csrftok), host=bmc, cookie='QSESSIONID={0}'.format(wc.cookies['QSESSIONID']))
        self.connected = True
        eventlet.spawn_n(self.recvdata)
        return

    def write(self, data):
        self.ws.send(data)

    def close(self):
        if self.ws:
            self.ws.close()
        self.connected = False
        self.datacallback = None

def create(nodes, element, configmanager, inputdata):
    if len(nodes) == 1:
        return TsmConsole(nodes[0], configmanager)
