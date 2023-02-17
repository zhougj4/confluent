# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2018 Lenovo
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

import base64
import confluent.collective.invites as invites
import confluent.config.configmanager as cfm
import confluent.exceptions as exc
import confluent.log as log
import confluent.noderange as noderange
import confluent.tlvdata as tlvdata
import confluent.util as util
import eventlet
import eventlet.greenpool as greenpool
import eventlet.green.socket as socket
import eventlet.green.ssl as ssl
import eventlet.green.threading as threading
import confluent.sortutil as sortutil
import greenlet
import random
import time
import sys
try:
    import OpenSSL.crypto as crypto
except ImportError:
    # while not always required, we use pyopenssl required for at least
    # collective
    crypto = None

currentleader = None
follower = None
retrythread = None
failovercheck = None
initting = True
reassimilate = None

class ContextBool(object):
    def __init__(self):
        self.active = False
        self.mylock = threading.RLock()

    def __enter__(self):
        self.active = True
        self.mylock.__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.active = False
        self.mylock.__exit__(exc_type, exc_val, exc_tb)

connecting = ContextBool()
leader_init = ContextBool()

def connect_to_leader(cert=None, name=None, leader=None, remote=None):
    global currentleader
    global follower
    if leader is None:
        leader = currentleader
    log.log({'info': 'Attempting connection to leader {0}'.format(leader),
             'subsystem': 'collective'})
    try:
        remote = connect_to_collective(cert, leader, remote)
    except Exception as e:
        log.log({'error': 'Collective connection attempt to {0} failed: {1}'
                          ''.format(leader, str(e)),
                 'subsystem': 'collective'})
        return False
    with connecting:
        with cfm._initlock:
            banner = tlvdata.recv(remote)  # the banner
            vers = banner.split()[2]
            if vers not in (b'v2', b'v3'):
                raise Exception('This instance only supports protocol 2 or 3, synchronize versions between collective members')
            tlvdata.recv(remote)  # authpassed... 0..
            if name is None:
                name = get_myname()
            tlvdata.send(remote, {'collective': {'operation': 'connect',
                                                 'name': name,
                                                 'txcount': cfm._txcount}})
            keydata = tlvdata.recv(remote)
            if not keydata:
                return False
            if 'error' in keydata:
                if 'backoff' in keydata:
                    log.log({
                        'info': 'Collective initialization in progress on '
                                '{0}'.format(leader),
                        'subsystem': 'collective'})
                    return False
                if 'leader' in keydata:
                    log.log(
                        {'info': 'Prospective leader {0} has redirected this '
                                 'member to {1}'.format(leader, keydata['leader']),
                         'subsystem': 'collective'})
                    ldrc = cfm.get_collective_member_by_address(
                        keydata['leader'])
                    if ldrc and ldrc['name'] == name:
                        raise Exception("Redirected to self")
                    return connect_to_leader(name=name,
                                             leader=keydata['leader'])
                if 'txcount' in keydata:
                    log.log({'info':
                                 'Prospective leader {0} has inferior '
                                 'transaction count, becoming leader'
                                 ''.format(leader), 'subsystem': 'collective',
                             'subsystem': 'collective'})
                    return become_leader(remote)
                return False
                follower.kill()
                cfm.stop_following()
                follower = None
            if follower is not None:
                follower.kill()
                cfm.stop_following()
                follower = None
            log.log({'info': 'Following leader {0}'.format(leader),
                     'subsystem': 'collective'})
            colldata = tlvdata.recv(remote)
            globaldata = tlvdata.recv(remote)
            dbi = tlvdata.recv(remote)
            dbsize = dbi['dbsize']
            dbjson = b''
            while (len(dbjson) < dbsize):
                ndata = remote.recv(dbsize - len(dbjson))
                if not ndata:
                    try:
                        remote.close()
                    except Exception:
                        pass
                    raise Exception("Error doing initial DB transfer")
                dbjson += ndata
            cfm.clear_configuration()
            try:
                cfm._restore_keys(keydata, None, sync=False)
                for c in colldata:
                    cfm._true_add_collective_member(c, colldata[c]['address'],
                                                    colldata[c]['fingerprint'],
                                                    sync=False)
                for globvar in globaldata:
                    cfm.set_global(globvar, globaldata[globvar], False)
                cfm._txcount = dbi.get('txcount', 0)
                cfm.ConfigManager(tenant=None)._load_from_json(dbjson,
                                                               sync=False)
                cfm.commit_clear()
            except Exception:
                cfm.stop_following()
                cfm.rollback_clear()
                raise
            currentleader = leader
        #spawn this as a thread...
        remote.settimeout(90)
        follower = eventlet.spawn(follow_leader, remote, leader)
    return True


