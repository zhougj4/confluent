# Copyright 2014 IBM Corporation
# Copyright 2015-2018 Lenovo
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

import atexit
import confluent.exceptions as exc
import confluent.firmwaremanager as firmwaremanager
import confluent.interface.console as conapi
import confluent.messages as msg
import confluent.util as util
import copy
import errno
import eventlet
import eventlet.event
import eventlet.green.threading as threading
import eventlet.greenpool as greenpool
import eventlet.queue as queue
import eventlet.support.greendns
from fnmatch import fnmatch
import os
import pwd
import pyghmi.constants as pygconstants
import pyghmi.exceptions as pygexc
import pyghmi.storage as storage
console = eventlet.import_patched('pyghmi.ipmi.console')
ipmicommand = eventlet.import_patched('pyghmi.ipmi.command')
import socket
import ssl
import traceback


if not hasattr(ssl, 'SSLEOFError'):
    ssl.SSLEOFError = None

try:
    range = xrange
except NameError:
    pass

pci_cache = {}

def get_dns_txt(qstring):
    return eventlet.support.greendns.resolver.query(
        qstring, 'TXT')[0].strings[0].replace('i=', '')

def get_pci_text_from_ids(subdevice, subvendor, device, vendor):
    fqpi = '{0}.{1}.{2}.{3}'.format(subdevice, subvendor, device, vendor)
    if fqpi in pci_cache:
        return pci_cache[fqpi]
    vendorstr = None
    try:
        vendorstr = get_dns_txt('{0}.pci.id.ucw.cz'.format(subvendor))
    except Exception:
        try:
            vendorstr = get_dns_txt('{0}.pci.id.ucw.cz'.format(vendor))
        except Exception:
            pass
    devstr = None
    try:
        devstr = get_dns_txt(fqpi + '.pci.id.ucw.cz')
    except Exception:
        try:
            devstr = get_dns_txt('{0}.{1}.pci.id.ucw.cz'.format(
                device, vendor))
        except Exception:
            pass
    if vendorstr and devstr:
        pci_cache[fqpi] = vendorstr, devstr
    return vendorstr, devstr


# There is something not right with the RLocks used in pyghmi when
# eventlet comes into play.  It seems like sometimes on acquire,
# it calls _get_ident and it isn't the id(greenlet) and so
# a thread deadlocks itself due to identity crisis?
# However, since we are not really threaded, the operations being protected
# are not actually dangerously multiplexed...  so we can replace with
# a null context manager for now
class NullLock(object):

    def donothing(self, *args, **kwargs):
        return 1

    __enter__ = donothing
    __exit__ = donothing
    acquire = donothing
    release = donothing

console.session.select = eventlet.green.select
console.session.threading = eventlet.green.threading
console.session.WAITING_SESSIONS = NullLock()
console.session.KEEPALIVE_SESSIONS = NullLock()
console.session.socket.getaddrinfo = eventlet.support.greendns.getaddrinfo


def exithandler():
    if console.session.iothread is not None:
        console.session.iothread.join()

atexit.register(exithandler)

_ipmiworkers = greenpool.GreenPool(512)

_ipmithread = None
_ipmiwaiters = []

sensor_categories = {
    'temperature': frozenset(['Temperature']),
    'energy': frozenset(['Energy']),
    'power': frozenset(['Power', 'Current']),
    'fans': frozenset(['Fan', 'Cooling Device']),
}


class EmptySensor(object):
    def __init__(self, name):
        self.name = name
        self.value = None
        self.states = ['Unavailable']
        self.units = None
        self.health = 'ok'


def hex2bin(hexstring):
    hexvals = hexstring.split(':')
    if len(hexvals) < 2:
        hexvals = hexstring.split(' ')
    if len(hexvals) < 2:
        hexvals = [hexstring[i:i+2] for i in range(0, len(hexstring), 2)]
    bytedata = [int(i, 16) for i in hexvals]
    return bytearray(bytedata)


def simplify_name(name):
    return name.lower().replace(' ', '_').replace('/', '-').replace(
        '_-_', '-')


def sanitize_invdata(indata):
    """Sanitize pyghmi data

    pyghmi will return bytearrays when it has no idea what to do.  In our
    case, we will change those to hex strings.  Additionally, ignore 'extra'
    fields if the oem_parser is set
    """
    if 'oem_parser' in indata and indata['oem_parser'] is not None:
        if 'board_extra' in indata:
            del indata['board_extra']
        if 'chassis_extra' in indata:
            del indata['chassis_extra']
        if 'product_extra' in indata:
            del indata['product_extra']
    for k in indata:
        if isinstance(indata[k], bytearray):
            indata[k] = '0x' + ''.join(format(x, '02x') for x in indata[k])
        elif isinstance(indata[k], dict):
            sanitize_invdata(indata[k])
        elif isinstance(indata[k], list):
            for idx, value in enumerate(indata[k]):
                if isinstance(value, bytearray):
                    indata[k][idx] = '0x' + ''.join(
                        format(x, '02x') for x in indata[k][idx])


class IpmiCommandWrapper(ipmicommand.Command):
    def __init__(self, node, cfm, **kwargs):
        self.cfm = cfm
        self.node = node
        self.sensormap = {}
        self._inhealth = False
        self._lasthealth = None
        kwargs['keepalive'] = False
        self._attribwatcher = cfm.watch_attributes(
            (node,), ('secret.hardwaremanagementuser', 'collective.manager',
                      'secret.hardwaremanagementpassword', 'secret.ipmikg',
                      'hardwaremanagement.manager'), self._attribschanged)
        super(self.__class__, self).__init__(**kwargs)
        self.setup_confluent_keyhandler()
        try:
            os.makedirs('/var/cache/confluent/ipmi/')
        except OSError as e:
            if e.errno != errno.EEXIST or not os.path.isdir(
                    '/var/cache/confluent/ipmi/'):
                raise
        try:
            self.set_sdr_cachedir('/var/cache/confluent/ipmi/')
        except Exception:
            pass


    def setup_confluent_keyhandler(self):
        self.register_key_handler(util.TLSCertVerifier(
            self.cfm, self.node, 'pubkeys.tls_hardwaremanager').verify_cert)

    def close_confluent(self):
        if self._attribwatcher:
            self.cfm.remove_watcher(self._attribwatcher)
            self._attribwatcher = None

    def _attribschanged(self, nodeattribs, configmanager, **kwargs):
        try:
            self.ipmi_session._mark_broken()
        except AttributeError:
            # if ipmi_session doesn't already exist,
            # then do nothing
            pass

    def get_health(self):
        if self._inhealth:
            while self._inhealth:
                eventlet.sleep(0.1)
            return self._lasthealth
        self._inhealth = True
        try:
            self._lasthealth = super(IpmiCommandWrapper, self).get_health()
        except Exception:
            self._inhealth = False
            raise
        self._inhealth = False
        return self._lasthealth


def _ipmi_evtloop():
    while True:
        try:
            console.session.Session.wait_for_rsp(timeout=600)
            while _ipmiwaiters:
                waiter = _ipmiwaiters.pop()
                waiter.send()
        except:  # TODO(jbjohnso): log the trace into the log

            traceback.print_exc()


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
    if 'secret.ipmikg' in configdata:
        kg = configdata['secret.ipmikg']['value']
    else:
        kg = passphrase
    # TODO(jbjohnso): check if the end has some number after a : without []
    # for non default port
    return {
        'username': username,
        'passphrase': passphrase,
        'kg': kg,
        'bmc': bmc,
        'port': 623,
    }


