# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2017-2022 Lenovo
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

# Documented somewhat at
# http://buildingskb.schneider-electric.com/view.php?AID=15197

# Here is the payload of an SSDP 'announce', sent to the multicast v4/v6 1900
# NOTIFY * HTTP/1.1
# HOST: 239.255.255.250:1900
# CACHE-CONTROL: max-age=1800
# AL: https://172.30.254.151:8080/redfish/v1
# SERVER: Linux/3.14.28-ltsi Redfish/1.0
# NT: urn:dmtf-org:service:redfish-rest:1
# USN: uuid:00000000-0000-0000-0005-000000000001::urn:dmtf-org:service:redfish-rest:1
# NTS: ssdp:alive


import confluent.config.configmanager as cfm
import confluent.collective.manager as collective
import confluent.neighutil as neighutil
import confluent.noderange as noderange
import confluent.util as util
import confluent.log as log
import confluent.netutil as netutil
import eventlet
import eventlet.green.select as select
import eventlet.green.socket as socket
import eventlet.greenpool as gp
import os
import time
import struct
import traceback

webclient = eventlet.import_patched('pyghmi.util.webclient')
mcastv4addr = '239.255.255.250'
mcastv6addr = 'ff02::c'

ssdp6mcast = socket.inet_pton(socket.AF_INET6, mcastv6addr)
smsg = ('M-SEARCH * HTTP/1.1\r\n'
        'HOST: {0}:1900\r\n'
        'MAN: "ssdp:discover"\r\n'
        'ST: {1}\r\n'
        'MX: 3\r\n\r\n')


def active_scan(handler, protocol=None):
    known_peers = set([])
    for scanned in scan(['urn:dmtf-org:service:redfish-rest:1', 'urn::service:affluent']):
        for addr in scanned['addresses']:
            if addr in known_peers:
                break
            hwaddr = neighutil.get_hwaddr(addr[0])
            if not hwaddr:
                continue
            if not scanned.get('hwaddr', None):
                scanned['hwaddr'] = hwaddr
            known_peers.add(addr)
        else:
            scanned['protocol'] = protocol
            handler(scanned)

def scan(services, target=None):
    for service in services:
        for rply in _find_service(service, target):
            yield rply


def _process_snoop(peer, rsp, mac, known_peers, newmacs, peerbymacaddress, byehandler, machandlers, handler):
    if mac in peerbymacaddress and peer not in peerbymacaddress[mac]['addresses']:
        peerbymacaddress[mac]['addresses'].append(peer)
    else:
        peerdata = {
            'hwaddr': mac,
            'addresses': [peer],
        }
        for headline in rsp[1:]:
            if not headline:
                continue
            headline = util.stringify(headline)
            header, _, value = headline.partition(':')
            header = header.strip()
            value = value.strip()
            if header == 'NT':
                if 'redfish-rest' not in value:
                    return
            elif header == 'NTS':
                if value == 'ssdp:byebye':
                    handler = byehandler
                elif value != 'ssdp:alive':
                    handler = None
            elif header == 'AL':
                if not value.endswith('/redfish/v1/'):
                    return
            elif header == 'LOCATION':
                if not value.endswith('/DeviceDescription.json'):
                    return
        if handler:
            eventlet.spawn_n(check_fish_handler, handler, peerdata, known_peers, newmacs, peerbymacaddress, machandlers, mac, peer)

def check_fish_handler(handler, peerdata, known_peers, newmacs, peerbymacaddress, machandlers, mac, peer):
    retdata = check_fish(('/DeviceDescription.json', peerdata))
    if retdata:
        known_peers.add(peer)
        newmacs.add(mac)
        peerbymacaddress[mac] = retdata
        machandlers[mac] = handler