def follow_leader(remote, leader):
    global currentleader
    global retrythread
    global follower
    cleanexit = False
    newleader = None
    try:
        exitcause = cfm.follow_channel(remote)
        newleader = exitcause.get('newleader', None)
    except greenlet.GreenletExit:
        cleanexit = True
    finally:
        if cleanexit:
            log.log({'info': 'Previous following cleanly closed',
                     'subsystem': 'collective'})
            return
        if newleader:
            log.log(
                {'info': 'Previous leader directed us to join new leader {}'.format(newleader)})
            try:
                if connect_to_leader(None, get_myname(), newleader):
                    return
            except Exception:
                log.log({'error': 'Unknown error attempting to connect to {}, check trace log'.format(newleader), 'subsystem': 'collective'})
                cfm.logException()
        log.log({'info': 'Current leader ({0}) has disappeared, restarting '
                         'collective membership'.format(leader), 'subsystem': 'collective'})
        # The leader has folded, time to startup again...
        follower = None
        cfm.stop_following()
        currentleader = None
        if retrythread is None:  # start a recovery
            retrythread = eventlet.spawn_after(
                random.random(), start_collective)

def create_connection(member):
        remote = None
        try:
            remote = socket.create_connection((member, 13001), 2)
            remote.settimeout(15)
            # TLS cert validation is custom and will not pass normal CA vetting
            # to override completely in the right place requires enormous effort, so just defer until after connect
            remote = ssl.wrap_socket(remote, cert_reqs=ssl.CERT_NONE, keyfile='/etc/confluent/privkey.pem',
                                    certfile='/etc/confluent/srvcert.pem')
        except Exception as e:
            return member, e
        return member, remote

def connect_to_collective(cert, member, remote=None):
    if remote is None:
        _, remote = create_connection(member)
        if isinstance(remote, Exception):
            raise remote
    if cert:
        fprint = cert
    else:
        collent = cfm.get_collective_member_by_address(member)
        fprint = collent['fingerprint']
    if not util.cert_matches(fprint, remote.getpeercert(binary_form=True)):
        # probably Janeway up to something
        raise Exception("Certificate mismatch in the collective")
    return remote

mycachedname = [None, 0]
def get_myname():
    if mycachedname[1] > time.time() - 15:
        return mycachedname[0]
    try:
        with open('/etc/confluent/cfg/myname', 'r') as f:
            mycachedname[0] = f.read().strip()
            mycachedname[1] = time.time()
            return mycachedname[0]
    except IOError:
        myname = socket.gethostname().split('.')[0]
        with open('/etc/confluent/cfg/myname', 'w') as f:
            f.write(myname)
        mycachedname[0] = myname
        mycachedname[1] = time.time()
        return myname

