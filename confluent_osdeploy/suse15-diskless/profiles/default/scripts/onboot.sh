#!/bin/sh

# This script is executed on each boot as it is
# completed. It is best to edit the middle of the file as
# noted below so custom commands are executed before
# the script notifies confluent that install is fully complete.

nodename=$(grep ^NODENAME /etc/confluent/confluent.info|awk '{print $2}')
confluent_apikey=$(cat /etc/confluent/confluent.apikey)
confluent_mgr=$(grep ^deploy_server: /etc/confluent/confluent.deploycfg|awk '{print $2}')
confluent_profile=$(grep ^profile: /etc/confluent/confluent.deploycfg|awk '{print $2}')
timedatectl set-timezone $(grep ^timezone: /etc/confluent/confluent.deploycfg|awk '{print $2}')
export nodename confluent_mgr confluent_profile
. /etc/confluent/functions
mkdir -p /var/log/confluent
chmod 700 /var/log/confluent
exec >> /var/log/confluent/confluent-onboot.log
exec 2>> /var/log/confluent/confluent-onboot.log
chmod 600 /var/log/confluent/confluent-onboot.log
tail -f /var/log/confluent/confluent-onboot.log > /dev/console &
logshowpid=$!

run_remote_python syncfileclient
run_remote_python confignet
run_remote onboot.custom
# onboot scripts may be placed into onboot.d, e.g. onboot.d/01-firstaction.sh, onboot.d/02-secondaction.sh
run_remote_parts onboot.d

# Induce execution of remote configuration, e.g. ansible plays in ansible/onboot.d/
run_remote_config onboot.d

#curl -X POST -d 'status: booted' -H "CONFLUENT_NODENAME: $nodename" -H "CONFLUENT_APIKEY: $confluent_apikey" https://$confluent_mgr/confluent-api/self/updatestatus
kill $logshowpid
