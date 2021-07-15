#!/usr/bin/python
import confluent.exceptions as exc
import confluent.messages as msg
import eventlet
import eventlet.green.select as select
import eventlet.green.subprocess as subprocess
import glob
import logging
logging.getLogger('libarchive').addHandler(logging.NullHandler())
import libarchive
import hashlib
import os
import shutil
import sys
import time
import yaml

COPY = 1
EXTRACT = 2
READFILES = set([
    'README.diskdefines',
    'media.1/products',
    'media.2/products',
    '.DISCINFO',
    '.discinfo',
    'zipl.prm',
])

HEADERSUMS = set([b'\x85\xeddW\x86\xc5\xbdhx\xbe\x81\x18X\x1e\xb4O\x14\x9d\x11\xb7C8\x9b\x97R\x0c-\xb8Ht\xcb\xb3'])
HASHPRINTS = {
    '69d5f1c5e4474d70b0fb5374bfcb29bf57ba828ff00a55237cd757e61ed71048': {'name': 'cumulus-broadcom-amd64-4.0.0', 'method': COPY},
}

from ctypes import byref, c_longlong, c_size_t, c_void_p

from libarchive.ffi import (
    write_disk_new, write_disk_set_options, write_free, write_header,
    read_data_block, write_data_block, write_finish_entry, ARCHIVE_EOF
)

def relax_umask():
    os.umask(0o22)


def makedirs(path, mode):
    try:
        os.makedirs(path, 0o755)
    except OSError as e:
        if e.errno != 17:
            raise

def symlink(src, targ):
    try:
        os.symlink(src, targ)
    except OSError as e:
        if e.errno != 17:
            raise


def update_boot(profilename):
    if profilename.startswith('/var/lib/confluent/public'):
        profiledir = profilename
    else:
        profiledir = '/var/lib/confluent/public/os/{0}'.format(profilename)
    profile = {}
    if profiledir.endswith('/'):
        profiledir = profiledir[:-1]
    profname = os.path.basename(profiledir)
    with open('{0}/profile.yaml'.format(profiledir)) as profileinfo:
        profile = yaml.safe_load(profileinfo)
    label = profile.get('label', profname)
    ostype = profile.get('ostype', 'linux')
    if ostype == 'linux':
        update_boot_linux(profiledir, profile, label)
    elif ostype == 'esxi':
        update_boot_esxi(profiledir, profile, label)