def handle_connection(connection, cert, request, local=False):
    global currentleader
    global retrythread
    global initting
    connection.settimeout(5)
    operation = request['operation']
    if cert:
        cert = crypto.dump_certificate(crypto.FILETYPE_ASN1, cert)
    else:
        if not local:
            return
        if operation in ('show', 'delete'):
            if not list(cfm.list_collective()):
                tlvdata.send(connection,
                             {'collective': {'error': 'Collective mode not '
                                                      'enabled on this '
                                                      'system'}})
                return
            if follower is not None:
                linfo = cfm.get_collective_member_by_address(currentleader)
                remote = socket.create_connection((currentleader, 13001), 15)
                remote = ssl.wrap_socket(remote, cert_reqs=ssl.CERT_NONE,
                                         keyfile='/etc/confluent/privkey.pem',
                                         certfile='/etc/confluent/srvcert.pem')
                cert = remote.getpeercert(binary_form=True)
                if not (linfo and util.cert_matches(
                        linfo['fingerprint'],
                        cert)):
                    remote.close()
                    tlvdata.send(connection,
                                 {'error': 'Invalid certificate, '
                                           'redo invitation process'})
                    connection.close()
                    return
                tlvdata.recv(remote)  # ignore banner
                tlvdata.recv(remote)  # ignore authpassed: 0
                tlvdata.send(remote,
                             {'collective': {'operation': 'getinfo',
                                             'name': get_myname()}})
                collinfo = tlvdata.recv(remote)
            else:
                collinfo = {}
                populate_collinfo(collinfo)
            try:
                cfm.check_quorum()
                collinfo['quorum'] = True
            except exc.DegradedCollective:
                collinfo['quorum'] = False
            if operation == 'show':
                tlvdata.send(connection, {'collective':  collinfo})
            elif operation == 'delete':
                todelete = request['member']
                if (todelete == collinfo['leader'] or 
                       todelete in collinfo['active']):
                    tlvdata.send(connection, {'collective':
                            {'error': '{0} is still active, stop the confluent service to remove it'.format(todelete)}})
                    return
                if todelete not in collinfo['offline']:
                    tlvdata.send(connection, {'collective':
                            {'error': '{0} is not a recognized collective member'.format(todelete)}})
                    return
                cfm.del_collective_member(todelete)
                tlvdata.send(connection,
                    {'collective': {'status': 'Successfully deleted {0}'.format(todelete)}})
                connection.close()
            return
        if 'invite' == operation:
            try:
                cfm.check_quorum()
            except exc.DegradedCollective:
                tlvdata.send(connection,
                    {'collective':
                         {'error': 'Collective does not have quorum'}})
                return
            #TODO(jjohnson2): Cannot do the invitation if not the head node, the certificate hand-carrying
            #can't work in such a case.
            name = request['name']
            invitation = invites.create_server_invitation(name)
            tlvdata.send(connection,
                         {'collective': {'invitation': invitation}})
            connection.close()
        if 'join' == operation:
            invitation = request['invitation']
            try:
                invitation = base64.b64decode(invitation)
                name, invitation = invitation.split(b'@', 1)
                name = util.stringify(name)
            except Exception:
                tlvdata.send(
                    connection,
                    {'collective':
                         {'status': 'Invalid token format'}})
                connection.close()
                return
            host = request['server']
            try:
                remote = socket.create_connection((host, 13001), 15)
                # This isn't what it looks like.  We do CERT_NONE to disable
                # openssl verification, but then use the invitation as a
                # shared secret to validate the certs as part of the join
                # operation
                remote = ssl.wrap_socket(remote,  cert_reqs=ssl.CERT_NONE,
                                         keyfile='/etc/confluent/privkey.pem',
                                         certfile='/etc/confluent/srvcert.pem')
            except Exception:
                tlvdata.send(
                    connection,
                    {'collective':
                         {'status': 'Failed to connect to {0}'.format(host)}})
                connection.close()
                return
            mycert = util.get_certificate_from_file(
                '/etc/confluent/srvcert.pem')
            cert = remote.getpeercert(binary_form=True)
            proof = base64.b64encode(invites.create_client_proof(
                invitation, mycert, cert))
            tlvdata.recv(remote)  # ignore banner
            tlvdata.recv(remote)  # ignore authpassed: 0
            tlvdata.send(remote, {'collective': {'operation': 'enroll',
                                                 'name': name, 'hmac': proof}})
            rsp = tlvdata.recv(remote)
            if 'error' in rsp:
                tlvdata.send(connection, {'collective':
                                              {'status': rsp['error']}})
                connection.close()
                return
            proof = rsp['collective']['approval']
            proof = base64.b64decode(proof)
            j = invites.check_server_proof(invitation, mycert, cert, proof)
            if not j:
                remote.close()
                tlvdata.send(connection, {'collective':
                                              {'status': 'Bad server token'}})
                connection.close()
                return
            tlvdata.send(connection, {'collective': {'status': 'Success'}})
            connection.close()
            currentleader = rsp['collective']['leader']
            f = open('/etc/confluent/cfg/myname', 'w')
            f.write(name)
            f.close()
            log.log({'info': 'Connecting to collective due to join',
                     'subsystem': 'collective'})
            eventlet.spawn_n(connect_to_leader, rsp['collective'][
                'fingerprint'], name)
    if 'enroll' == operation:
        #TODO(jjohnson2): error appropriately when asked to enroll, but the master is elsewhere
        mycert = util.get_certificate_from_file('/etc/confluent/srvcert.pem')
        proof = base64.b64decode(request['hmac'])
        myrsp = invites.check_client_proof(request['name'], mycert,
                                           cert, proof)
        if not myrsp:
            tlvdata.send(connection, {'error': 'Invalid token'})
            connection.close()
            return
        if not list(cfm.list_collective()):
            # First enrollment of a collective, since the collective doesn't
            # quite exist, then set initting false to let the enrollment action
            # drive this particular initialization
            initting = False
        myrsp = base64.b64encode(myrsp)
        fprint = util.get_fingerprint(cert)
        myfprint = util.get_fingerprint(mycert)
        cfm.add_collective_member(get_myname(),
                                  connection.getsockname()[0], myfprint)
        cfm.add_collective_member(request['name'],
                                  connection.getpeername()[0], fprint)
        myleader = get_leader(connection)
        ldrfprint = cfm.get_collective_member_by_address(
            myleader)['fingerprint']
        tlvdata.send(connection,
                     {'collective': {'approval': myrsp,
                                     'fingerprint': ldrfprint,
                                     'leader': get_leader(connection)}})
    if 'assimilate' == operation:
        drone = request['name']
        droneinfo = cfm.get_collective_member(drone)
        if not droneinfo:
            tlvdata.send(connection,
                         {'error': 'Unrecognized leader, '
                                   'redo invitation process'})
            return
        if not util.cert_matches(droneinfo['fingerprint'], cert):
            tlvdata.send(connection,
                         {'error': 'Invalid certificate, '
                                   'redo invitation process'})
            return
        if request['txcount'] < cfm._txcount:
            tlvdata.send(connection,
                         {'error': 'Refusing to be assimilated by inferior'
                                   'transaction count',
                          'txcount': cfm._txcount,})
            return
        if cfm.cfgstreams and request['txcount'] == cfm._txcount:
            try:
                cfm.check_quorum()
                tlvdata.send(connection,
                         {'error': 'Refusing to be assimilated as I am a leader with quorum',
                          'txcount': cfm._txcount,})
                return
            except exc.DegradedCollective:
                followcount = request.get('followcount', None)
                myfollowcount = len(list(cfm.cfgstreams))
                if followcount is not None:
                    if followcount < myfollowcount:
                        tlvdata.send(connection,
                             {'error': 'Refusing to be assimilated by leader with fewer followers',
                            'txcount': cfm._txcount,})
                        return
                    elif followcount == myfollowcount:
                        myname = sortutil.naturalize_string(get_myname())
                        if myname < sortutil.naturalize_string(request['name']):
                            tlvdata.send(connection,
                                {'error': 'Refusing, my name is better',
                                'txcount': cfm._txcount,})
                            return
        if follower is not None and not follower.dead:
            tlvdata.send(
                connection,
                {'error': 'Already following, assimilate leader first',
                 'leader': currentleader})
            connection.close()
            return
        if connecting.active:
            # don't try to connect while actively already trying to connect
            tlvdata.send(connection, {'status': 0})
            connection.close()
            return
        if (currentleader == connection.getpeername()[0] and
                follower and not follower.dead):
            # if we are happily following this leader already, don't stir
            # the pot
            tlvdata.send(connection, {'status': 0})
            connection.close()
            return
        log.log({'info': 'Connecting in response to assimilation',
                 'subsystem': 'collective'})
        newleader = connection.getpeername()[0]
        if cfm.cfgstreams:
            retire_as_leader(newleader)
        tlvdata.send(connection, {'status': 0})
        connection.close()
        if not connect_to_leader(None, None, leader=newleader):
            if retrythread is None:
                retrythread = eventlet.spawn_after(random.random(),
                                                   start_collective)
    if 'getinfo' == operation:
        drone = request['name']
        droneinfo = cfm.get_collective_member(drone)
        if not (droneinfo and util.cert_matches(droneinfo['fingerprint'],
                                                cert)):
            tlvdata.send(connection,
                         {'error': 'Invalid certificate, '
                                   'redo invitation process'})
            connection.close()
            return
        collinfo = {}
        populate_collinfo(collinfo)
        tlvdata.send(connection, collinfo)
    if 'connect' == operation:
        drone = request['name']
        droneinfo = cfm.get_collective_member(drone)
        if not (droneinfo and util.cert_matches(droneinfo['fingerprint'],
                                                cert)):
            tlvdata.send(connection,
                         {'error': 'Invalid certificate, '
                                   'redo invitation process'})
            connection.close()
            return
        myself = connection.getsockname()[0]
        if connecting.active or initting:
            tlvdata.send(connection, {'error': 'Connecting right now',
                                      'backoff': True})
            connection.close()
            return
        if myself != get_leader(connection):
            tlvdata.send(
                connection,
                {'error': 'Cannot assimilate, our leader is '
                          'in another castle', 'leader': currentleader})
            connection.close()
            return
        if request['txcount'] > cfm._txcount:
            retire_as_leader()
            tlvdata.send(connection,
                         {'error': 'Client has higher tranasaction count, '
                                   'should assimilate me, connecting..',
                          'txcount': cfm._txcount})
            log.log({'info': 'Connecting to leader due to superior '
                             'transaction count', 'subsystem': 'collective'})
            connection.close()
            if not connect_to_leader(
                None, None, connection.getpeername()[0]):
                if retrythread is None:
                    retrythread = eventlet.spawn_after(5 + random.random(),
                                                   start_collective)
            return
        if retrythread is not None:
            retrythread.cancel()
            retrythread = None
        with leader_init:
            cfm.update_collective_address(request['name'],
                                          connection.getpeername()[0])
            tlvdata.send(connection, cfm._dump_keys(None, False))
            tlvdata.send(connection, cfm._cfgstore['collective'])
            tlvdata.send(connection, {'confluent_uuid': cfm.get_global('confluent_uuid')}) # cfm.get_globals())
            cfgdata = cfm.ConfigManager(None)._dump_to_json()
            tlvdata.send(connection, {'txcount': cfm._txcount,
                                      'dbsize': len(cfgdata)})
            connection.sendall(cfgdata)
        #tlvdata.send(connection, {'tenants': 0}) # skip the tenants for now,
        # so far unused anyway
        connection.settimeout(90)
        if not cfm.relay_slaved_requests(drone, connection):
            log.log({'info': 'All clients have disconnected, starting recovery process',
                     'subsystem': 'collective'})
            if retrythread is None:  # start a recovery if everyone else seems
                # to have disappeared
                retrythread = eventlet.spawn_after(5 + random.random(),
                                                   start_collective)
        # ok, we have a connecting member whose certificate checks out
        # He needs to bootstrap his configuration and subscribe it to updates