def snoop(handler, byehandler=None, protocol=None, uuidlookup=None):
    """Watch for SSDP notify messages

    The handler shall be called on any service coming online.
    byehandler is called whenever a system advertises that it is departing.
    If no byehandler is specified, byebye messages are ignored.  The handler is
    given (as possible), the mac address, a list of viable sockaddrs to reference
    the peer, and the notification type (e.g.
    'urn:dmtf-org:service:redfish-rest:1'

    :param handler:  A handler for online notifications from network
    :param byehandler: Optional handler for devices going off the network
    """
    # Normally, I like using v6/v4 agnostic socket. However, since we are
    # dabbling in multicast wizardry here, such sockets can cause big problems,
    # so we will have two distinct sockets
    tracelog = log.Logger('trace')
    try:
        active_scan(handler, protocol)
    except Exception as e:
        tracelog.log(traceback.format_exc(), ltype=log.DataTypes.event,
                    event=log.Events.stacktrace)
    known_peers = set([])
    recent_peers = set([])
    net6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    net6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    for ifidx in util.list_interface_indexes():
        v6grp = ssdp6mcast + struct.pack('=I', ifidx)
        net6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, v6grp)
    net6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    net4 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for i4 in util.list_ips():
        ssdp4mcast = socket.inet_pton(socket.AF_INET, mcastv4addr) + \
                     socket.inet_aton(i4['addr'])
        try:
            net4.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            ssdp4mcast)
        except socket.error as e:
            if e.errno != 98:
                # errno 98 can happen if aliased, skip for now
                raise
    net4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    net4.bind(('', 1900))
    net6.bind(('', 1900))
    peerbymacaddress = {}
    while True:
        try:
            newmacs = set([])
            deferrednotifies = []
            machandlers = {}
            r = select.select((net4, net6), (), (), 60)
            if r:
                r = r[0]
            recent_peers = set([])
            while r and len(deferrednotifies) < 256:
                for s in r:
                    (rsp, peer) = s.recvfrom(9000)
                    if rsp[:4] == b'PING':
                        continue
                    if peer in recent_peers:
                        continue
                    rsp = rsp.split(b'\r\n')
                    if b' ' not in rsp[0]:
                        continue
                    method, _ = rsp[0].split(b' ', 1)
                    if method == b'NOTIFY':
                        if peer in known_peers:
                            continue
                        recent_peers.add(peer)
                        mac = neighutil.get_hwaddr(peer[0])
                        if mac == False:
                            # neighutil determined peer ip is not local, skip attempt
                            # to probe and critically, skip growing deferrednotifiers
                            continue
                        if not mac:
                            probepeer = (peer[0], struct.unpack('H', os.urandom(2))[0] | 1025) + peer[2:]
                            try:
                                s.sendto(b'\x00', probepeer)
                            except Exception:
                                continue
                            deferrednotifies.append((peer, rsp))
                            continue
                        _process_snoop(peer, rsp, mac, known_peers, newmacs, peerbymacaddress, byehandler, machandlers, handler)
                    elif method == b'M-SEARCH':
                        if not uuidlookup:
                            continue
                        #ip = peer[0].partition('%')[0]
                        for headline in rsp[1:]:
                            if not headline:
                                continue
                            headline = util.stringify(headline)
                            headline = headline.partition(':')
                            if len(headline) < 3:
                                continue
                            forcereply = False
                            if  headline[0] == 'ST' and headline[-1].startswith(' urn:xcat.org:service:confluent:'):
                                try:
                                    cfm.check_quorum()
                                except Exception:
                                    continue
                                for query in headline[-1].split('/'):
                                    node = None
                                    if query.startswith('confluentuuid='):
                                        myuuid = cfm.get_global('confluent_uuid')
                                        curruuid = query.split('=', 1)[1].lower()
                                        if curruuid != myuuid:
                                            break
                                        forcereply = True
                                    elif query.startswith('allconfluent=1'):
                                        reply = 'HTTP/1.1 200 OK\r\n\r\nCONFLUENT: PRESENT\r\n'
                                        if not isinstance(reply, bytes):
                                            reply = reply.encode('utf8')
                                        s.sendto(reply, peer)
                                    elif query.startswith('uuid='):
                                        curruuid = query.split('=', 1)[1].lower()
                                        node = uuidlookup(curruuid)
                                    elif query.startswith('mac='):
                                        currmac = query.split('=', 1)[1].lower()
                                        node = uuidlookup(currmac)
                                    if node:
                                        cfg = cfm.ConfigManager(None)
                                        cfd = cfg.get_node_attributes(
                                            node, ['deployment.pendingprofile', 'collective.managercandidates'])
                                        if not forcereply:
                                            # Do not bother replying to a node that
                                            # we have no deployment activity
                                            # planned for
                                            if not cfd.get(node, {}).get(
                                                    'deployment.pendingprofile', {}).get('value', None):
                                                break
                                        candmgrs = cfd.get(node, {}).get('collective.managercandidates', {}).get('value', None)
                                        if candmgrs:
                                            candmgrs = noderange.NodeRange(candmgrs, cfg).nodes
                                            if collective.get_myname() not in candmgrs:
                                                break
                                        currtime = time.time()
                                        seconds = int(currtime)
                                        msecs = int(currtime * 1000 % 1000)
                                        reply = 'HTTP/1.1 200 OK\r\nNODENAME: {0}\r\nCURRTIME: {1}\r\nCURRMSECS: {2}\r\n'.format(node, seconds, msecs)
                                        if '%' in peer[0]:
                                            iface = socket.getaddrinfo(peer[0], 0, socket.AF_INET6, socket.SOCK_DGRAM)[0][-1][-1]
                                            reply += 'MGTIFACE: {0}\r\n'.format(
                                                peer[0].split('%', 1)[1])
                                            ncfg = netutil.get_nic_config(
                                                cfg, node, ifidx=iface)
                                            if ncfg.get('matchesnodename', None):
                                                reply += 'DEFAULTNET: 1\r\n'
                                        elif not netutil.address_is_local(peer[0]):
                                            continue
                                        if not isinstance(reply, bytes):
                                            reply = reply.encode('utf8')
                                        s.sendto(reply, peer)
                                        break
                r = select.select((net4, net6), (), (), 0.2)
                if r:
                    r = r[0]
            if deferrednotifies:
                eventlet.sleep(2.2)
            for peerrsp in deferrednotifies:
                peer, rsp = peerrsp
                mac = neighutil.get_hwaddr(peer[0])
                if not mac:
                    continue
                _process_snoop(peer, rsp, mac, known_peers, newmacs, peerbymacaddress, byehandler, machandlers, handler)
            for mac in newmacs:
                thehandler = machandlers.get(mac, None)
                if thehandler:
                    thehandler(peerbymacaddress[mac])
        except Exception:
                tracelog.log(traceback.format_exc(), ltype=log.DataTypes.event,
                             event=log.Events.stacktrace)