_configattributes = ('secret.hardwaremanagementuser',
                     'secret.hardwaremanagementpassword',
                     'secret.ipmikg', 'hardwaremanagement.manager')


def _donothing(data):
    # a dummy function to avoid some awkward exceptions from
    # zombie pyghmi console objects
    pass


class IpmiConsole(conapi.Console):
    configattributes = frozenset(_configattributes)
    bmctonodemapping = {}

    def __init__(self, node, config):
        self.error = None
        self.datacallback = None
        crypt = config.decrypt
        self.solconnection = None
        config.decrypt = True
        self.broken = False
        configdata = config.get_node_attributes([node], _configattributes)
        connparams = get_conn_params(node, configdata[node])
        config.decrypt = crypt
        self.username = connparams['username']
        self.password = connparams['passphrase']
        self.kg = connparams['kg']
        self.bmc = connparams['bmc']
        self.port = connparams['port']
        self.connected = False
        # ok, is self.bmc unique among nodes already
        # Cannot actually create console until 'connect', when we get callback
        if (self.bmc in self.bmctonodemapping and
                self.bmctonodemapping[self.bmc] != node):
            raise Exception(
                "Duplicate hardwaremanagement.manager attribute for {0} and {1}".format(
                    node, self.bmctonodemapping[self.bmc]))
        self.bmctonodemapping[self.bmc] = node

    def __del__(self):
        self.solconnection = None
        try:
            del self.bmctonodemapping[self.bmc]
        except KeyError:
            pass

    def handle_data(self, data):
        if type(data) == dict:
            if 'error' in data:
                self.solconnection = None
                self.broken = True
                self.error = data['error']
                if self.connected:
                    self.connected = False
                    self.datacallback(conapi.ConsoleEvent.Disconnect)
        else:
            self.datacallback(data)

    def connect(self, callback):
        self.datacallback = callback
        # we provide a weak reference to pyghmi as otherwise we'd
        # have a circular reference and reference counting would never get
        # out...
        try:
            self.solconnection = console.Console(bmc=self.bmc, port=self.port,
                                                 userid=self.username,
                                                 password=self.password,
                                                 kg=self.kg, force=True,
                                                 iohandler=self.handle_data)
            self.solconnection.outputlock = NullLock()
            while (self.solconnection and not self.solconnection.connected and
                   not (self.broken or self.solconnection.broken or
                        self.solconnection.ipmi_session.broken)):
                w = eventlet.event.Event()
                _ipmiwaiters.append(w)
                w.wait(15)
            if (self.broken or not self.solconnection or
                    self.solconnection.broken or
                    self.solconnection.ipmi_session.broken):
                if not self.error:
                    self.error = 'Unknown error'
                if (self.error.startswith('Incorrect password') or
                        self.error.startswith('Unauthorized name')):
                    raise exc.TargetEndpointBadCredentials
                else:
                    raise exc.TargetEndpointUnreachable(self.error)
            self.connected = True
        except socket.gaierror as err:
            raise exc.TargetEndpointUnreachable(str(err))

    def close(self):
        if self.solconnection is not None:
            # break the circular reference here
            self.solconnection.out_handler = _donothing
            self.solconnection.close()
        self.solconnection = None
        self.datacallback = None
        self.broken = True
        self.error = "closed"

    def write(self, data):
        self.solconnection.send_data(data)

    def send_break(self):
        self.solconnection.send_break()


def perform_requests(operator, nodes, element, cfg, inputdata, realop):
    cryptit = cfg.decrypt
    cfg.decrypt = True
    configdata = cfg.get_node_attributes(nodes, _configattributes)
    cfg.decrypt = cryptit
    resultdata = queue.LightQueue()
    livingthreads = set([])
    numnodes = len(nodes)
    for node in nodes:
        livingthreads.add(_ipmiworkers.spawn(
            perform_request, operator, node, element, configdata, inputdata,
            cfg, resultdata, realop))
    while livingthreads:
        try:
            bundle = []
            datum = resultdata.get(timeout=10)
            while datum:
                if datum != 'Done':
                    if isinstance(datum, Exception):
                        raise datum
                    if (hasattr(datum, 'kvpairs') and datum.kvpairs and
                            len(datum.kvpairs) == 1):
                        bundle.append((list(datum.kvpairs)[0], datum))
                        numnodes -= 1
                    else:
                        yield datum
                timeout = 0.1 if numnodes else 0.001
                datum = resultdata.get(timeout=timeout)
        except queue.Empty:
            pass
        finally:
            for datum in sorted(
                    bundle, key=lambda x: util.naturalize_string(x[0])):
                yield datum[1]
        for t in list(livingthreads):
            if t.dead:
                livingthreads.discard(t)
    try:
        # drain queue if a thread put something on the queue and died
        while True:
            datum = resultdata.get_nowait()
            if datum != 'Done':
                yield datum
    except queue.Empty:
        pass


def perform_request(operator, node, element,
                    configdata, inputdata, cfg, results, realop):
        try:
            return IpmiHandler(operator, node, element, configdata, inputdata,
                               cfg, results, realop).handle_request()
        except pygexc.IpmiException as ipmiexc:
            excmsg = str(ipmiexc)
            if excmsg in ('Session no longer connected', 'timeout'):
                results.put(msg.ConfluentTargetTimeout(node))
            else:
                results.put(msg.ConfluentNodeError(node, excmsg))
                raise
        except exc.TargetEndpointUnreachable as tu:
            results.put(msg.ConfluentTargetTimeout(node, str(tu)))
        except ssl.SSLEOFError:
            results.put(msg.ConfluentNodeError(
                node, 'Unable to communicate with the https server on '
                      'the target BMC'))
        except exc.PubkeyInvalid:
            results.put(msg.ConfluentNodeError(
                node,
                'Mismatch detected between target certificate fingerprint '
                'and pubkeys.tls_hardwaremanager attribute'))
        except pygexc.InvalidParameterValue as e:
            results.put(msg.ConfluentNodeError(node, str(e)))
        except Exception as e:
            results.put(msg.ConfluentNodeError(node, 'Unexpected Error: {0}'.format(str(e))))
            traceback.print_exc()
        finally:
            results.put('Done')

persistent_ipmicmds = {}

