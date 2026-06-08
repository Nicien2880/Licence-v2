3PAR ARTICLE-LIKE SSH COLLECTOR + ZABBIX 7.4 TEMPLATE

Files:
- 3par_article_like_ssh.py
- template_hpe_3par_article_like_ssh_zabbix74.yaml

Purpose:
This is a Python SSH version of the common 3PAR monitoring approach described in articles with PowerShell/HPE toolkit scripts.
It uses 3PAR CLI commands directly and returns one compact JSON for Zabbix.

No LLD. The template creates fixed aggregate items only, so it should not create thousands of items.

1. Install Python dependency

    python3 -m pip install paramiko

2. Install script

    mkdir -p /usr/lib/zabbix/externalscripts/3par
    cp 3par_article_like_ssh.py /usr/lib/zabbix/externalscripts/3par/3par_article_like.py
    chmod +x /usr/lib/zabbix/externalscripts/3par/3par_article_like.py
    chown zabbix:zabbix /usr/lib/zabbix/externalscripts/3par/3par_article_like.py

3. Create config

    mkdir -p /etc/zabbix/3par
    nano /etc/zabbix/3par/3par-article.conf

Example config:

    [3par]
    host=10.10.10.50
    port=22
    user=zbx_monitor
    password=YOUR_PASSWORD
    timeout=35

    cpu_command=statcpu -d 1 -iter 1
    vv_command=statvv -rw -d 1 -iter 1
    vlun_command=statvlun -rw -d 1 -iter 1
    port_command=statport -rw -d 1 -iter 1
    pd_command=statpd -rw -d 1 -iter 1

    enable_vlun=true
    enable_port=true
    enable_pd=true
    raw_output=false

Permissions:

    chown -R root:zabbix /etc/zabbix/3par
    chmod 750 /etc/zabbix/3par
    chmod 640 /etc/zabbix/3par/3par-article.conf

4. Test from console

    sudo -u zabbix /usr/lib/zabbix/externalscripts/3par/3par_article_like.py 3par-article --pretty

Expected:
    "status": 1

For debugging:
    sudo -u zabbix /usr/lib/zabbix/externalscripts/3par/3par_article_like.py 3par-article --pretty --raw

5. Import template

    Data collection -> Templates -> Import
    File: template_hpe_3par_article_like_ssh_zabbix74.yaml

6. Link template to 3PAR host

Host macro:
    {$3PAR.ARTICLE.PROFILE} = 3par-article

Master item key:
    3par/3par_article_like.py["{$3PAR.ARTICLE.PROFILE}"]

Main collected blocks:
- CPU by physical node/controller 0-3 and total
- VV read/write/total summary
- VLUN read/write/total summary
- Port read/write/total summary
- Physical disk read/write/total summary
- command status/elapsed
- parser rows detected/used

If some commands are too heavy:
Set in config:
    enable_vlun=false
or:
    enable_port=false
or:
    enable_pd=false

If parser uses zero rows:
Run with --raw and check real CLI output. The table headers may differ on your 3PAR OS version; then the parser field aliases need to be adjusted.