def populate_collinfo(collinfo):
    iam = get_myname()
    collinfo['leader'] = iam
    collinfo['active'] = list(cfm.cfgstreams)
    activemembers = set(cfm.cfgstreams)
    activemembers.add(iam)
    collinfo['offline'] = []
    for member in cfm.list_collective():
        if member not in activemembers:
            collinfo['offline'].append(member)


def try_assimilate(drone, followcount, remote):
    global retrythread
    try:
        remote = connect_to_collective(None, drone, remote)
    except socket.error:
        # Oh well, unable to connect, hopefully the rest will be
        # in order
        return
    tlvdata.send(remote, {'collective': {'operation': 'assimilate',
                                         'name': get_myname(),
                                         'followcount': followcount,
                                         'txcount': cfm._txcount}})
    tlvdata.recv(remote)  # the banner
    tlvdata.recv(remote)  # authpassed... 0..
    answer = tlvdata.recv(remote)
    if not answer:
        log.log(
            {'error':
                 'No answer from {0} while trying to assimilate'.format(
                     drone),
            'subsystem': 'collective'})
        return True
    if 'txcount' in answer:
        log.log({'info': 'Deferring to {0} due to target being a better leader'.format(
            drone), 'subsystem': 'collective'})
        retire_as_leader(drone)
        if not connect_to_leader(None, None, leader=remote.getpeername()[0]):
            if retrythread is None:
                retrythread = eventlet.spawn_after(random.random(),
                                                    start_collective)
        return False
    if 'leader' in answer:
        # Will wait for leader to see about assimilation
        return True
    if 'error' in answer:
        log.log({
            'error': 'Error encountered while attempting to '
                     'assimilate {0}: {1}'.format(drone, answer['error']),
            'subsystem': 'collective'})
        return True
    log.log({'info': 'Assimilated {0} into collective'.format(drone),
             'subsystem': 'collective'})
    return True