class IpmiHandler(object):
    def __init__(self, operation, node, element, cfd, inputdata, cfg, output,
                 realop):
        self.cfm = cfg
        self.invmap = {}
        self.output = output
        self.sensorcategory = None
        self.broken = False
        self.error = None
        eventlet.sleep(0)
        self.cfg = cfd[node]
        self.current_user = cfg.current_user
        self.loggedin = False
        self.node = node
        self.element = element
        self.op = operation
        self.realop = realop
        connparams = get_conn_params(node, self.cfg)
        self.ipmicmd = None
        self.inputdata = inputdata
        self.tenant = cfg.tenant
        tenant = cfg.tenant
        while ((node, tenant) not in persistent_ipmicmds or
                not (persistent_ipmicmds[(node, tenant)].ipmi_session.logged or persistent_ipmicmds[(node, tenant)].ipmi_session.logging) or
                persistent_ipmicmds[(node, tenant)].ipmi_session.broken):
            try:
                persistent_ipmicmds[(node, tenant)].close_confluent()
                persistent_ipmicmds[(node, tenant)].ipmi_session._mark_broken()
            except KeyError:  # was no previous session
                pass
            try:
                persistent_ipmicmds[(node, tenant)] = IpmiCommandWrapper(
                    node, cfg, bmc=connparams['bmc'],
                    userid=connparams['username'],
                    password=connparams['passphrase'], kg=connparams['kg'],
                    port=connparams['port'], onlogon=self.logged)

                ipmisess = persistent_ipmicmds[(node, tenant)].ipmi_session
                begin = util.monotonic_time()
                while ((not (ipmisess.broken or self.loggedin)) and
                               (util.monotonic_time() - begin) < 30):
                    ipmisess.wait_for_rsp(31 - (util.monotonic_time() - begin))
                if self.broken or self.loggedin:
                    break
                cfd = cfg.get_node_attributes(node, _configattributes, decrypt=True)
                self.cfg = cfd[node]
                connparams = get_conn_params(node, self.cfg)
                ipmisess._mark_broken()
                #    raise exc.TargetEndpointUnreachable(
                #        "Login process to " + connparams['bmc'] + " died")
            except socket.gaierror as ge:
                if ge.errno == -2:
                    raise exc.TargetEndpointUnreachable(ge.strerror)
                raise
        self.ipmicmd = persistent_ipmicmds[(node, tenant)]
        giveup = util.monotonic_time() + 60
        while not self.ipmicmd.ipmi_session.broken and not self.ipmicmd.ipmi_session.logged and self.ipmicmd.ipmi_session.logging:
            self.ipmicmd.ipmi_session.wait_for_rsp(3)
            if util.monotonic_time() > giveup:
                self.ipmicmd.ipmi_session.broken = True

    bootdevices = {
        'optical': 'cd'
    }

    def logged(self, response, ipmicmd):
        if 'error' in response:
            self.broken = True
            self.error = response['error']
        else:
            self.ipmicmd = ipmicmd
            self.loggedin = True

    def handle_request(self):
        if self.broken:
            if (self.error == 'timeout' or
                    'Insufficient resources' in self.error):
                self.error = self.error.replace(' reported in RAKP4', '')
                self.output.put(msg.ConfluentTargetTimeout(
                    self.node, self.error))
                return
            elif 'Invalid Session ID' in self.error:
                self.output.put(msg.ConfluentTargetTimeout(
                    self.node, 'Temporary Login Error'))
                return
            elif ('Unauthorized' in self.error or
                    'Incorrect password' in self.error):
                self.output.put(
                    msg.ConfluentTargetInvalidCredentials(self.node))
                return
            else:
                raise Exception(self.error)
        if self.element == ['power', 'state']:
            self.power()
        elif self.element == ['_enclosure', 'reseat_bay']:
            self.reseat_bay()
        elif self.element == ['boot', 'nextdevice']:
            self.bootdevice()
        elif self.element == ['health', 'hardware']:
            self.health()
        elif self.element == ['identify']:
            self.identify()
        elif self.element[0] == 'sensors':
            self.handle_sensors()
        elif self.element[:2] == ['configuration', 'storage']:
            self.handle_storage()
        elif self.element[0] == 'configuration':
            self.handle_configuration()
        elif self.element[:3] == ['inventory', 'firmware', 'updates']:
            self.handle_update()
        elif self.element[0] == 'inventory':
            self.handle_inventory()
        elif self.element == ['media', 'attach']:
            self.handle_attach_media()
        elif self.element == ['media', 'detach']:
            self.handle_detach_media()
        elif self.element == ['media', 'uploads']:
            self.handle_media_upload()
        elif self.element == ['media', 'current']:
            self.handle_list_media()
        elif self.element == ['events', 'hardware', 'log']:
            self.do_eventlog()
        elif self.element == ['events', 'hardware', 'decode']:
            self.decode_alert()
        elif self.element == ['console', 'license']:
            self.handle_license()
        elif self.element == ['console', 'graphical']:
            self.handle_graphical_console()
        elif self.element == ['support', 'servicedata']:
            self.handle_servicedata_fetch()
        elif self.element == ['description']:
            self.handle_description()
        else:
            raise Exception('Not Implemented')

    def handle_update(self):
        u = firmwaremanager.Updater(self.node, self.ipmicmd.update_firmware,
                                    self.inputdata.nodefile(self.node), self.tenant,
                                    bank=self.inputdata.bank, configmanager=self.cfm)
        self.output.put(
            msg.CreatedResource(
                'nodes/{0}/inventory/firmware/updates/active/{1}'.format(
                    self.node, u.name)))

    def handle_media_upload(self):
        u = firmwaremanager.Updater(self.node, self.ipmicmd.upload_media,
                                     self.inputdata.nodefile(self.node), self.tenant,
                                     type='mediaupload', configmanager=self.cfm)
        self.output.put(msg.CreatedResource(
            'nodes/{0}/media/uploads/{1}'.format(self.node, u.name)))

    def get_diags(self, savefile, progress, data=None):
        return self.ipmicmd.get_diagnostic_data(
            savefile, progress=progress, autosuffix=True)

    def handle_servicedata_fetch(self):
        u = firmwaremanager.Updater(
            self.node, self.get_diags,
            self.inputdata.nodefile(self.node), self.tenant, type='ffdc',
            owner=self.current_user)
        self.output.put(msg.CreatedResource(
            'nodes/{0}/support/servicedata/{1}'.format(self.node, u.name)))

    def handle_attach_media(self):
        try:
            self.ipmicmd.attach_remote_media(self.inputdata.nodefile(
                self.node))
        except pygexc.UnsupportedFunctionality as uf:
            self.output.put(msg.ConfluentNodeError(self.node, str(uf)))

    def handle_detach_media(self):
        self.ipmicmd.detach_remote_media()

    def handle_list_media(self):
        for media in self.ipmicmd.list_media():
            self.output.put(msg.Media(self.node, media))

    def handle_configuration(self):
        if self.element[1:3] == ['management_controller', 'alerts']:
            return self.handle_alerts()
        elif self.element[1:3] == ['management_controller', 'users']:
            return self.handle_users()
        elif self.element[1:3] == ['management_controller', 'net_interfaces']:
            return self.handle_nets()
        elif self.element[1:3] == ['management_controller', 'reset']:
            return self.handle_reset()
        elif self.element[1:3] == ['management_controller', 'identifier']:
            return self.handle_identifier()
        elif self.element[1:3] == ['management_controller', 'hostname']:
            return self.handle_hostname()
        elif self.element[1:3] == ['management_controller', 'domain_name']:
            return self.handle_domain_name()
        elif self.element[1:3] == ['management_controller', 'ntp']:
            return self.handle_ntp()
        elif self.element[1:4] == ['management_controller', 'extended', 'all']:
            return self.handle_bmcconfig()
        elif self.element[1:4] == ['management_controller', 'extended', 'advanced']:
            return self.handle_bmcconfig(True)
        elif self.element[1:4] == ['management_controller', 'extended', 'extra']:
            return self.handle_bmcconfig(True, extended=True)
        elif self.element[1:3] == ['system', 'all']:
            return self.handle_sysconfig()
        elif self.element[1:3] == ['system', 'advanced']:
            return self.handle_sysconfig(True)
        elif self.element[1:3] == ['system', 'clear']:
            return self.handle_sysconfigclear()
        elif self.element[1:3] == ['management_controller', 'clear']:
            return self.handle_bmcconfigclear()
        elif self.element[1:3] == ['management_controller', 'licenses']:
            return self.handle_licenses()
        elif self.element[1:3] == ['management_controller', 'save_licenses']:
            return self.save_licenses()
        raise Exception('Not implemented')

    def decode_alert(self):
        inputdata = self.inputdata.get_alert(self.node)
        specifictrap = int(inputdata['.1.3.6.1.6.3.1.1.4.1.0'].rpartition(
            '.')[-1])
        for tmpvarbind in inputdata:
            if tmpvarbind.endswith('3183.1.1'):
                varbinddata = inputdata[tmpvarbind]
        varbinddata = hex2bin(varbinddata)
        event = self.ipmicmd.decode_pet(specifictrap, varbinddata)
        self.pyghmi_event_to_confluent(event)
        self.output.put(msg.EventCollection((event,), name=self.node))

    def handle_alerts(self):
        if self.element[3] == 'destinations':
            if len(self.element) == 4:
                # A list of destinations
                maxdest = self.ipmicmd.get_alert_destination_count()
                for alertidx in range(0, maxdest + 1):
                    self.output.put(msg.ChildCollection(alertidx))
                return
            elif len(self.element) == 5:
                alertidx = int(self.element[-1])
                if self.op == 'read':
                    destdata = self.ipmicmd.get_alert_destination(alertidx)
                    self.output.put(msg.AlertDestination(
                        ip=destdata['address'],
                        acknowledge=destdata['acknowledge_required'],
                        acknowledge_timeout=destdata.get('acknowledge_timeout', None),
                        retries=destdata['retries'],
                        name=self.node))
                    return
                elif self.op == 'update':
                    alertparms = self.inputdata.alert_params_by_node(
                        self.node)
                    alertargs = {}
                    if 'acknowledge' in alertparms:
                        alertargs['acknowledge_required'] = alertparms['acknowledge']
                    if 'acknowledge_timeout' in alertparms:
                        alertargs['acknowledge_timeout'] = alertparms['acknowledge_timeout']
                    if 'ip' in alertparms:
                        alertargs['ip'] = alertparms['ip']
                    if 'retries' in alertparms:
                        alertargs['retries'] = alertparms['retries']
                    self.ipmicmd.set_alert_destination(destination=alertidx,
                                                       **alertargs)
                    return
                elif self.op == 'delete':
                    self.ipmicmd.clear_alert_destination(alertidx)
                    return
        raise Exception('Not implemented')

    def handle_nets(self):
        if len(self.element) == 3:
            if self.op != 'read':
                self.output.put(
                    msg.ConfluentNodeError(self.node, 'Unsupported operation'))
                return
            self.output.put(msg.ChildCollection('management'))
        elif len(self.element) == 4 and self.element[-1] == 'management':
            if self.op == 'read':
                lancfg = self.ipmicmd.get_net_configuration()
                v6cfg = self.ipmicmd.get_net6_configuration()
                self.output.put(msg.NetworkConfiguration(
                    self.node, ipv4addr=lancfg['ipv4_address'],
                    ipv4gateway=lancfg['ipv4_gateway'],
                    ipv4cfgmethod=lancfg['ipv4_configuration'],
                    hwaddr=lancfg['mac_address'],
                    staticv6addrs=v6cfg.get('static_addrs', ''),
                    staticv6gateway=v6cfg.get('static_gateway', ''),
                ))
            elif self.op == 'update':
                config = self.inputdata.netconfig(self.node)
                try:
                    self.ipmicmd.set_net_configuration(
                        ipv4_address=config['ipv4_address'],
                        ipv4_configuration=config['ipv4_configuration'],
                        ipv4_gateway=config['ipv4_gateway'])
                    v6addrs = config.get('static_v6_addresses', None)
                    if v6addrs is not None:
                        v6addrs = v6addrs.split(',')
                    v6gw = config.get('static_v6_gateway', None)
                    self.ipmicmd.set_net6_configuration(static_addresses=v6addrs, static_gateway=v6gw)
                except socket.error as se:
                    self.output.put(msg.ConfluentNodeError(self.node,
                                                           se.message))
                except ValueError as e:
                    if e.message == 'negative shift count':
                        self.output.put(msg.ConfluentNodeError(
                            self.node, 'Invalid prefix length given'))
                    else:
                        raise
        elif len(self.element) == 4 and self.element[-1] != 'management':
            self.output.put(
                    msg.ConfluentTargetNotFound(self.node,
                                                'Interface not found'))

    def handle_users(self):
        # Create user
        if len(self.element) == 3:
            if self.op == 'update':
                user = self.inputdata.credentials[self.node]
                self.ipmicmd.create_user(uid=user['uid'], name=user['username'],
                                    password=user['password'],
                                    callback=True,link_auth=True, ipmi_msg=True,
                                    privilege_level=user['privilege_level'])
            # A list of users
            self.output.put(msg.ChildCollection('all'))
            for user in self.ipmicmd.get_users():
                self.output.put(msg.ChildCollection(user, candelete=True))
            return
        # List all users
        elif len(self.element) == 4 and self.element[-1] == 'all':
            users = []
            for user in self.ipmicmd.get_users():
                users.append(self.ipmicmd.get_user(uid=user))
            self.output.put(msg.UserCollection(users=users, name=self.node))
            return
        # Update user
        elif len(self.element) == 4:
            user = int(self.element[-1])
            if self.op == 'read':
                data = self.ipmicmd.get_user(uid=user)
                self.output.put(msg.User(
                    uid=data['uid'],
                    username=data['name'],
                    privilege_level=data['access']['privilege_level'],
                    expiration=data['expiration'],
                    name=self.node))
                return
            elif self.op == 'update':
                user = self.inputdata.credentials[self.node]

                if 'username' in user:
                    self.ipmicmd.set_user_name(uid=user['uid'],
                                               name=user['username'])

                if 'password' in user:
                    self.ipmicmd.set_user_password(uid=user['uid'],
                                                   password=user['password'])
                    self.ipmicmd.set_user_password(uid=user['uid'],
                                    mode='enable', password=user['password'])
                if 'privilege_level' in user:
                    self.ipmicmd.set_user_access(uid=user['uid'],
                                                    privilege_level=user[
                                                        'privilege_level'])
                if 'enabled' in user:
                    if user['enabled'] == 'yes':
                        mode = 'enable'
                    else:
                        mode = 'disable'
                    self.ipmicmd.disable_user(user['uid'], mode)
                return
            elif self.op == 'delete':
                self.ipmicmd.user_delete(uid=user)
                return

    def do_eventlog(self):
        eventout = []
        clear = False
        if self.op == 'delete':
            clear = True
        for event in self.ipmicmd.get_event_log(clear):
            self.pyghmi_event_to_confluent(event)
            eventout.append(event)
        self.output.put(msg.EventCollection(eventout, name=self.node))

    def pyghmi_event_to_confluent(self, event):
        event['severity'] = _str_health(event.get('severity', 'unknown'))
        if 'event_data' in event:
            event['event'] = '{0} - {1}'.format(
                event['event'], event['event_data'])
        if 'event_id' in event:
            event['id'] = '{0}.{1}'.format(event['event_id'],
                                           event['component_type_id'])

    def make_inventory_map(self):
        invnames = self.ipmicmd.get_inventory_descriptions()
        for name in invnames:
            self.invmap[simplify_name(name)] = name

    def make_sensor_map(self, sensors=None):
        if sensors is None:
            sensors = self.ipmicmd.get_sensor_descriptions()
        for sensor in sensors:
            resourcename = sensor['name']
            self.ipmicmd.sensormap[simplify_name(resourcename)] = resourcename

    def read_sensors(self, sensorname):
        if sensorname == 'all':
            sensors = self.ipmicmd.get_sensor_descriptions()
            readings = []
            for sensor in filter(self.match_sensor, sensors):
                try:
                    reading = self.ipmicmd.get_sensor_reading(
                        sensor['name'])
                except pygexc.IpmiException as ie:
                    if ie.ipmicode == 203:
                        self.output.put(msg.SensorReadings([EmptySensor(
                            sensor['name'])], name=self.node))
                        continue
                    raise
                if hasattr(reading, 'health'):
                    reading.health = _str_health(reading.health)
                if hasattr(reading, 'unavailable') and reading.unavailable:
                    self.output.put(msg.SensorReadings([EmptySensor(
                        reading.name)], name=self.node))
                    continue
                readings.append(reading)
            self.output.put(msg.SensorReadings(readings, name=self.node))
        else:
            if sensorname not in self.ipmicmd.sensormap:
                self.make_sensor_map()
            if sensorname not in self.ipmicmd.sensormap:
                self.output.put(
                    msg.ConfluentTargetNotFound(self.node,
                                                'Sensor not found'))
                return
            try:
                reading = self.ipmicmd.get_sensor_reading(
                    self.ipmicmd.sensormap[sensorname])
                if hasattr(reading, 'health'):
                    reading.health = _str_health(reading.health)
                if hasattr(reading, 'unavailable') and reading.unavailable:
                    self.output.put(msg.SensorReadings([EmptySensor(
                        reading.name)], name=self.node))
                else:
                    self.output.put(
                        msg.SensorReadings([reading],
                                           name=self.node))
            except pygexc.IpmiException as ie:
                if ie.ipmicode == 203:
                    self.output.put(msg.ConfluentResourceUnavailable(
                        self.node, 'Unavailable'
                    ))
                else:
                    self.output.put(msg.ConfluentTargetTimeout(self.node))

    def list_inventory(self):
        try:
            components = self.ipmicmd.get_inventory_descriptions()
        except pygexc.IpmiException:
            self.output.put(msg.ConfluentTargetTimeout(self.node))
            return
        self.output.put(msg.ChildCollection('all'))
        for component in components:
            self.output.put(msg.ChildCollection(simplify_name(component)))

    def list_firmware(self):
        self.output.put(msg.ChildCollection('all'))
        for id, data in self.ipmicmd.get_firmware():
            self.output.put(msg.ChildCollection(simplify_name(id)))

    def read_firmware(self, component):
        items = []
        errorneeded = False
        try:
            complist = () if component == 'all' else (component,)
            for id, data in self.ipmicmd.get_firmware(complist):
                if (component in ('core', 'all') or
                        component == simplify_name(id)):
                    items.append({id: data})
        except ssl.SSLEOFError:
            errorneeded = msg.ConfluentNodeError(
                self.node, 'Unable to communicate with the https server on '
                           'the target BMC while trying to read extended '
                           'information')
        except exc.PubkeyInvalid:
            errorneeded = msg.ConfluentNodeError(
                self.node,
                'Extended information unavailable, mismatch detected between '
                'target certificate fingerprint and '
                'pubkeys.tls_hardwaremanager attribute')
        except pygexc.TemporaryError as e:
                errorneeded = msg.ConfluentNodeError(
                self.node, str(e))
        self.output.put(msg.Firmware(items, self.node))
        if errorneeded:
            self.output.put(errorneeded)

    def handle_inventory(self):
        if self.element[1] == 'firmware':
            if len(self.element) == 3:
                return self.list_firmware()
            elif len(self.element) == 4:
                return self.read_firmware(self.element[-1])
        elif self.element[1] == 'hardware':
            if len(self.element) == 3:  # list things in inventory
                return self.list_inventory()
            elif len(self.element) == 4:  # actually read inventory data
                return self.read_inventory(self.element[-1])
        raise Exception('Unsupported scenario...')

    def list_leds(self):
        self.output.put(msg.ChildCollection('all'))
        for category, info in self.ipmicmd.get_leds():
            self.output.put(msg.ChildCollection(simplify_name(category)))

    def read_leds(self, component):
        led_categories = []
        for category, info in self.ipmicmd.get_leds():
            if component == 'all' or component == simplify_name(category):
                led_categories.append({category: info})
        self.output.put(msg.LEDStatus(led_categories, self.node))

    def read_inventory(self, component):
        errorneeded = False
        try:
            invitems = []
            if component == 'all':
                for invdata in self.ipmicmd.get_inventory():
                    if invdata[1] is None:
                        newinf = {'present': False, 'information': None,
                                  'name': invdata[0]}

                    else:
                        sanitize_invdata(invdata[1])
                        newinf = {'present': True, 'information': invdata[1]}
                        newinf['name'] = invdata[1].get('name', invdata[0])
                    self.add_invitem(invitems, newinf)
            else:
                self.make_inventory_map()
                compname = self.invmap.get(component, None)
                if compname is None:
                    self.output.put(msg.ConfluentTargetNotFound())
                    return
                invdata = self.ipmicmd.get_inventory_of_component(compname)
                if invdata is None:
                    newinf = {'present': False, 'information': None,
                              'name': compname}
                else:
                    sanitize_invdata(invdata)
                    newinf = {'present': True, 'information': invdata,
                              'name': invdata.get('name', compname)}
                self.add_invitem(invitems, newinf)
        except ssl.SSLEOFError:
            errorneeded = msg.ConfluentNodeError(
                self.node, 'Unable to communicate with the https server on '
                           'the target BMC while trying to read extended '
                           'information')
        except exc.PubkeyInvalid:
            errorneeded = msg.ConfluentNodeError(
                self.node,
                'Extended information unavailable, mismatch detected between '
                'target certificate fingerprint and '
                'pubkeys.tls_hardwaremanager attribute')
        newinvdata = {'inventory': invitems}
        self.output.put(msg.KeyValueData(newinvdata, self.node))
        if errorneeded:
            self.output.put(errorneeded)

    def add_invitem(self, invitems, newinf):
        if newinf.get('information', None) and 'name' in newinf['information']:
            newinf = copy.deepcopy(newinf)
            del newinf['information']['name']
        if (fnmatch(newinf['name'], 'Adapter ??:??:??') or fnmatch(
                newinf['name'], 'PCIeGen? x*') or not newinf['name']):
            myinf = newinf.get('information', {})
            sdid = myinf.get('PCI Subsystem Device ID', None)
            svid = myinf.get('PCI Subsystem Vendor ID', None)
            did = myinf.get('PCI Device ID', None)
            vid = myinf.get('PCI Vendor ID', None)
            vstr, dstr = get_pci_text_from_ids(sdid, svid, did, vid)
            if vstr:
                newinf['information']['PCI Vendor'] = vstr
            if dstr:
                newinf['name'] = dstr
        invitems.append(newinf)

    def handle_storage(self):
        if self.element[-1] == '':
            self.element = self.element[:-1]
        storelem = self.element[2:]
        if 'read' == self.op:
            return self._show_storage(storelem)
        elif 'update' == self.realop:
            return self._update_storage(storelem)
        elif 'delete' == self.op:
            return self._delete_storage(storelem)
        elif 'create' == self.realop:
            return self._create_storage(storelem)

    def _delete_storage(self, storelem):
        if len(storelem) < 2:
            storelem.append('')
        if len(storelem) < 2 or storelem[0] != 'volumes':
            raise exc.InvalidArgumentException('Must target a specific volume')
        volname = storelem[-1]
        curr = self.ipmicmd.get_storage_configuration()
        volumes = []
        volsfound = False
        toremove = storage.ConfigSpec(arrays=[storage.Array(volumes=volumes)])
        for pool in curr.arrays:
            for vol in pool.volumes:
                if simplify_name(vol.name) == volname:
                    volsfound = True
                    volumes.append(vol)
        if not volsfound:
            self.output.put(msg.ConfluentTargetNotFound(
                self.node, "No volume named '{0}' found".format(volname)))
            return
        self.ipmicmd.remove_storage_configuration(toremove)
        self.output.put(msg.DeletedResource(volname))

    def _create_storage(self, storelem):
        if 'volumes' not in storelem:
            raise exc.InvalidArgumentException('Can only create volumes')
        vols = []
        thedisks = None
        currcfg = self.ipmicmd.get_storage_configuration()
        currnames = []
        for arr in currcfg.arrays:
            arrname = '{0}-{1}'.format(*arr.id)
            for vol in arr.volumes:
                currnames.append(vol.name)
        disks = []
        vols = []
        vol = self.inputdata.inputbynode[self.node][0]
        raidlvl = vol['raidlevel']
        for disk in currcfg.disks:
            if simplify_name(disk.name) in vol['disks']:
                disks.append(disk)
            elif (disk.status == 'Unconfigured Good' and
                  vol['disks'][0] in ('remainder', 'rest')):
                disks.append(disk)
            elif vol['disks'][0] == 'all':
                disks.append(disk)
        for vol in self.inputdata.inputbynode[self.node]:
            if thedisks and thedisks != vol['disks']:
                    raise exc.InvalidArgumentException(
                        'Not currently supported to create multiple arrays '
                        'in a single request')
            if raidlvl and vol['raidlevel'] != raidlvl:
                raise exc.InvalidArgumentException('Cannot mix raid levels in '
                                                   'a single array')
            vols.append(storage.Volume(name=vol['name'], size=vol['size'], stripsize=vol['stripsize']))
        newcfg = storage.ConfigSpec(
            arrays=(storage.Array(raid=raidlvl, disks=disks, volumes=vols),))
        self.ipmicmd.apply_storage_configuration(newcfg)
        for vol in self.inputdata.inputbynode[self.node]:
            if vol['name'] is None:
                newcfg = self.ipmicmd.get_storage_configuration()
                for arr in newcfg.arrays:
                    arrname = '{0}-{1}'.format(*arr.id)
                    for vol in arr.volumes:
                        if vol.name not in currnames:
                            self.output.put(
                                msg.Volume(self.node, vol.name, vol.size,
                                           vol.status, arrname))
                return
            else:
                self._show_storage(storelem[:1] + [vol['name']])

    def _update_storage(self, storelem):
        if storelem[0] == 'disks':
            if len(storelem) == 1:
                raise exc.InvalidArgumentException('Must target a disk')
            self.set_disk(storelem[-1],
                          self.inputdata.inputbynode[self.node])
        self._show_storage(storelem)

    def _show_storage(self, storelem):
        if storelem[0] == 'disks':
            if len(storelem) == 1:
                return self.list_disks()
            return self.show_disk(storelem[1])
        elif storelem[0] == 'arrays':
            if len(storelem) == 1:
                return self.list_arrays()
            return self.show_array(storelem[1])
        elif storelem[0] == 'volumes':
            if len(storelem) == 1:
                return self.list_volumes()
            return self.show_volume(storelem[1])
        elif storelem[0] == 'all':
            return self._show_all_storage()


    def handle_sensors(self):
        if self.element[-1] == '':
            self.element = self.element[:-1]
        if len(self.element) < 3:
            return
        self.sensorcategory = self.element[2]
        # list sensors per category
        if len(self.element) == 3 and self.element[-2] == 'hardware':
            if self.sensorcategory == 'leds':
                return self.list_leds()
            return self.list_sensors()
        elif len(self.element) == 4:  # resource requested
            if self.sensorcategory == 'leds':
                return self.read_leds(self.element[-1])
            return self.read_sensors(self.element[-1])

    def match_sensor(self, sensor):
        if self.sensorcategory == 'all':
            return True
        if sensor['type'] in sensor_categories[self.sensorcategory]:
            return True
        return False

    def set_disk(self, name, state):
        scfg = self.ipmicmd.get_storage_configuration()
        for disk in scfg.disks:
            if (name == 'all' or simplify_name(disk.name) == name or
                    disk == name):
                disk.status = state
        self.ipmicmd.apply_storage_configuration(
            storage.ConfigSpec(disks=scfg.disks))

    def _show_all_storage(self):
        scfg = self.ipmicmd.get_storage_configuration()
        for disk in scfg.disks:
            self.output.put(
                msg.Disk(self.node, disk.name, disk.description,
                         disk.id, disk.status, disk.serial,
                         disk.fru))
        for arr in scfg.arrays:
            for disk in arr.disks:
                self.output.put(
                    msg.Disk(self.node, disk.name, disk.description,
                             disk.id, disk.status, disk.serial,
                             disk.fru, array='{0}-{1}'.format(*arr.id)))
            for disk in arr.hotspares:
                self.output.put(
                    msg.Disk(self.node, disk.name, disk.description,
                             disk.id, disk.status, disk.serial,
                             disk.fru, array='{0}-{1}'.format(*arr.id)))
        for arr in scfg.arrays:
            arrname = '{0}-{1}'.format(*arr.id)
            self._detail_array(arr, arrname, True)

    def show_disk(self, name):
        scfg = self.ipmicmd.get_storage_configuration()
        for disk in scfg.disks:
            if simplify_name(disk.name) == name or disk == name:
                self.output.put(
                    msg.Disk(self.node, disk.name, disk.description,
                                         disk.id, disk.status, disk.serial,
                                         disk.fru))
        for arr in scfg.arrays:
            arrname = '{0}-{1}'.format(*arr.id)
            for disk in arr.disks:
                if (name == 'all' or simplify_name(disk.name) == name or
                        disk == name):
                    self.output.put(
                        msg.Disk(self.node, disk.name, disk.description,
                                 disk.id, disk.status, disk.serial,
                                 disk.fru, arrname))
            for disk in arr.hotspares:
                if (name == 'all' or simplify_name(disk.name) == name or
                        disk == name):
                    self.output.put(
                        msg.Disk(self.node, disk.name, disk.description,
                                 disk.id, disk.status, disk.serial,
                                 disk.fru, arrname))

    def list_disks(self):
        scfg = self.ipmicmd.get_storage_configuration()
        for disk in scfg.disks:
            self.output.put(msg.ChildCollection(simplify_name(disk.name)))
        for arr in scfg.arrays:
            for disk in arr.disks:
                self.output.put(msg.ChildCollection(simplify_name(disk.name)))
            for disk in arr.hotspares:
                self.output.put(msg.ChildCollection(simplify_name(disk.name)))

    def list_arrays(self):
        scfg = self.ipmicmd.get_storage_configuration()
        for arr in scfg.arrays:
            self.output.put(msg.ChildCollection('{0}-{1}'.format(*arr.id)))

    def show_array(self, name):
        scfg = self.ipmicmd.get_storage_configuration()
        for arr in scfg.arrays:
            arrname = '{0}-{1}'.format(*arr.id)
            if arrname == name:
                self._detail_array(arr, arrname)

    def _detail_array(self, arr, arrname, detailvol=False):
        vols = []
        for vol in arr.volumes:
            vols.append(simplify_name(vol.name))
        disks = []
        for disk in arr.disks:
            disks.append(simplify_name(disk.name))
        for disk in arr.hotspares:
            disks.append(simplify_name(disk.name))
        self.output.put(msg.Array(self.node, disks, arr.raid,
                                  vols, arrname, arr.capacity,
                                  arr.available_capacity))
        if detailvol:
            for vol in arr.volumes:
                self.output.put(msg.Volume(self.node, vol.name, vol.size,
                                           vol.status, arrname))

    def show_volume(self, name):
        scfg = self.ipmicmd.get_storage_configuration()
        for arr in scfg.arrays:
            arrname = '{0}-{1}'.format(*arr.id)
            for vol in arr.volumes:
                if name == simplify_name(vol.name):
                    self.output.put(msg.Volume(self.node, vol.name, vol.size,
                                               vol.status, arrname))

    def list_volumes(self):
        scfg = self.ipmicmd.get_storage_configuration()
        for arr in scfg.arrays:
            for vol in arr.volumes:
                self.output.put(msg.ChildCollection(simplify_name(vol.name)))

    def list_sensors(self):
        try:
            sensors = self.ipmicmd.get_sensor_descriptions()
        except pygexc.IpmiException:
            self.output.put(msg.ConfluentTargetTimeout(self.node))
            return
        self.output.put(msg.ChildCollection('all'))
        for sensor in filter(self.match_sensor, sensors):
            self.output.put(msg.ChildCollection(simplify_name(sensor['name'])))

    def health(self):
        if 'read' == self.op:
            try:
                response = self.ipmicmd.get_health()
            except pygexc.IpmiException:
                self.output.put(msg.ConfluentTargetTimeout(self.node))
                return
            health = response['health']
            health = _str_health(health)
            self.output.put(msg.HealthSummary(health, self.node))
            if 'badreadings' in response:
                badsensors = []
                for reading in response['badreadings']:
                    if hasattr(reading, 'health'):
                        reading.health = _str_health(reading.health)
                    badsensors.append(reading)
                self.output.put(msg.SensorReadings(badsensors, name=self.node))
        else:
            raise exc.InvalidArgumentException('health is read-only')

    def reseat_bay(self):
        bay = self.inputdata.inputbynode[self.node]
        try:
            self.ipmicmd.reseat_bay(bay)
            self.output.put(msg.ReseatResult(self.node, 'success'))
        except pygexc.UnsupportedFunctionality as uf:
            self.output.put(uf)

    def bootdevice(self):
        if 'read' == self.op:
            bootdev = self.ipmicmd.get_bootdev()
            if bootdev['bootdev'] in self.bootdevices:
                bootdev['bootdev'] = self.bootdevices[bootdev['bootdev']]
            bootmode = 'unspecified'
            if 'uefimode' in bootdev:
                if bootdev['uefimode']:
                    bootmode = 'uefi'
                else:
                    bootmode = 'bios'
            persistent = False
            if 'persistent' in bootdev:
                persistent = bootdev['persistent']
            self.output.put(msg.BootDevice(node=self.node,
                                           device=bootdev['bootdev'],
                                           bootmode=bootmode,
                                           persistent=persistent))
            return
        elif 'update' == self.op:
            bootdev = self.inputdata.bootdevice(self.node)
            douefi = False
            if self.inputdata.bootmode(self.node) == 'uefi':
                douefi = True
            persistent = self.inputdata.persistent(self.node)
            bootdev = self.ipmicmd.set_bootdev(bootdev, uefiboot=douefi,
                                               persist=persistent)
            if bootdev['bootdev'] in self.bootdevices:
                bootdev['bootdev'] = self.bootdevices[bootdev['bootdev']]
            self.output.put(msg.BootDevice(node=self.node,
                                           device=bootdev['bootdev']))

    def identify(self):
        if 'update' == self.op:
            identifystate = self.inputdata.inputbynode[self.node] == 'on'
            if self.inputdata.inputbynode[self.node] == 'blink':
                raise exc.InvalidArgumentException(
                    '"blink" is not supported with ipmi')
            self.ipmicmd.set_identify(on=identifystate)
            self.output.put(msg.IdentifyState(
                node=self.node, state=self.inputdata.inputbynode[self.node]))
            return
        elif 'read' == self.op:
            # ipmi has identify as read-only for now
            self.output.put(msg.IdentifyState(node=self.node, state=''))
            return

    def power(self):
        if 'read' == self.op:
            power = self.ipmicmd.get_power()
            self.output.put(msg.PowerState(node=self.node,
                                           state=power['powerstate']))
            return
        elif 'update' == self.op:
            powerstate = self.inputdata.powerstate(self.node)
            oldpower = None
            waitamount = 30
            if powerstate == 'boot':
                oldpower = self.ipmicmd.get_power()
                if 'powerstate' in oldpower:
                    oldpower = oldpower['powerstate']
            elif powerstate == 'shutdown':
                waitamount = True
            self.ipmicmd.set_power(powerstate, wait=waitamount)
            if powerstate == 'boot' and oldpower == 'on':
                power = {'powerstate': 'reset'}
            else:
                power = self.ipmicmd.get_power()
                if powerstate == 'reset' and power['powerstate'] == 'on':
                    power['powerstate'] = 'reset'

            self.output.put(msg.PowerState(node=self.node,
                                           state=power['powerstate'],
                                           oldstate=oldpower))
            return

    def handle_reset(self):
        if 'read' == self.op:
            self.output.put(msg.BMCReset(node=self.node,
                                         state='reset'))
            return
        elif 'update' == self.op:
            self.ipmicmd.reset_bmc()
            return

    def handle_identifier(self):
        if 'read' == self.op:
            mci = self.ipmicmd.get_mci()
            self.output.put(msg.MCI(self.node, mci))
            return
        elif 'update' == self.op:
            mci = self.inputdata.mci(self.node)
            self.ipmicmd.set_mci(mci)
            return

    def handle_hostname(self):
        if 'read' == self.op:
            hostname = self.ipmicmd.get_hostname()
            self.output.put(msg.Hostname(self.node, hostname))
            return
        elif 'update' == self.op:
            hostname = self.inputdata.hostname(self.node)
            self.ipmicmd.set_hostname(hostname)
            return

    def handle_domain_name(self):
        if 'read' == self.op:
            dn = self.ipmicmd.get_domain_name()
            self.output.put(msg.DomainName(self.node, dn))
            return
        elif 'update' == self.op:
            dn = self.inputdata.domain_name(self.node)
            self.ipmicmd.set_domain_name(dn)
            return

    def handle_bmcconfigclear(self):
        if 'read' == self.op:
            raise exc.InvalidArgumentException(
                'Cannot read the "clear" resource')
        self.ipmicmd.clear_bmc_configuration()

    def handle_sysconfigclear(self):
        if 'read' == self.op:
            raise exc.InvalidArgumentException(
                'Cannot read the "clear" resource')
        self.ipmicmd.clear_system_configuration()

    def handle_bmcconfig(self, advanced=False, extended=False):
        if 'read' == self.op:
            try:
                if extended:
                    bmccfg = self.ipmicmd.get_extended_bmc_configuration()
                else:
                    bmccfg = self.ipmicmd.get_bmc_configuration()
                self.output.put(msg.ConfigSet(self.node, bmccfg))
            except Exception as e:
                self.output.put(
                    msg.ConfluentNodeError(self.node, str(e)))
        elif 'update' == self.op:
            self.ipmicmd.set_bmc_configuration(
                self.inputdata.get_attributes(self.node))

    def handle_sysconfig(self, advanced=False):
        if 'read' == self.op:
            try:
                self.output.put(msg.ConfigSet(
                    self.node, self.ipmicmd.get_system_configuration(
                        hideadvanced=not advanced)))
            except Exception as e:
                self.output.put(
                    msg.ConfluentNodeError(self.node, str(e)))
        elif 'update' == self.op:
            self.ipmicmd.set_system_configuration(
                self.inputdata.get_attributes(self.node))

    def handle_ntp(self):
        if self.element[3] == 'enabled':
            if 'read' == self.op:
                enabled = self.ipmicmd.get_ntp_enabled()
                self.output.put(msg.NTPEnabled(self.node, enabled))
                return
            elif 'update' == self.op:
                enabled = self.inputdata.ntp_enabled(self.node)
                self.ipmicmd.set_ntp_enabled(enabled == 'True')
                return
        elif self.element[3] == 'servers':
            if len(self.element) == 4:
                self.output.put(msg.ChildCollection('all'))
                size = len(self.ipmicmd.get_ntp_servers())
                for idx in range(1, size + 1):
                    self.output.put(msg.ChildCollection(idx))
            else:
                if 'read' == self.op:
                    if self.element[-1] == 'all':
                        servers = self.ipmicmd.get_ntp_servers()
                        self.output.put(msg.NTPServers(self.node, servers))
                        return
                    else:
                        idx = int(self.element[-1]) - 1
                        servers = self.ipmicmd.get_ntp_servers()
                        if len(servers) > idx:
                            self.output.put(msg.NTPServer(self.node, servers[idx]))
                        else:
                            self.output.put(
                                msg.ConfluentTargetNotFound(
                                    self.node, 'Requested NTP configuration not found'))
                        return
                elif self.op in ('update', 'create'):
                    if self.element[-1] == 'all':
                        servers = self.inputdata.ntp_servers(self.node)
                        for idx in servers:
                            self.ipmicmd.set_ntp_server(servers[idx],
                                                        int(idx[-1])-1)
                        return
                    else:
                        idx = int(self.element[-1]) - 1
                        server = self.inputdata.ntp_server(self.node)
                        self.ipmicmd.set_ntp_server(server, idx)
                        return

    def handle_license(self):
        available = self.ipmicmd.get_remote_kvm_available()
        self.output.put(msg.License(self.node, available))
        return

    def save_licenses(self):
        directory = self.inputdata.nodefile(self.node)
        checkdir = directory
        if not os.access(directory, os.W_OK):
            raise exc.InvalidArgumentException(
                'The confluent system user/group is unable to write to '
                'directory {0}, check ownership and permissions'.format(
                    checkdir))
        for saved in self.ipmicmd.save_licenses(directory):
            if self.current_user:
                try:
                    pwent = pwd.getpwnam(self.current_user)
                    os.chown(saved, pwent.pw_uid, pwent.pw_gid)
                except KeyError:
                    pass
            self.output.put(msg.SavedFile(self.node, saved))

    def handle_licenses(self):
        if self.element[-1] == '':
            self.element = self.element[:-1]
        if self.op in ('create', 'update'):
            filename = self.inputdata.nodefile(self.node)
            datfile = None
            if filename in self.cfm.clientfiles:
                cf = self.cfm.clientfiles[filename]
                datfile = os.fdopen(os.dup(cf.fileno()), cf.mode)
            if datfile is None and not os.access(filename, os.R_OK):
                errstr =  ('{0} is not readable by confluent on {1} '
                           '(ensure confluent user or group can access file '
                           'and parent directories)').format(
                               filename, socket.gethostname())
                self.output.put(msg.ConfluentNodeError(self.node, errstr))
                return
            try:
                self.ipmicmd.apply_license(filename, data=datfile)
            finally:
                if datfile is not None:
                    datfile.close()
        if len(self.element) == 3:
            self.output.put(msg.ChildCollection('all'))
            i = 1
            for lic in self.ipmicmd.get_licenses():
                self.output.put(msg.ChildCollection(str(i)))
                i += 1
            return
        licname = self.element[3]
        if licname == 'all':
            for lic in self.ipmicmd.get_licenses():
                if self.op == 'delete':
                    self.ipmicmd.delete_license(lic['name'])
                else:
                    self.output.put(msg.License(self.node, feature=lic['name'], state=lic.get('state', 'Active')))
        else:
            index = int(licname)
            lic = list(self.ipmicmd.get_licenses())[index - 1]
            if self.op == 'delete':
                self.ipmicmd.delete_license(lic['name'])
            else:
                self.output.put(msg.License(self.node, feature=lic['name'], state=lic.get('state', 'Active')))
    def handle_description(self):
        dsc = self.ipmicmd.get_description()
        self.output.put(msg.KeyValueData(dsc, self.node))

    def handle_graphical_console(self):
        args = self.ipmicmd.get_graphical_console()
        m = msg.GraphicalConsole(self.node, *args)
        self.output.put(m)
        return