def update_boot_esxi(profiledir, profile, label):
    profname = os.path.basename(profiledir)
    kernelargs = profile.get('kernelargs', '')
    oum = os.umask(0o22)
    bootcfg = open('{0}/distribution/BOOT.CFG'.format(profiledir), 'r').read()
    bootcfg = bootcfg.split('\n')
    newbootcfg = ''
    efibootcfg = ''
    filesneeded = []
    for cfgline in bootcfg:
        if cfgline.startswith('title='):
            newbootcfg += 'title={0}\n'.format(label)
            efibootcfg += 'title={0}\n'.format(label)
        elif cfgline.startswith('kernelopt='):
            newbootcfg += 'kernelopt={0}\n'.format(kernelargs)
            efibootcfg += 'kernelopt={0}\n'.format(kernelargs)
        elif cfgline.startswith('kernel='):
            kern = cfgline.split('=', 1)[1]
            kern = kern.replace('/', '')
            newbootcfg += 'kernel={0}\n'.format(kern)
            efibootcfg += cfgline + '\n'
            filesneeded.append(kern)
        elif cfgline.startswith('modules='):
            modlist = cfgline.split('=', 1)[1]
            mods = modlist.split(' --- ')
            efibootcfg += 'modules=' + ' --- '.join(mods) + ' --- /initramfs/addons.tgz --- /site.tgz\n'
            mods = [x.replace('/', '') for x in mods]
            filesneeded.extend(mods)
            newbootcfg += 'modules=' + ' --- '.join(mods) + ' --- initramfs/addons.tgz --- site.tgz\n'
        else:
            newbootcfg += cfgline + '\n'
            efibootcfg += cfgline + '\n'
    makedirs('{0}/boot/efi/boot/'.format(profiledir), 0o755)
    bcfgout = os.open('{0}/boot/efi/boot/boot.cfg'.format(profiledir), os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o644)
    bcfg = os.fdopen(bcfgout, 'w')
    try:
        bcfg.write(efibootcfg)
    finally:
        bcfg.close()
    bcfgout = os.open('{0}/boot/boot.cfg'.format(profiledir), os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o644)
    bcfg = os.fdopen(bcfgout, 'w')
    try:
        bcfg.write(newbootcfg)
    finally:
        bcfg.close()
    symlink('/var/lib/confluent/public/site/initramfs.tgz',
               '{0}/boot/site.tgz'.format(profiledir))
    for fn in filesneeded:
        if fn.startswith('/'):
            fn = fn[1:]
        sourcefile = '{0}/distribution/{1}'.format(profiledir, fn)
        if not os.path.exists(sourcefile):
            sourcefile = '{0}/distribution/{1}'.format(profiledir, fn.upper())
        symlink(sourcefile, '{0}/boot/{1}'.format(profiledir, fn))
    symlink('{0}/distribution/EFI/BOOT/BOOTX64.EFI'.format(profiledir), '{0}/boot/efi/boot/bootx64.efi'.format(profiledir))
    if os.path.exists('{0}/distribution/EFI/BOOT/CRYPTO64.EFI'.format(profiledir)):
        symlink('{0}/distribution/EFI/BOOT/CRYPTO64.EFI'.format(profiledir), '{0}/boot/efi/boot/crypto64.efi'.format(profiledir))
    ipout = os.open(profiledir + '/boot.ipxe', os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o644)
    ipxeout = os.fdopen(ipout, 'w')
    try:
        os.umask(oum)
        ipxeout.write('#!ipxe\n')
        pname = os.path.split(profiledir)[-1]
        ipxeout.write(
            'chain boot/efi/boot/bootx64.efi -c /confluent-public/os/{0}/boot/boot.cfg'.format(pname))
    finally:
        ipxeout.close()
    subprocess.check_call(
        ['/opt/confluent/bin/dir2img', '{0}/boot'.format(profiledir),
         '{0}/boot.img'.format(profiledir), profname], preexec_fn=relax_umask)


def update_boot_linux(profiledir, profile, label):
    profname = os.path.basename(profiledir)
    kernelargs = profile.get('kernelargs', '')
    grubcfg = "set timeout=5\nmenuentry '"
    grubcfg += label
    grubcfg += "' {\n    linuxefi /kernel " + kernelargs + "\n"
    initrds = []
    for initramfs in glob.glob(profiledir + '/boot/initramfs/*.cpio'):
        initramfs = os.path.basename(initramfs)
        initrds.append(initramfs)
    for initramfs in os.listdir(profiledir + '/boot/initramfs'):
        if initramfs not in initrds:
            initrds.append(initramfs)
    grubcfg += "    initrdefi "
    for initramfs in initrds:
        grubcfg += " /initramfs/{0}".format(initramfs)
    grubcfg += "\n}\n"
    with open(profiledir + '/boot/efi/boot/grub.cfg', 'w') as grubout:
        grubout.write(grubcfg)
    ipxeargs = kernelargs
    for initramfs in initrds:
        ipxeargs += " initrd=" + initramfs
    oum = os.umask(0o22)
    ipout = os.open(profiledir + '/boot.ipxe', os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o644)
    ipxeout = os.fdopen(ipout, 'w')
    try:
        os.umask(oum)
        ipxeout.write('#!ipxe\n')
        ipxeout.write('imgfetch boot/kernel ' + ipxeargs + '\n')
        for initramfs in initrds:
            ipxeout.write('imgfetch boot/initramfs/{0}\n'.format(initramfs))
        ipxeout.write('imgload kernel\nimgexec kernel\n')
    finally:
        ipxeout.close()
    subprocess.check_call(
        ['/opt/confluent/bin/dir2img', '{0}/boot'.format(profiledir),
         '{0}/boot.img'.format(profiledir), profname], preexec_fn=relax_umask)


