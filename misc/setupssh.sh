[ -f /lib/confluent/functions ] && . /lib/confluent/functions
[ -f /etc/confluent/functions ] && . /etc/confluent/functions
[ -f /opt/confluent/bin/apiclient ] && confapiclient=/opt/confluent/bin/apiclient
[ -f /etc/confluent/apiclient ] && confapiclient=/etc/confluent/apiclient
for pubkey in /etc/ssh/ssh_host*key.pub; do
    certfile=${pubkey/.pub/-cert.pub}
    rm $certfile
    confluentpython $confapiclient /confluent-api/self/sshcert $pubkey -o $certfile
done
TMPDIR=$(mktemp -d)
cd $TMPDIR
confluentpython $confapiclient /confluent-public/site/initramfs.tgz -o initramfs.tgz
tar xf initramfs.tgz
for ca in ssh/*.ca; do
	LINE=$(cat $ca)
	cp -af /etc/ssh/ssh_known_hosts /etc/ssh/ssh_known_hosts.new
	grep -v "$LINE" /etc/ssh/ssh_known_hosts > /etc/ssh/ssh_known_hosts.new
	echo '@cert-authority *' $LINE >> /etc/ssh/ssh_known_hosts.new
	mv /etc/ssh/ssh_known_hosts.new /etc/ssh/ssh_known_hosts
done
for pubkey in ssh/*.*pubkey; do
	LINE=$(cat $pubkey)
	cp -af /root/.ssh/authorized_keys /root/.ssh/authorized_keys.new
	grep -v "$LINE" /root/.ssh/authorized_keys > /root/.ssh/authorized_keys.new
	echo "$LINE" >> /root/.ssh/authorized_keys.new
	mv /root/.ssh/authorized_keys.new /root/.ssh/authorized_keys
done
confluentpython $confapiclient /confluent-api/self/nodelist | sed -e 's/^- //' > /etc/ssh/shosts.equiv
cat /etc/ssh/shosts.equiv > /root/.shosts
cd -
rm -rf $TMPDIR