def _get_svrip(peerdata):
    for addr in peerdata['addresses']:
        if addr[0].startswith('fe80::'):
            if '%' not in addr[0]:
                return addr[0] + '%{0}'.format(addr[3])
            return addr[0]
    return peerdata['addresses'][0][0]

def _find_service(service, target):
    net4 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    net6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    net6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    if target:
        addrs = socket.getaddrinfo(target, 1900, 0, socket.SOCK_DGRAM)
        for addr in addrs:
            host = addr[4][0]
            if addr[0] == socket.AF_INET:
                msg = smsg.format(host, service)
                if not isinstance(msg, bytes):
                    msg = msg.encode('utf8')
                net4.sendto(msg, addr[4])
            elif addr[0] == socket.AF_INET6:
                host = '[{0}]'.format(host)
                msg = smsg.format(host, service)
                if not isinstance(msg, bytes):
                    msg = msg.encode('utf8')               
                net6.sendto(msg, addr[4])
    else:
        net4.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for idx in util.list_interface_indexes():
            net6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF,
                            idx)
            try:
                msg = smsg.format('[{0}]'.format(mcastv6addr), service)
                if not isinstance(msg, bytes):
                    msg = msg.encode('utf8')
                net6.sendto(msg, (mcastv6addr, 1900, 0, 0))
            except socket.error:
                # ignore interfaces without ipv6 multicast causing error
                pass
        for i4 in util.list_ips():
            if 'broadcast' not in i4:
                continue
            addr = i4['addr']
            bcast = i4['broadcast']
            net4.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                            socket.inet_aton(addr))
            msg = smsg.format(mcastv4addr, service)
            if not isinstance(msg, bytes):
                msg = msg.encode('utf8')
            try:
                net4.sendto(msg, (mcastv4addr, 1900))
            except socket.error as se:
                if se.errno != 101:
                    raise
            msg = smsg.format(bcast, service)
            if not isinstance(msg, bytes):
                msg = msg.encode('utf8')
            net4.sendto(msg, (bcast, 1900))
    # SSDP by spec encourages responses to spread out over a 3 second interval
    # hence we must be a bit more patient
    deadline = util.monotonic_time() + 4
    r, _, _ = select.select((net4, net6), (), (), 4)
    peerdata = {}
    deferparse = []
    while r:
        for s in r:
            (rsp, peer) = s.recvfrom(9000)
            if not neighutil.get_hwaddr(peer[0]):
                probepeer = (peer[0], struct.unpack('H', os.urandom(2))[0] | 1025) + peer[2:]
                try:
                    s.sendto(b'\x00', probepeer)
                except Exception:
                    continue
                deferparse.append((rsp, peer))
                continue
            _parse_ssdp(peer, rsp, peerdata)
        timeout = deadline - util.monotonic_time()
        if timeout < 0:
            timeout = 0
        r, _, _ = select.select((net4, net6), (), (), timeout)
    if deferparse:
        eventlet.sleep(2.2)
    for dp in deferparse:
        rsp, peer = dp
        _parse_ssdp(peer, rsp, peerdata)
    querypool = gp.GreenPool()
    pooltargs = []
    for nid in peerdata:
        if peerdata[nid].get('services', [None])[0] == 'urn::service:affluent:1':
            peerdata[nid]['attributes'] = {
                'type': 'affluent-switch',
            }
            peerdata[nid]['services'] = ['affluent-switch']
            mya = peerdata[nid]['attributes']
            usn = peerdata[nid]['usn']
            idinfo = usn.split('::')
            for idi in idinfo:
                key, val = idi.split(':', 1)
                if key == 'uuid':
                    peerdata[nid]['uuid'] = val
                elif key == 'serial':
                    mya['enclosure-serial-number'] = [val]
                elif key == 'model':
                    mya['enclosure-machinetype-model'] = [val]
            yield peerdata[nid]
            continue
        if '/redfish/v1/' not in peerdata[nid].get('urls', ()) and '/redfish/v1' not in peerdata[nid].get('urls', ()):
            continue
        if '/DeviceDescription.json' in peerdata[nid]['urls']:
            pooltargs.append(('/DeviceDescription.json', peerdata[nid]))
        # For now, don't interrogate generic redfish bmcs
        # This is due to a need to deduplicate from some supported SLP
        # targets (IMM, TSM, others)
        # activate this else once the core filters/merges duplicate uuid
        # or we drop support for those devices
        #else:
        #    pooltargs.append(('/redfish/v1/', peerdata[nid]))
    for pi in querypool.imap(check_fish, pooltargs):
        if pi is not None:
            yield pi