def extract_entries(entries, flags=0, callback=None, totalsize=None, extractlist=None):
    """Extracts the given archive entries into the current directory.
    """
    buff, size, offset = c_void_p(), c_size_t(), c_longlong()
    buff_p, size_p, offset_p = byref(buff), byref(size), byref(offset)
    sizedone = 0
    printat = 0
    with libarchive.extract.new_archive_write_disk(flags) as write_p:
        for entry in entries:
            if str(entry).endswith('TRANS.TBL'):
                continue
            if extractlist and str(entry) not in extractlist:
                continue
            write_header(write_p, entry._entry_p)
            read_p = entry._archive_p
            while 1:
                r = read_data_block(read_p, buff_p, size_p, offset_p)
                sizedone += size.value
                if callback and time.time() > printat:
                    callback({'progress': float(sizedone) / float(totalsize)})
                    printat = time.time() + 0.5
                if r == ARCHIVE_EOF:
                    break
                write_data_block(write_p, buff, size, offset)
            write_finish_entry(write_p)
            if os.path.isdir(str(entry)):
                os.chmod(str(entry), 0o755)
            else:
                os.chmod(str(entry), 0o644)
    if callback:
        callback({'progress': float(sizedone) / float(totalsize)})
    return float(sizedone) / float(totalsize)


def extract_file(archfile, flags=0, callback=lambda x: None, imginfo=(), extractlist=None):
    """Extracts an archive from a file into the current directory."""
    totalsize = 0
    for img in imginfo:
        if not imginfo[img]:
            continue
        totalsize += imginfo[img]
    dfd = os.dup(archfile.fileno())
    os.lseek(dfd, 0, 0)
    pctdone = 0
    try:
        with libarchive.fd_reader(dfd) as archive:
            pctdone = extract_entries(archive, flags, callback, totalsize,
                                      extractlist)
    finally:
        os.close(dfd)
    return pctdone


def check_rocky(isoinfo):
    ver = None
    arch = None
    cat = None
    for entry in isoinfo[0]:
        if 'rocky-release-8' in entry:
            ver = entry.split('-')[2]
            arch = entry.split('.')[-2]
            cat = 'el8'
            break
    else:
        return None
    if arch == 'noarch' and '.discinfo' in isoinfo[1]:
        prodinfo = isoinfo[1]['.discinfo']
        arch = prodinfo.split(b'\n')[2]
        if not isinstance(arch, str):
            arch = arch.decode('utf-8')
    return {'name': 'rocky-{0}-{1}'.format(ver, arch), 'method': EXTRACT, 'category': cat}


def check_alma(isoinfo):
    ver = None
    arch = None
    cat = None
    for entry in isoinfo[0]:
        if 'almalinux-release-8' in entry:
            ver = entry.split('-')[2]
            arch = entry.split('.')[-2]
            cat = 'el8'
            break
    else:
        return None
    if arch == 'noarch' and '.discinfo' in isoinfo[1]:
        prodinfo = isoinfo[1]['.discinfo']
        arch = prodinfo.split(b'\n')[2]
        if not isinstance(arch, str):
            arch = arch.decode('utf-8')
    return {'name': 'alma-{0}-{1}'.format(ver, arch), 'method': EXTRACT, 'category': cat}


def check_centos(isoinfo):
    ver = None
    arch = None
    cat = None
    isstream = ''
    for entry in isoinfo[0]:
        if 'centos-release-7' in entry:
            dotsplit = entry.split('.')
            arch = dotsplit[-2]
            ver = dotsplit[0].split('release-')[-1].replace('-', '.')
            cat = 'el7'
            break
        elif 'centos-release-8' in entry:
            ver = entry.split('-')[2]
            arch = entry.split('.')[-2]
            cat = 'el8'
            break
        elif 'centos-stream-release-8' in entry:
            ver = entry.split('-')[3]
            arch = entry.split('.')[-2]
            cat = 'el8'
            isstream = '_stream'
            break
        elif 'centos-linux-release-8' in entry:
            ver = entry.split('-')[3]
            arch = entry.split('.')[-2]
            cat = 'el8'
            break
    else:
        return None
    if arch == 'noarch' and '.discinfo' in isoinfo[1]:
        prodinfo = isoinfo[1]['.discinfo']
        arch = prodinfo.split(b'\n')[2]
        if not isinstance(arch, str):
            arch = arch.decode('utf-8')
    return {'name': 'centos{2}-{0}-{1}'.format(ver, arch, isstream), 'method': EXTRACT, 'category': cat}

