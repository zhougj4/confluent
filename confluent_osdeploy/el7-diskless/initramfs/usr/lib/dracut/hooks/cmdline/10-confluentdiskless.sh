get_remote_apikey() {
    while [ -z "$confluent_apikey" ]; do
        /opt/confluent/bin/clortho $nodename $confluent_mgr > /etc/confluent/confluent.apikey
        if grep ^SEALED: /etc/confluent/confluent.apikey > /dev/null; then
            # we don't support remote sealed api keys anymore
            echo > /etc/confluent/confluent.apikey
        fi
        confluent_apikey=$(cat /etc/confluent/confluent.apikey)
        if [ -z "$confluent_apikey" ]; then
            echo "Unable to acquire node api key, set deployment.apiarmed=once on node '$nodename', retrying..."
            sleep 10
        elif [ -c /dev/tpmrm0 -a -x /usr/bin/tpm2_startauthsession ]; then
            tmpdir=$(mktemp -d)
            cd $tmpdir
            tpm2_startauthsession --session=session.ctx
            tpm2_policypcr -Q --session=session.ctx --pcr-list="sha256:15" --policy=pcr15.sha256.policy
            tpm2_createprimary -G ecc -Q --key-context=prim.ctx
            (echo -n "CONFLUENT_APIKEY:";cat /etc/confluent/confluent.apikey) | tpm2_create -Q --policy=pcr15.sha256.policy --public=data.pub --private=data.priv -i - -C prim.ctx
            tpm2_load -Q --parent-context=prim.ctx --public=data.pub --private=data.priv --name=confluent.apikey --key-context=data.ctx
            tpm2_evictcontrol -Q -c data.ctx
            tpm2_flushcontext session.ctx
            cd - > /dev/null
            rm -rf $tmpdir
        fi
    done
}
root=1
rootok=1
netroot=confluent
clear
mkdir -p /etc/ssh
mkdir -p /var/tmp/
mkdir -p /var/empty/sshd
mkdir -p /etc/confluent
sed -i '/^root:x/d' /etc/passwd
echo root:x:0:0::/:/bin/bash >> /etc/passwd
echo sshd:x:30:30:SSH User:/var/empty/sshd:/sbin/nologin >> /etc/passwd

