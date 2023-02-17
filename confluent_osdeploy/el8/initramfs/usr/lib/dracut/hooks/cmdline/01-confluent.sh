#!/bin/bash
echo -n "" >> /tmp/net.ifaces
echo -n "" > /tmp/01-autocons.devnode
cat /tls/*.0 >> /etc/pki/tls/certs/ca-bundle.crt
if ! grep console= /proc/cmdline >& /dev/null; then
    autocons=$(/opt/confluent/bin/autocons)
    if [ -n "$autocons" ]; then
        echo console=$autocons |sed -e 's!/dev/!!' >> /tmp/01-autocons.conf
        autocons=${autocons%,*}
	echo $autocons > /tmp/01-autocons.devnode
	echo "Detected firmware specified console at $(cat /tmp/01-autocons.conf)" > $autocons
        echo "Initializing auto detected console when installer starts" > $autocons
    fi
fi
if grep console=ttyS /proc/cmdline >& /dev/null; then
    echo "Serial console has been requested in the kernel arguments, the local video may not show progress" > /dev/tty1
fi
. /lib/anaconda-lib.sh
wait_for_kickstart