def check_esxi(isoinfo):
    if '.DISCINFO' not in isoinfo[1]:
        return
    isesxi = False
    version = None
    for line in isoinfo[1]['.DISCINFO'].split(b'\n'):
        if b'ESXi' == line:
            isesxi = True
        if line.startswith(b'Version: '):
            _, version = line.split(b' ', 1)
            if not isinstance(version, str):
                version = version.decode('utf8')
    if isesxi and version:
        return {
            'name': 'esxi-{0}'.format(version),
            'method': EXTRACT,
            'category': 'esxi{0}'.format(version.split('.', 1)[0])
        }

def check_ubuntu(isoinfo):
    if 'README.diskdefines' not in isoinfo[1]:
        return None
    arch = None
    variant = None
    ver = None
    diskdefs = isoinfo[1]['README.diskdefines']
    for info in diskdefs.split(b'\n'):
        if not info:
            continue
        _, key, val = info.split(b' ', 2)
        val = val.strip()
        if key == b'ARCH':
            arch = val
            if arch == b'amd64':
                arch = b'x86_64'
        elif key == b'DISKNAME':
            variant, ver, _ = val.split(b' ', 2)
            if variant != b'Ubuntu-Server':
                return None
    if variant:
        if not isinstance(ver, str):
            ver = ver.decode('utf8')
        if not isinstance(arch, str):
            arch = arch.decode('utf8')
        major = '.'.join(ver.split('.', 2)[:2])
        return {'name': 'ubuntu-{0}-{1}'.format(ver, arch),
                'method': EXTRACT|COPY,
                'extractlist': ['casper/vmlinuz', 'casper/initrd',
                'EFI/BOOT/BOOTx64.EFI', 'EFI/BOOT/grubx64.efi'
                ],
                'copyto': 'install.iso',
                'category': 'ubuntu{0}'.format(major)}


def check_sles(isoinfo):
    ver = None
    arch = 'x86_64'
    disk = None
    distro = ''
    if 'media.1/products' in isoinfo[1]:
        medianame = 'media.1/products'
    elif 'media.2/products' in isoinfo[1]:
        medianame = 'media.2/products'
    else:
        return None
    prodinfo = isoinfo[1][medianame]
    if not isinstance(prodinfo, str):
        prodinfo = prodinfo.decode('utf8')
    prodinfo = prodinfo.split('\n')
    hline = prodinfo[0].split(' ')
    ver = hline[-1].split('-')[0]
    major = ver.split('.', 2)[0]
    if hline[-1].startswith('15'):
        if hline[1] == 'openSUSE-Leap':
            distro = 'opensuse_leap'
        else:
            distro = 'sle'
        if hline[0] == '/' or 'boot' in isoinfo[0]:
            disk = '1'
        elif hline[0].startswith('/Module'):
            disk = '2'
    elif hline[-1].startswith('12'):
        if 'SLES' in hline[1]:
            distro = 'sles'
        if '.1' in medianame:
            disk = '1'
        elif '.2' in medianame:
            disk = '2'
    if disk and distro:
        return {'name': '{0}-{1}-{2}'.format(distro, ver, arch),
                'method': EXTRACT, 'subname': disk,
                'category': 'suse{0}'.format(major)}
    return None


def _priv_check_oraclelinux(isoinfo):
    ver = None
    arch = None
    for entry in isoinfo[0]:
        if 'oraclelinux-release-' in entry and 'release-el7' not in entry:
            ver = entry.split('-')[2]
            arch = entry.split('.')[-2]
            break
    else:
        return None
    major = ver.split('.', 1)[0]
    return {'name': 'oraclelinux-{0}-{1}'.format(ver, arch), 'method': EXTRACT,
            'category': 'el{0}'.format(major)}


def fixup_coreos(targpath):
    # the efi boot image holds content that the init script would want
    # to mcopy, but the boot sector is malformed usually, so change it to 1
    # sector per track
    if os.path.exists(targpath + '/images/efiboot.img'):
        with open(targpath + '/images/efiboot.img', 'rb+') as bootimg:
            bootimg.seek(0x18)
            if bootimg.read != b'\x00\x00':
                bootimg.seek(0x18)
                bootimg.write(b'\x01')


