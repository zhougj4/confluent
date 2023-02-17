# Copyright 2022 Lenovo
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

import pyghmi.util.webclient as wc
import confluent.util as util
import confluent.messages as msg
import confluent.exceptions as exc


class GeistClient(object):
    def __init__(self, pdu, configmanager):
        self.node = pdu
        self.configmanager = configmanager
        self._token = None
        self._wc = None
        self.username = None

    @property
    def token(self):
        if not self._token:
            self._token = self.login(self.configmanager)
        return self._token

    @property
    def wc(self):
        if self._wc:
            return self._wc
        targcfg = self.configmanager.get_node_attributes(self.node,
                                            ['hardwaremanagement.manager'],
                                            decrypt=True)
        targcfg = targcfg.get(self.node, {})
        target = targcfg.get(
            'hardwaremanagement.manager', {}).get('value', None)
        if not target:
            target = self.node
        target = target.split('/', 1)[0]
        cv = util.TLSCertVerifier(
            self.configmanager, self.node,
            'pubkeys.tls_hardwaremanager').verify_cert
        self._wc = wc.SecureHTTPConnection(target, port=443, verifycallback=cv)
        return self._wc

    def login(self, configmanager):
        credcfg = configmanager.get_node_attributes(self.node,
                                            ['secret.hardwaremanagementuser',
                                             'secret.hardwaremanagementpassword'],
                                            decrypt=True)
        credcfg = credcfg.get(self.node, {})
        username = credcfg.get(
            'secret.hardwaremanagementuser', {}).get('value', None)
        passwd = credcfg.get(
            'secret.hardwaremanagementpassword', {}).get('value', None)
        if not isinstance(username, str):
            username = username.decode('utf8')
        if not isinstance(passwd, str):
            passwd = passwd.decode('utf8')
        if not username or not passwd:
            raise Exception('Missing username or password')
        self.username = username
        rsp = self.wc.grab_json_response(
            '/api/auth/{0}'.format(username),
            {'cmd': 'login', 'data': {'password': passwd}})
        token = rsp['data']['token']
        return token

    def logout(self):
        if self._token:
            self.wc.grab_json_response('/api/auth/{0}'.format(self.username),
                                       {'cmd': 'logout', 'token': self.token})
            self._token = None

    def get_outlet(self, outlet):
        rsp = self.wc.grab_json_response('/api/dev')
        rsp = rsp['data']
        if len(rsp) != 1:
            raise Exception('Multiple PDUs not supported per pdu')
        pduname = list(rsp)[0]
        outlet = rsp[pduname]['outlet'][str(int(outlet) - 1)]
        state = outlet['state'].split('2')[-1]
        return state

    def set_outlet(self, outlet, state):
        rsp = self.wc.grab_json_response('/api/dev')
        if len(rsp['data']) != 1:
            self.logout()
            raise Exception('Multiple PDUs per endpoint not supported')
        pdu = list(rsp['data'])[0]
        outlet = int(outlet) - 1
        rsp = self.wc.grab_json_response(
            '/api/dev/{0}/outlet/{1}'.format(pdu, outlet),
            {'cmd': 'control', 'token': self.token,
            'data': {'action': state, 'delay': False}})


def retrieve(nodes, element, configmanager, inputdata):
    if 'outlets' not in element:
        for node in nodes:
            yield  msg.ConfluentResourceUnavailable(node, 'Not implemented')
        return
    for node in nodes:
        gc = GeistClient(node, configmanager)
        state = gc.get_outlet(element[-1])
        yield msg.PowerState(node=node, state=state)

def update(nodes, element, configmanager, inputdata):
    if 'outlets' not in element:
        yield msg.ConfluentResourceUnavailable(node, 'Not implemented')
        return
    for node in nodes:
        gc = GeistClient(node, configmanager)
        newstate = inputdata.powerstate(node)
        gc.set_outlet(element[-1], newstate)
    for res in retrieve(nodes, element, configmanager, inputdata):
        yield res