def get_leader(connection):
    if currentleader is None or connection.getpeername()[0] == currentleader:
        # cancel retry if a retry is pending
        if currentleader is None:
            msg = 'Becoming leader as no leader known'
        else:
            msg = 'Becoming leader because {0} attempted to connect and it ' \
                  'is current leader'.format(currentleader)
        log.log({'info': msg, 'subsystem': 'collective'})
        become_leader(connection)
    return currentleader

def retire_as_leader(newleader=None):
    global currentleader
    cfm.stop_leading(newleader)
    currentleader = None

def become_leader(connection):
    global currentleader
    global follower
    global retrythread
    global reassimilate
    log.log({'info': 'Becoming leader of collective',
             'subsystem': 'collective'})
    if follower is not None:
        follower.kill()
        cfm.stop_following()
        follower = None
    if retrythread is not None:
        retrythread.cancel()
        retrythread = None
    currentleader = connection.getsockname()[0]
    skipaddr = connection.getpeername()[0]
    if _assimilate_missing(skipaddr):
        schedule_rebalance()
        if reassimilate is not None:
            reassimilate.kill()
        reassimilate = eventlet.spawn(reassimilate_missing)

def reassimilate_missing():
    eventlet.sleep(30)
    while cfm.cfgstreams and _assimilate_missing():
        eventlet.sleep(30)