def check_coreos(isoinfo):
    arch = 'x86_64'  # TODO: would check magic of vmlinuz to see which arch
    if 'zipl.prm' in isoinfo[1]:
        prodinfo = isoinfo[1]['zipl.prm']
        if not isinstance(prodinfo, str):
            prodinfo = prodinfo.decode('utf8')
        for inf in prodinfo.split():
            if inf.startswith('coreos.liveiso=rhcos-'):
                ver = inf.split('-')[1]
                return {'name': 'rhcos-{0}-{1}'.format(ver, arch),
                        'method': EXTRACT, 'category': 'coreos'}
            elif inf.startswith('coreos.liveiso=fedore-coreos-'):
                ver = inf.split('-')[2]
                return {'name': 'fedoracoreos-{0}-{1}'.format(ver, arch),
                        'method': EXTRACT, 'category': 'coreos'}



def check_rhel(isoinfo):
    ver = None
    arch = None
    isoracle = _priv_check_oraclelinux(isoinfo)
    if isoracle:
        return isoracle
    for entry in isoinfo[0]:
        if 'redhat-release-7' in entry:
            dotsplit = entry.split('.')
            arch = dotsplit[-2]
            ver = dotsplit[0].split('release-')[-1].replace('-', '.')
            break
        elif 'redhat-release-server-7' in entry:
            dotsplit = entry.split('.')
            arch = dotsplit[-2]
            ver = dotsplit[0].split('release-server-')[-1].replace('-', '.')
            if '.' not in ver:
                minor = dotsplit[1].split('-', 1)[0]
                ver = ver + '.' + minor
            break
        elif 'redhat-release-8' in entry:
            ver = entry.split('-')[2]
            arch = entry.split('.')[-2]
            break
    else:
        if '.discinfo' in isoinfo[1]:
            prodinfo = isoinfo[1]['.discinfo']
            if not isinstance(prodinfo, str):
                prodinfo = prodinfo.decode('utf8')
                prodinfo = prodinfo.split('\n')
                if len(prodinfo) < 3:
                    return None
                arch = prodinfo[2]
                prodinfo = prodinfo[1].split(' ')
                if len(prodinfo) < 2 or prodinfo[0] != 'RHVH':
                    return None
                major = prodinfo[1].split('.')[0]
                cat = 'rhvh{0}'.format(major)
                return {'name': 'rhvh-{0}-{1}'.format(prodinfo[1], arch),
                        'method': EXTRACT, 'category': cat}
        return None
    major = ver.split('.', 1)[0]
    return {'name': 'rhel-{0}-{1}'.format(ver, arch), 'method': EXTRACT, 'category': 'el{0}'.format(major)}


def scan_iso(archive):
    filesizes = {}
    filecontents = {}
    dfd = os.dup(archive.fileno())
    os.lseek(dfd, 0, 0)
    try:
        with libarchive.fd_reader(dfd) as reader:
            for ent in reader:
                if str(ent).endswith('TRANS.TBL'):
                    continue
                eventlet.sleep(0)
                filesizes[str(ent)] = ent.size
                if str(ent) in READFILES:
                    filecontents[str(ent)] = b''
                    for block in ent.get_blocks():
                        filecontents[str(ent)] += bytes(block)
    finally:
        os.close(dfd)
    return filesizes, filecontents


def fingerprint(archive):
    archive.seek(0)
    header = archive.read(32768)
    archive.seek(32769)
    if archive.read(6) == b'CD001\x01':
        # ISO image
        isoinfo = scan_iso(archive)
        name = None
        for fun in globals():
            if fun.startswith('check_'):
                name = globals()[fun](isoinfo)
                if name:
                    return name, isoinfo[0], fun.replace('check_', '')
        return None
    else:
        sum = hashlib.sha256(header)
        if sum.digest() in HEADERSUMS:
            archive.seek(32768)
            chunk = archive.read(32768)
            while chunk:
                sum.update(chunk)
                chunk = archive.read(32768)
            imginfo = HASHPRINTS.get(sum.hexdigest(), None)
            if imginfo:
                return imginfo, None, None


