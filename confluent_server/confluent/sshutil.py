#!/usr/bin/python

import base64
import confluent.config.configmanager as cfm
import confluent.collective.manager as collective
import confluent.util as util
import eventlet.green.subprocess as subprocess
import eventlet
import glob
import os
import shutil
import tempfile

agent_pid = None
ready_keys = {}
_sshver = None

def sshver():
    global _sshver
    if _sshver is None:
        p = subprocess.Popen(['ssh', '-V'], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        _, output = p.communicate()
        _sshver = float(output.split()[0].split(b'_')[1].split(b'p')[0])
    return _sshver

def normalize_uid():
    curruid = os.geteuid()
    neededuid = os.stat('/etc/confluent').st_uid
    if curruid != neededuid:
        os.seteuid(neededuid)
    if os.geteuid() != neededuid:
        raise Exception('Need to run as root or owner of /etc/confluent')
    return curruid

agent_starting = False
def assure_agent():
    global agent_starting
    global agent_pid
    while agent_starting:
        eventlet.sleep(0.1)
    if agent_pid is None:
        try:
            agent_starting = True
            sai = util.run(['ssh-agent'])[0]
            for line in sai.split(b'\n'):
                if b';' not in line:
                    continue
                line, _ = line.split(b';', 1)
                if b'=' not in line:
                    continue
                k, v = line.split(b'=', 1)
                if not isinstance(k, str):
                    k = k.decode('utf8')
                    v = v.decode('utf8')
                if k == 'SSH_AGENT_PID':
                    agent_pid = v
                os.environ[k] = v
        finally:
            agent_starting = False
    return True

def get_passphrase():
    if sshver() <= 7.6:
        return ''
    # convert the master key to base64
    # for use in ssh passphrase context
    if cfm._masterkey is None:
        cfm.init_masterkey()
    phrase = base64.b64encode(cfm._masterkey)
    if not isinstance(phrase, str):
        phrase = phrase.decode('utf8')
    return phrase

class AlreadyExists(Exception):
    pass

def initialize_ca():
    ouid = normalize_uid()
    # if already there, skip, make warning
    myname = collective.get_myname()
    if os.path.exists('/etc/confluent/ssh/ca.pub'):
        cafilename = '/var/lib/confluent/public/site/ssh/{0}.ca'.format(myname)
        shutil.copy('/etc/confluent/ssh/ca.pub', cafilename)
        os.seteuid(ouid)
        raise AlreadyExists()
    try:
        os.makedirs('/etc/confluent/ssh', mode=0o700)
    except OSError as e:
        if e.errno != 17:
            raise
    finally:
        os.seteuid(ouid)
    comment = '{0} SSH CA'.format(myname)
    subprocess.check_call(
        ['ssh-keygen', '-C', comment, '-t', 'ed25519', '-f',
         '/etc/confluent/ssh/ca', '-N', get_passphrase()],
         preexec_fn=normalize_uid)
    ouid = normalize_uid()
    try:
        os.makedirs('/var/lib/confluent/public/site/ssh/', mode=0o755)
    except OSError as e:
        if e.errno != 17:
            raise
    finally:
        os.seteuid(ouid)
    cafilename = '/var/lib/confluent/public/site/ssh/{0}.ca'.format(myname)
    shutil.copy('/etc/confluent/ssh/ca.pub', cafilename)
    #    newent = '@cert-authority * ' + capub.read()


adding_key = False
def prep_ssh_key(keyname):
    global adding_key
    while adding_key:
        eventlet.sleep(0.1)
    adding_key = True
    if keyname in ready_keys:
        adding_key = False
        return
    if not assure_agent():
        ready_keys[keyname] = 1
        adding_key = False
        return
    tmpdir = tempfile.mkdtemp()
    try:
        askpass = os.path.join(tmpdir, 'askpass.sh')
        with open(askpass, 'w') as ap:
            ap.write('#!/bin/sh\necho $CONFLUENT_SSH_PASSPHRASE\nrm {0}\n'.format(askpass))
        os.chmod(askpass, 0o700)
        os.environ['CONFLUENT_SSH_PASSPHRASE'] = get_passphrase()
        os.environ['DISPLAY'] = 'NONE'
        os.environ['SSH_ASKPASS'] = askpass
        with open(os.devnull, 'wb') as devnull:
            subprocess.check_output(['ssh-add', keyname], stdin=devnull, stderr=devnull)
        del os.environ['CONFLUENT_SSH_PASSPHRASE']
        ready_keys[keyname] = 1
    finally:
        adding_key = False
        shutil.rmtree(tmpdir)

def sign_host_key(pubkey, nodename, principals=()):
    tmpdir = tempfile.mkdtemp()
    try:
        prep_ssh_key('/etc/confluent/ssh/ca')
        ready_keys['ca.pub'] = 1
        pkeyname = os.path.join(tmpdir, 'hostkey.pub')
        with open(pkeyname, 'wb') as pubfile:
            pubfile.write(pubkey)
        principals = set(principals)
        principals.add(nodename)
        principals = ','.join(sorted(principals))
        flags = '-Us' if sshver() > 7.6 else '-s'
        keyname = '/etc/confluent/ssh/ca.pub' if flags == '-Us' else '/etc/confluent/ssh/ca'
        subprocess.check_call(
            ['ssh-keygen', flags, keyname, '-I', nodename,
             '-n', principals, '-h', pkeyname])
        certname = pkeyname.replace('.pub', '-cert.pub')
        with open(certname) as cert:
            return cert.read()
    finally:
        shutil.rmtree(tmpdir)

def initialize_root_key(generate, automation=False):
    authorized = []
    myname = collective.get_myname()
    alreadyexist = False
    for currkey in glob.glob('/root/.ssh/*.pub'):
        authorized.append(currkey)
    if generate and not authorized and not automation:
        subprocess.check_call(['ssh-keygen', '-t', 'ed25519', '-f', '/root/.ssh/id_ed25519', '-N', ''])
        for currkey in glob.glob('/root/.ssh/*.pub'):
            authorized.append(currkey)
    if automation and generate:
        if os.path.exists('/etc/confluent/ssh/automation'):
            alreadyexist = True
        else:
            subprocess.check_call(
                ['ssh-keygen', '-t', 'ed25519',
                '-f','/etc/confluent/ssh/automation', '-N', get_passphrase(),
                '-C', 'Confluent Automation by {}'.format(myname)],
                preexec_fn=normalize_uid)
        authorized = ['/etc/confluent/ssh/automation.pub']
    ouid = normalize_uid()
    try:
        os.makedirs('/var/lib/confluent/public/site/ssh', mode=0o755)
    except OSError as e:
        if e.errno != 17:
            raise
    finally:
        os.seteuid(ouid)
    neededuid = os.stat('/etc/confluent').st_uid
    if automation:
        suffix = 'automationpubkey'
    else:
        suffix = 'rootpubkey'
    for auth in authorized:
        shutil.copy(
            auth,
            '/var/lib/confluent/public/site/ssh/{0}.{1}'.format(
                    myname, suffix))
        os.chmod('/var/lib/confluent/public/site/ssh/{0}.{1}'.format(
                myname, suffix), 0o644)
        os.chown('/var/lib/confluent/public/site/ssh/{0}.{1}'.format(
                myname, suffix), neededuid, -1)
    if alreadyexist:
        raise AlreadyExists()



def ca_exists():
    return os.path.exists('/etc/confluent/ssh/ca')


if __name__ == '__main__':
    initialize_root_key(True)
    if not ca_exists():
        initialize_ca()
    print(repr(sign_host_key(open('/etc/ssh/ssh_host_ed25519_key.pub').read(), collective.get_myname())))