def _str_health(health):
    if isinstance(health, str):
        return health
    if pygconstants.Health.Failed & health:
        health = 'failed'
    elif pygconstants.Health.Critical & health:
        health = 'critical'
    elif pygconstants.Health.Warning & health:
        health = 'warning'
    else:
        health = 'ok'
    return health


def initthread():
    global _ipmithread
    if _ipmithread is None:
        _ipmithread = eventlet.spawn(_ipmi_evtloop)


def create(nodes, element, configmanager, inputdata, realop='create'):
    initthread()
    if element == ['_console', 'session']:
        if len(nodes) > 1:
            raise Exception("_console/session does not support multiple nodes")
        return IpmiConsole(nodes[0], configmanager)
    else:
        return perform_requests(
            'update', nodes, element, configmanager, inputdata, realop)


def update(nodes, element, configmanager, inputdata):
    initthread()
    return create(nodes, element, configmanager, inputdata, 'update')


def retrieve(nodes, element, configmanager, inputdata):
    initthread()
    if '/'.join(element).startswith('inventory/firmware/updates/active'):
        return firmwaremanager.list_updates(nodes, configmanager.tenant,
                                            element)
    elif '/'.join(element).startswith('media/uploads'):
        return firmwaremanager.list_updates(nodes, configmanager.tenant,
                                            element, 'mediaupload')
    elif '/'.join(element).startswith('support/servicedata'):
        return firmwaremanager.list_updates(nodes, configmanager.tenant,
                                            element, 'ffdc')
    else:
        return perform_requests('read', nodes, element, configmanager,
                                inputdata, 'read')

def delete(nodes, element, configmanager, inputdata):
    initthread()
    if '/'.join(element).startswith('inventory/firmware/updates/active'):
        return firmwaremanager.remove_updates(nodes, configmanager.tenant,
                                              element)
    elif '/'.join(element).startswith('media/uploads'):
        return firmwaremanager.remove_updates(nodes, configmanager.tenant,
                                              element, type='mediaupload')
    elif '/'.join(element).startswith('support/servicedata'):
        return firmwaremanager.remove_updates(nodes, configmanager.tenant,
                                              element, type='ffdc')
    return perform_requests(
        'delete', nodes, element, configmanager, inputdata, 'delete')