def import_image(filename, callback, backend=False, mfd=None):
    if mfd:
        archive = os.fdopen(int(mfd), 'rb')
    else:
        archive = open(filename, 'rb')
    identity = fingerprint(archive)
    if not identity:
        return -1
    identity, imginfo, funname = identity
    targpath = identity['name']
    distpath = '/var/lib/confluent/distributions/' + targpath
    if identity.get('subname', None):
        targpath += '/' + identity['subname']
    targpath = '/var/lib/confluent/distributions/' + targpath
    os.makedirs(targpath, 0o755)
    filename = os.path.abspath(filename)
    os.chdir(targpath)
    if not backend:
        print('Importing OS to ' + targpath + ':')
    callback({'progress': 0.0})
    pct = 0.0
    if EXTRACT & identity['method']:
        pct = extract_file(archive, callback=callback, imginfo=imginfo,
                           extractlist=identity.get('extractlist', None))
    if COPY & identity['method']:
        basename = identity.get('copyto', os.path.basename(filename))
        targiso = os.path.join(targpath, basename)
        archive.seek(0, 2)
        totalsz = archive.tell()
        currsz = 0
        modpct = 1.0 - pct
        archive.seek(0, 0)
        printat = 0
        with open(targiso, 'wb') as targ:
            buf = archive.read(32768)
            while buf:
                currsz += len(buf)
                pgress = pct + ((float(currsz) / float(totalsz)) * modpct)
                if time.time() > printat:
                    callback({'progress': pgress})
                    printat = time.time() + 0.5
                targ.write(buf)
                buf = archive.read(32768)
    with open(targpath + '/distinfo.yaml', 'w') as distinfo:
        distinfo.write(yaml.dump(identity, default_flow_style=False))
    if 'subname' in identity:
        del identity['subname']
    with open(distpath + '/distinfo.yaml', 'w') as distinfo:
        distinfo.write(yaml.dump(identity, default_flow_style=False))
    if 'fixup_{0}'.format(funname) in globals():
        globals()['fixup_{0}'.format(funname)](targpath)
    callback({'progress': 1.0})
    sys.stdout.write('\n')

def printit(info):
    sys.stdout.write('     \r{:.2f}%'.format(100 * info['progress']))
    sys.stdout.flush()


def list_distros():
    return os.listdir('/var/lib/confluent/distributions')

def list_profiles():
    return os.listdir('/var/lib/confluent/public/os/')

def get_profile_label(profile):
    with open('/var/lib/confluent/public/os/{0}/profile.yaml') as metadata:
        prof = yaml.safe_load(metadata)
    return prof.get('label', profile)

importing = {}


def generate_stock_profiles(defprofile, distpath, targpath, osname,
                            profilelist):
    osd, osversion, arch = osname.split('-')
    bootupdates = []
    for prof in os.listdir('{0}/profiles'.format(defprofile)):
        srcname = '{0}/profiles/{1}'.format(defprofile, prof)
        profname = '{0}-{1}'.format(osname, prof)
        dirname = '/var/lib/confluent/public/os/{0}'.format(profname)
        if os.path.exists(dirname):
            continue
        oumask = os.umask(0o22)
        shutil.copytree(srcname, dirname)
        profdata = None
        try:
            os.makedirs('{0}/boot/initramfs'.format(dirname), 0o755)
        except OSError as e:
            if e.errno != 17:
                raise
        finally:
            os.umask(oumask)
        with open('{0}/profile.yaml'.format(dirname)) as yin:
            profdata = yin.read()
            profdata = profdata.replace('%%DISTRO%%', osd)
            profdata = profdata.replace('%%VERSION%%', osversion)
            profdata = profdata.replace('%%ARCH%%', arch)
            profdata = profdata.replace('%%PROFILE%%', profname)
        if profdata:
            with open('{0}/profile.yaml'.format(dirname), 'w') as yout:
                yout.write(profdata)
        for initrd in os.listdir('{0}/initramfs'.format(defprofile)):
            fullpath = '{0}/initramfs/{1}'.format(defprofile, initrd)
            os.symlink(fullpath,
                       '{0}/boot/initramfs/{1}'.format(dirname, initrd))
        os.symlink(
            '/var/lib/confluent/public/site/initramfs.cpio',
            '{0}/boot/initramfs/site.cpio'.format(dirname))
        os.symlink(distpath, '{0}/distribution'.format(dirname))
        subprocess.check_call(
            ['sh', '{0}/initprofile.sh'.format(dirname),
             targpath, dirname])
        bootupdates.append(eventlet.spawn(update_boot, dirname))
        profilelist.append(profname)
    for upd in bootupdates:
        upd.wait()


