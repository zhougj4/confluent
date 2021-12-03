#!/bin/sh

nodename=$(grep ^NODENAME: /etc/confluent/confluent.info | awk '{print $2}')
confluent_mgr=$(grep ^MANAGER: /etc/confluent/confluent.info| head -n 1| awk '{print $2}' | sed -e s/%/%25/)
if [[ $confluent_mgr = *:* ]]; then
    confluent_mgr=[$confluent_mgr]
fi

mkdir -p /sysroot/var/home/core/.ssh/
chmod 700 /sysroot/var/home/core/.ssh
cat /ssh/*.rootpubkey >> /sysroot/var/home/core/.ssh/authorized_keys
chmod 600 /sysroot/var/home/core/.ssh/authorized_keys
cp -a /etc/confluent /sysroot/etc/confluent
mkdir -p /sysroot/opt/confluent/bin/
cat > /sysroot/opt/confluent/bin/setssh.sh << 'EOF'
#!/bin/sh
nodename=$(grep ^NODENAME: /etc/confluent/confluent.info | awk '{print $2}')
confluent_mgr=$(grep ^MANAGER: /etc/confluent/confluent.info| head -n 1| awk '{print $2}' | sed -e s/%/%25/)
if [[ $confluent_mgr = *:* ]]; then
    confluent_mgr=[$confluent_mgr]
fi

for i in  /etc/ssh/ssh_host*key.pub; do
    certname=${i/.pub/-cert.pub}
    curl --cacert /etc/confluent/ca.pem -sf -X POST -H "CONFLUENT_NODENAME: $nodename" -H "CONFLUENT_APIKEY: $(cat /etc/confluent/confluent.apikey)" -d @$i https://$confluent_mgr/confluent-api/self/sshcert > $certname
    if ! grep HostKey ${i%.pub} /etc/ssh/sshd_config > /dev/null; then
        echo HostKey ${i%.pub} >> /etc/ssh/sshd_config
    fi
    if ! grep HostCertificate $certname /etc/ssh/sshd_config > /dev/null; then
        echo HostCertificate $certname >> /etc/ssh/sshd_config
    fi
done
EOF

chmod 755 /sysroot/opt/confluent/bin/setssh.sh
chcon system_u:object_r:bin_t:s0 /sysroot/opt/confluent/bin/setssh.sh


cat > /sysroot/etc/systemd/system/confluent-ssh.service << EOF
[Service]
Type=oneshot
ExecStart=/opt/confluent/bin/setssh.sh

[Unit]
PartOf=sshd.service
Before=sshd.service
After=sshd-keygen.target
Wants=sshd-keygen.target
Requires=network-online.target
After=network-online.target


[Install]
WantedBy=sshd.service
EOF

mkdir -p /sysroot/etc/systemd/system/sshd.service.wants
ln -s /etc/systemd/system/confluent-ssh.service /sysroot/etc/systemd/system/sshd.service.wants/
chcon -h system_u:object_r:systemd_unit_file_t:s0 /sysroot/etc/systemd/system/confluent-ssh.service /sysroot/etc/systemd/system/sshd.service.wants/confluent-ssh.service