if ! grep console= /proc/cmdline >& /dev/null; then
    autocons=$(/opt/confluent/bin/autocons)
    autocons=${autocons##*/}
    echo "Automatic console configured for $autocons"
fi
echo "Initializing confluent diskless environment"
echo -n "udevd: "
/usr/lib/systemd/systemd-udevd --daemon
echo -n "Loading drivers..."
udevadm trigger
udevadm trigger --type=devices --action=add
udevadm settle
modprobe ib_ipoib
modprobe ib_umad
modprobe hfi1
modprobe mlx5_ib
echo "done"
cat > /etc/ssh/sshd_config << EOF
Port 2222
Subsystem       sftp    /usr/libexec/openssh/sftp-server
PermitRootLogin yes
AuthorizedKeysFile      .ssh/authorized_keys
EOF
mkdir /root/.ssh
mkdir /.ssh
cat /ssh/*pubkey > /root/.ssh/authorized_keys 2>/dev/null
cp /root/.ssh/authorized_keys /.ssh/
cat /tls/*.pem > /etc/confluent/ca.pem
mkdir -p /etc/pki/tls/certs
cat /tls/*.pem > /etc/pki/tls/certs/ca-bundle.crt
TRIES=0
oldumask=$(umask)
umask 0077
tpmdir=$(mktemp -d)
cd $tpmdir
lasthdl=""
if [ -c /dev/tpmrm0 -a -x /usr/bin/tpm2_getcap ]; then
    for hdl in $(tpm2_getcap handles-persistent|awk '{print $2}'); do
        tpm2_startauthsession --policy-session --session=session.ctx
        tpm2_policypcr -Q --session=session.ctx --pcr-list="sha256:15" --policy=pcr15.sha256.policy
        unsealeddata=$(tpm2_unseal --auth=session:session.ctx -Q -c $hdl 2>/dev/null)
        tpm2_flushcontext session.ctx
        if [[ $unsealeddata == "CONFLUENT_APIKEY:"* ]]; then
            confluent_apikey=${unsealeddata#CONFLUENT_APIKEY:}
            echo $confluent_apikey > /etc/confluent/confluent.apikey
            if [ -n "$lasthdl" ]; then
                tpm2_evictcontrol -c $lasthdl
            fi
            lasthdl=$hdl
        fi
    done
fi
cd - > /dev/null
rm -rf $tpmdir
touch /etc/confluent/confluent.info
cd /sys/class/net
echo -n "Scanning for network configuration..."
while ! grep ^EXTMGRINFO: /etc/confluent/confluent.info | awk -F'|' '{print $3}' | grep 1 >& /dev/null && [ "$TRIES" -lt 30 ]; do
    TRIES=$((TRIES + 1))
    for i in *; do
        ip link set $i up
    done
    /opt/confluent/bin/copernicus -t > /etc/confluent/confluent.info
done
cd /
nodename=$(grep ^NODENAME /etc/confluent/confluent.info|awk '{print $2}')
hostname $nodename
confluent_mgr=$(grep '^EXTMGRINFO:.*1$' /etc/confluent/confluent.info | head -n 1 | awk -F': ' '{print $2}' | awk -F'|' '{print $1}')
if [ -z "$confluent_mgr" ]; then
    confluent_mgr=$(grep ^MANAGER: /etc/confluent/confluent.info|head -n 1 | awk '{print $2}')
fi
if [[ $confluent_mgr == *%* ]]; then
    echo $confluent_mgr | awk -F% '{print $2}' > /tmp/confluent.ifidx
    ifidx=$(cat /tmp/confluent.ifidx)
    ifname=$(ip link |grep ^$ifidx:|awk '{print $2}')
    ifname=${ifname%:}
fi

ready=0
while [ $ready = "0" ]; do
    get_remote_apikey
    if [[ $confluent_mgr == *:* ]]; then
        confluent_mgr="[${confluent_mgr/\%/%25}]"
    fi
    tmperr=$(mktemp)
    curl -gsSf -H "CONFLUENT_NODENAME: $nodename" -H "CONFLUENT_APIKEY: $confluent_apikey" https://$confluent_mgr/confluent-api/self/deploycfg > /etc/confluent/confluent.deploycfg 2> $tmperr
    if grep 401 $tmperr > /dev/null; then
        confluent_apikey=""
        if [ -n "$lasthdl" ]; then
            tpm2_evictcontrol -c $lasthdl
        fi
        confluent_mgr=${confluent_mgr#[}
        confluent_mgr=${confluent_mgr%]}
    else
        ready=1
    fi
    rm $tmperr
done
if [ ! -z "$autocons" ] && grep textconsole: true /etc/confluent/confluent.deploycfg > /dev/null; then /opt/confluent/bin/autocons -c > /dev/null; fi
if [ -c /dev/tpmrm0 -a -x /usr/bin/tpm2_pcrextend ]; then
    tpm2_pcrextend 15:sha256=2fbe96c50dde38ce9cd2764ddb79c216cfbcd3499568b1125450e60c45dd19f2
fi
umask $oldumask
autoconfigmethod=$(grep ^ipv4_method: /etc/confluent/confluent.deploycfg |awk '{print $2}')
if [ "$autoconfigmethod" = "dhcp" ]; then
    echo -n "Attempting to use dhcp to bring up $ifname..."
    dhclient $ifname
    echo "Complete:"
    ip addr show dev $ifname
else
    v4addr=$(grep ^ipv4_address: /etc/confluent/confluent.deploycfg)
    v4addr=${v4addr#ipv4_address: }
    v4gw=$(grep ^ipv4_gateway: /etc/confluent/confluent.deploycfg)
    v4gw=${v4gw#ipv4_gateway: }
    if [ "$v4gw" = "null" ]; then
        v4gw=""
    fi
    v4nm=$(grep ^prefix: /etc/confluent/confluent.deploycfg)
    v4nm=${v4nm#prefix: }
    echo "Setting up $ifname as static at $v4addr/$v4nm"
    ip addr add dev $ifname $v4addr/$v4nm
    if [ ! -z "$v4gw" ]; then
        ip route add default via $v4gw
    fi
    mkdir -p /run/NetworkManager/system-connections
    cat > /run/NetworkManager/system-connections/$ifname.nmconnection << EOC
[connection]
EOC
    echo id=${ifname} >> /run/NetworkManager/system-connections/$ifname.nmconnection
    echo uuid=$(uuidgen) >> /run/NetworkManager/system-connections/$ifname.nmconnection
    cat >> /run/NetworkManager/system-connections/$ifname.nmconnection << EOC
type=ethernet
autoconnect-retries=1
EOC
    echo interface-name=$ifname >> /run/NetworkManager/system-connections/$ifname.nmconnection
    cat >> /run/NetworkManager/system-connections/$ifname.nmconnection << EOC
multi-connect=1
permissions=
wait-device-timeout=60000

[ethernet]
mac-address-blacklist=

[ipv4]
EOC
    echo address1=$v4addr/$v4nm >> /run/NetworkManager/system-connections/$ifname.nmconnection
    if [ ! -z "$v4gw" ]; then
        echo gateway=$v4gw >> /run/NetworkManager/system-connections/$ifname.nmconnection
    fi
    nameserversec=0
    nameservers=""
    while read -r entry; do
        if [ $nameserversec = 1 ]; then
            if [[ $entry == "-"* ]]; then
                nameservers="$nameservers"${entry#- }";"
                continue
            fi
        fi
        nameserversec=0
        if [ "${entry%:*}" = "nameservers" ]; then
            nameserversec=1
            continue
        fi
    done < /etc/confluent/confluent.deploycfg
    echo dns=$nameservers >> /run/NetworkManager/system-connections/$ifname.nmconnection
    dnsdomain=$(grep ^dnsdomain: /etc/confluent/confluent.deploycfg)
    dnsdomain=${dnsdomain#dnsdomain: }
    echo dns-search=$dnsdomain >> /run/NetworkManager/system-connections/$ifname.nmconnection
    cat >> /run/NetworkManager/system-connections/$ifname.nmconnection << EOC
may-fail=false
method=manual

[ipv6]
addr-gen-mode=eui64
method=auto

[proxy]
EOC
fi
chmod 600 /run/NetworkManager/system-connections/*.nmconnection
echo -n "Initializing ssh..."
ssh-keygen -A
for pubkey in /etc/ssh/ssh_host*key.pub; do
    certfile=${pubkey/.pub/-cert.pub}
    privfile=${pubkey%.pub}
    curl -gsf -X POST -H "CONFLUENT_NODENAME: $nodename" -H "CONFLUENT_APIKEY: $confluent_apikey" -d @$pubkey  https://$confluent_mgr/confluent-api/self/sshcert > $certfile
    if [ -s $certfile ]; then
        echo HostCertificate $certfile >> /etc/ssh/sshd_config
    fi
    echo HostKey $privfile >> /etc/ssh/sshd_config
done
/usr/sbin/sshd
confluent_profile=$(grep ^profile: /etc/confluent/confluent.deploycfg| awk '{print $2}')
confluent_proto=$(grep ^protocol: /etc/confluent/confluent.deploycfg| awk '{print $2}')
confluent_urls=""
for addr in $(grep ^MANAGER: /etc/confluent/confluent.info|awk '{print $2}'|sed -e s/%/%25/); do
    if [[ $addr == *:* ]]; then
        confluent_urls="$confluent_urls $confluent_proto://[$addr]/confluent-public/os/$confluent_profile/rootimg.sfs"
    else
        confluent_urls="$confluent_urls $confluent_proto://$addr/confluent-public/os/$confluent_profile/rootimg.sfs"
    fi
done
confluent_mgr=$(grep ^deploy_server: /etc/confluent/confluent.deploycfg| awk '{print $2}')
mkdir -p /etc/confluent
curl -gsf https://$confluent_mgr/confluent-public/os/$confluent_profile/scripts/functions > /etc/confluent/functions
. /etc/confluent/functions
source_remote imageboot.sh