class MediaImporter(object):

    def __init__(self, media, cfm=None):
        self.worker = None
        if not os.path.exists('/var/lib/confluent/public'):
            raise Exception('`osdeploy initialize` must be executed before importing any media')
        self.profiles = []
        medfile = None
        if cfm and media in cfm.clientfiles:
            medfile = cfm.clientfiles[media]
        else:
            medfile = open(media, 'rb')
        identity = fingerprint(medfile)
        if not identity:
            raise exc.InvalidArgumentException('Unsupported Media')
        self.percent = 0.0
        identity, _, _ = identity
        self.phase = 'copying'
        if not identity:
            raise Exception('Unrecognized OS Media')
        if 'subname' in identity:
            importkey = '{0}-{1}'.format(identity['name'], identity['subname'])
        else:
            importkey = identity['name']
        if importkey in importing:
            raise Exception('Media import already in progress for this media')
        self.importkey = importkey
        importing[importkey] = self
        self.importkey = importkey
        self.osname = identity['name']
        self.oscategory = identity.get('category', None)
        targpath = identity['name']
        self.distpath = '/var/lib/confluent/distributions/' + targpath
        if identity.get('subname', None):
            targpath += '/' + identity['subname']
        self.targpath = '/var/lib/confluent/distributions/' + targpath
        if os.path.exists(self.targpath):
            del importing[importkey]
            raise Exception('{0} already exists'.format(self.targpath))
        self.filename = os.path.abspath(media)
        self.medfile = medfile
        self.importer = eventlet.spawn(self.importmedia)

    def stop(self):
        if self.worker and self.worker.poll() is None:
            self.worker.kill()

    @property
    def progress(self):
        return {'phase': self.phase, 'progress': self.percent, 'profiles': self.profiles}

    def importmedia(self):
        os.environ['PYTHONPATH'] = ':'.join(sys.path)
        os.environ['CONFLUENT_MEDIAFD'] = '{0}'.format(self.medfile.fileno())
        with open(os.devnull, 'w') as devnull:
            self.worker = subprocess.Popen(
                [sys.executable, __file__, self.filename, '-b'],
                stdin=devnull, stdout=subprocess.PIPE, close_fds=False)
        wkr = self.worker
        currline = b''
        while wkr.poll() is None:
            currline += wkr.stdout.read(1)
            if b'\r' in currline:
                val = currline.split(b'%')[0].strip()
                if val:
                    self.percent = float(val)
                currline = b''
        a = wkr.stdout.read(1)
        while a:
            currline += a
            if b'\r' in currline:
                val = currline.split(b'%')[0].strip()
                if val:
                    self.percent = float(val)
            currline = b''
            a = wkr.stdout.read(1)
        if self.oscategory:
            defprofile = '/opt/confluent/lib/osdeploy/{0}'.format(
                self.oscategory)
            generate_stock_profiles(defprofile, self.distpath, self.targpath,
                                    self.osname, self.profiles)
        self.phase = 'complete'
        self.percent = 100.0


def list_importing():
    return [msg.ChildCollection(x) for x in importing]


def remove_importing(importkey):
    importing[importkey].stop()
    del importing[importkey]
    yield msg.DeletedResource('deployment/importing/{0}'.format(importkey))


def get_importing_status(importkey):
    yield msg.KeyValueData(importing[importkey].progress)


if __name__ == '__main__':
    os.umask(0o022)
    if len(sys.argv) > 2:
        mfd = os.environ.get('CONFLUENT_MEDIAFD', None)
        sys.exit(import_image(sys.argv[1], callback=printit, backend=True, mfd=mfd))
    else:
        sys.exit(import_image(sys.argv[1], callback=printit))