def _assimilate_missing(skipaddr=None):
    connecto = []
    myname = get_myname()
    skipem = set(cfm.cfgstreams)
    numfollowers = len(skipem)
    skipem.add(currentleader)
    if skipaddr is not None:
        skipem.add(skipaddr)
    for member in cfm.list_collective():
        dronecandidate = cfm.get_collective_member(member)['address']
        if dronecandidate in skipem or member == myname or member in skipem:
            continue
        connecto.append(dronecandidate)
    if not connecto:
        return True
    conpool = greenpool.GreenPool(64)
    connections = conpool.imap(create_connection, connecto)
    for ent in connections:
        member, remote = ent
        if isinstance(remote, Exception):
            continue
        if not try_assimilate(member, numfollowers, remote):
            return False
    return True


def startup():
    members = list(cfm.list_collective())
    if len(members) < 2:
        # Not in collective mode, return
        return
    eventlet.spawn_n(start_collective)

def check_managers():
    global failovercheck
    if not follower:
        try:
            cfm.check_quorum()
        except exc.DegradedCollective:
            failovercheck = None
            return
        c = cfm.ConfigManager(None)
        collinfo = {}
        populate_collinfo(collinfo)
        availmanagers = {}
        offlinemgrs = set(collinfo['offline'])
        offlinemgrs.add('')
        for offline in collinfo['offline']:
            nodes = noderange.NodeRange(
                'collective.manager=={}'.format(offline), c).nodes
            managercandidates = c.get_node_attributes(
                nodes, 'collective.managercandidates')
            expandednoderanges = {}
            for node in nodes:
                if node not in managercandidates:
                    continue
                targets = managercandidates[node].get('collective.managercandidates', {}).get('value', None)
                if not targets:
                    continue
                if not availmanagers:
                    for active in collinfo['active']:
                        availmanagers[active] = len(
                            noderange.NodeRange(
                                'collective.manager=={}'.format(active), c).nodes)
                    availmanagers[collinfo['leader']] = len(
                            noderange.NodeRange(
                                'collective.manager=={}'.format(
                                    collinfo['leader']), c).nodes)
                if targets not in expandednoderanges:
                    expandednoderanges[targets] = set(
                        noderange.NodeRange(targets, c).nodes) - offlinemgrs
                targets = sorted(expandednoderanges[targets], key=availmanagers.get)
                if not targets:
                    continue
                c.set_node_attributes({node: {'collective.manager': {'value': targets[0]}}})
                availmanagers[targets[0]] += 1
        _assimilate_missing()
    failovercheck = None

def schedule_rebalance():
    global failovercheck
    if not failovercheck:
        failovercheck = True
        failovercheck = eventlet.spawn_after(10, check_managers)

def start_collective():
    global follower
    global retrythread
    global initting
    initting = True
    retrythread = None
    try:
        cfm.membership_callback = schedule_rebalance
        if follower is not None:
            initting = False
            return
        try:
            if cfm.cfgstreams:
                cfm.check_quorum()
                # Do not start if we have quorum and are leader
                return
        except exc.DegradedCollective:
            pass
        if leader_init.active:  # do not start trying to connect if we are
            # xmitting data to a follower
            return
        myname = get_myname()
        connecto = []
        for member in sorted(list(cfm.list_collective())):
            if member == myname:
                continue
            if cfm.cfgleader is None:
                cfm.stop_following(True)
            ldrcandidate = cfm.get_collective_member(member)['address']
            connecto.append(ldrcandidate)
        conpool = greenpool.GreenPool(64)
        connections = conpool.imap(create_connection, connecto)
        for ent in connections:
            member, remote = ent
            if isinstance(remote, Exception):
                continue
            if follower is None:
                log.log({'info': 'Performing startup attempt to {0}'.format(
                    member), 'subsystem': 'collective'})
                if not connect_to_leader(name=myname, leader=member, remote=remote):
                    remote.close()
            else:
                remote.close()
    except Exception as e:
        pass
    finally:
        if retrythread is None and follower is None:
            retrythread = eventlet.spawn_after(5 + random.random(),
                                               start_collective)
        initting = False