def check_fish(urldata, port=443, verifycallback=None):
    if not verifycallback:
        verifycallback = lambda x: True
    url, data = urldata
    try:
        wc = webclient.SecureHTTPConnection(_get_svrip(data), port, verifycallback=verifycallback, timeout=1.5)
        peerinfo = wc.grab_json_response(url)
    except socket.error:
        return None
    if url == '/DeviceDescription.json':
        try:
            peerinfo = peerinfo[0]
            myuuid = peerinfo['node-uuid'].lower()
            if '-' not in myuuid:
                myuuid = '-'.join([myuuid[:8], myuuid[8:12], myuuid[12:16], myuuid[16:20], myuuid[20:]])
            data['uuid'] = myuuid
            data['attributes'] = peerinfo
            data['services'] = ['lenovo-xcc']
            return data
        except (IndexError, KeyError):
            return None
            url = '/redfish/v1/'
            peerinfo = wc.grab_json_response('/redfish/v1/')
    if url == '/redfish/v1/':
        if 'UUID' in peerinfo:
            data['services'] = ['service:redfish-bmc']
            data['uuid'] = peerinfo['UUID'].lower()
            return data
    return None

def _parse_ssdp(peer, rsp, peerdata):
    nid = peer[0]
    mac = None
    mac = neighutil.get_hwaddr(peer[0])
    if mac:
        nid = mac
    headlines = rsp.split(b'\r\n')
    try:
        _, code, _ = headlines[0].split(b' ', 2)
    except ValueError:
        return
    if code == b'200':
        if nid in peerdata:
            peerdatum = peerdata[nid]
            if peer not in peerdatum['addresses']:
                peerdatum['addresses'].append(peer)
        else:
            peerdatum = {
                'addresses': [peer],
                'hwaddr': mac,
            }
            peerdata[nid] = peerdatum
        for headline in headlines[1:]:
            if not headline:
                continue
            header, _, value = headline.partition(b':')
            header = header.strip().decode('utf8')
            value = value.strip().decode('utf8')
            if header == 'AL' or header == 'LOCATION':
                value = value[value.index('://')+3:]
                value = value[value.index('/'):]
                if 'urls' not in peerdatum:
                    peerdatum['urls'] = [value]
                elif value not in peerdatum['urls']:
                    peerdatum['urls'].append(value)
            elif header == 'ST':
                if 'services' not in peerdatum:
                    peerdatum['services'] = [value]
                elif value not in peerdatum['services']:
                    peerdatum['services'].append(value)
            elif header == 'USN':
                peerdatum['usn'] = value
            elif header == 'MODELNAME':
                peerdatum['modelname'] = value



if __name__ == '__main__':
    def printit(rsp):
        print(repr(rsp))
    active_scan(printit)
