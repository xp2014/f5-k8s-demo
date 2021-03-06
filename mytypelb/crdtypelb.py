#!/usr/bin/env python
#
# MyTypeLB
##########
#
# The following is a simple PoC of using type:LoadBalancer with
# Container Ingress Services 2.2.0 using CRD
#
# The process is to assign a service that includes can optionally
# include "loadBalancerIP"
#
# This script will identify these resources and create a BIG-IP
# VirtuallServer with the IP address specified or one from a pre-allocated
# range
#
# This is done by creating "TransportServer" entries to match each
# service/port.
#
# Running the Script
####################
#
# This script assumes that you have a kubeconfig file with
# appropriate privileges.
#
# Known issues
###############
#
# -hardcodes "default" namespace and name of configmap
#
#
from kubernetes import client, config, watch
import json
import sys
import logging
import time
from ipaddress import ip_network
# Configs can be set in Configuration class directly or using helper utility

logger = logging.getLogger('chen_pam')

class ChenPam(object):

    def __init__(self,config_name='chenpam',config_namespace='default',poll_interval=60,incluster=False):
        if incluster:
            config.load_incluster_config()
        else:
            config.load_kube_config()
        self.base_configmap_name = config_name
        self.base_configmap_namespace = config_namespace
        self.v1 = client.CoreV1Api()
        self.api = client.CustomObjectsApi()

        self.crdmap = {}
        self.poll_interval = poll_interval
        self.resource_version = ''

        self.all_ips = []
        self.found_ips = []
        self.service_to_ip = {}

        # hardcoded values
        namespace = "default"

        # base config
        #
        # This is a very simple local IPAM known as "chenpam"
        #
        # It relies on a ConfigMap that contains a list of IP ranges
        # the LoadBalancerIP will be picked either from one provided by
        # the end-user or from the list
        #
    def load_config(self):
        ret = self.v1.read_namespaced_config_map(self.base_configmap_name, self.base_configmap_namespace)

        self.base_config = json.loads(ret.data['config'])
        self.my_configmap = ret

        self.all_ips = []
        self.found_ips = []
        self.service_to_ip = {}
        for range in self.base_config['ranges']:
            logger.debug(range)
            self.all_ips.extend([str(a) for a in ip_network(range).hosts()])
            if range.endswith('/32'):
                self.all_ips.append(range[:-3])
        logger.debug("all_ips len %d" %(len(self.all_ips)))
        self.next_idx = self.all_ips.index(self.base_config['next'])
        logger.debug("next_idx: %d" %(self.next_idx))

    def get_next_ip(self):

        if set(self.all_ips).difference(set(self.base_config['allocated']).union(self.base_config['conflict'])) == set():
            return None
        if len(self.all_ips) < self.next_idx+1:
            self.next_idx = 0
            return self.get_next_ip()
        ip_addr = self.all_ips[self.next_idx]
        self.next_idx += 1
        if ip_addr not in self.base_config['allocated'] and ip_addr not in self.base_config['conflict']:
            return ip_addr
        else:
            return self.get_next_ip()
    def update(self):
        self.load_config()
        try:
            # load from configmap in case of aborted process
            ret  = self.v1.read_namespaced_config_map(self.base_configmap_name + '.crdmap', self.base_configmap_namespace)
            logger.debug('loading crdmap from configmap')
            self.crdmap = json.loads(ret.data['crdmap'])
        except client.exceptions.ApiException as err:
            logger.debug(err)
            self.list_services()
            self.v1.create_namespaced_config_map(self.base_configmap_namespace, {'metadata':{'name':self.base_configmap_name + '.crdmap'},
                                                                                             'data':{'crdmap':json.dumps(self.crdmap)}})
        self.create_ts()
        self.save_config()
        # everything complete, delete cache of service to ip mapping
        ret  = self.v1.delete_namespaced_config_map(self.base_configmap_name + '.crdmap', self.base_configmap_namespace)
        #logger.debug(ret)
    def watch_updates(self):
        w = watch.Watch()
        lastcheck = time.time()
        last_seen_version = ''
        while 1:
            needsupdate = True
            for event in w.stream(self.v1.list_service_for_all_namespaces,timeout_seconds=self.poll_interval,resource_version=self.resource_version):
                if needsupdate:
                    logger.debug('ping')
                    self.update()
                    time.sleep(self.poll_interval)
                needsupdate = False
#            logger.debug('ended loop')

    def poll_updates(self):
        while 1:
            try:
                self.update()
            except Exception as err:
                logger.error(err)
            logger.debug('sleeping for %d seconds' %(self.poll_interval))
            time.sleep(self.poll_interval)
    # grab all services
    def list_services(self):

        all_services = self.v1.list_service_for_all_namespaces(watch=False)
        logger.debug('resource_version: %s' %(all_services.metadata.resource_version))
        self.resource_version = all_services.metadata.resource_version
        self.crdmap = {}
        for i in all_services.items:
            # only look for type LoadBalancer
            if i.spec.type != "LoadBalancer":
                continue
            logger.debug("load balancer %s/%s status: %s" %(i.metadata.namespace,i.metadata.name,i.status.load_balancer.ingress))

            if i.status.load_balancer.ingress:
                ip_addr = i.status.load_balancer.ingress[0].ip
            # only inspect services that specify a load balancer IP
            elif i.spec.load_balancer_ip:
                ip_addr = i.spec.load_balancer_ip
            else:
                ip_addr = self.get_next_ip()
                if ip_addr == None:
                    logger.warning("No more IP addresses available, skipping %s/%s" %(i.metadata.namespace,i.metadata.name))
                    continue

            # add to list

            # add status
            i.status.load_balancer.ingress = [{'ip':ip_addr,'hostname':"%s.%s.dc1.example.com" %(i.metadata.name,i.metadata.namespace)}]
            # add labels that will be used by AS3

            servicePorts = [(p.port,p.target_port) for p in  i.spec.ports]
            self.crdmap["%s_%s" %(i.metadata.namespace,i.metadata.name)] = (ip_addr, servicePorts, i.metadata.name, i.metadata.namespace)
            logger.debug(self.crdmap)

    def create_ts(self):
        crd_output = {}
        for app, dest in self.crdmap.items():
            # AS3 definition of TCP service

            for port in dest[1]:
                crd = {'apiVersion': 'cis.f5.com/v1',
                'kind': 'TransportServer',
                'metadata': {'labels': {'f5cr': 'true','chenpam':'true'},
                            'name': "%s-%d" %(dest[2],port[0]),
                            'namespace': dest[3]
                },
                'spec': {'mode': 'performance',
                        'pool': {'monitor': {'interval': 10, 'timeout': 10, 'type': 'tcp'},
                                'service': dest[2],
                                'servicePort': port[1]},
                        'snat': 'auto',
                        'virtualServerAddress': dest[0],
                        'virtualServerPort': port[0]}}

                crd_output["%s_%s-%s" %(dest[3],dest[2],port[0])] = crd

        group = 'cis.f5.com'
        version = 'v1'

        all_existing_crds = self.api.list_cluster_custom_object(group, version, 'transportservers', watch=False)

        existing_crds = {}
        for crd in all_existing_crds['items']:

            crd_name = "%s_%s" %(crd['metadata']['namespace'],crd['metadata']['name'])
            existing_crds[crd_name] = crd

        for key,val in crd_output.items():
            crd_namespace = val['metadata']['namespace']
            ip_addr = val['spec']['virtualServerAddress']
            service_name = val['spec']['pool']['service']
            service_namespace = crd_namespace
            if "%s_%s" %(service_namespace, service_name) not in self.crdmap:
                logger.info('deleting %s with IP %s (service does not exist)' %(key, ip_addr))
                del existing_crds[key]
                continue
            elif key in existing_crds:
                old_crd = existing_crds[key]
                if 'labels' not in old_crd['metadata'] or 'chenpam' not in old_crd['metadata']['labels'] or old_crd['metadata']['labels']['chenpam'] != 'true':
                    continue
                # updating existing CRD
                needsupdate = False
                for i in val['spec']:
                    if old_crd['spec'][i] != val['spec'][i]:
                        needsudpate = True
                    old_crd['spec'][i] = val['spec'][i]
                logger.debug('old_crd: %s' %(old_crd))
                logger.debug('new_crd: %s' %(val))
                if needsupdate:
                    logger.info('updating %s with IP %s' %(key, ip_addr))
                    self.api.replace_namespaced_custom_object(group, version, crd_namespace, 'transportservers', val['metadata']['name'], old_crd)
                del existing_crds[key]
            else:
                logger.info('creating %s with IP %s' %(key, ip_addr))
                self.api.create_namespaced_custom_object(group, version, crd_namespace, 'transportservers', val)
            # update service with labels and status
            body = client.V1Service()

            body.metadata = {'name':val['spec']['pool']['service']}

            body.status = {'loadBalancer':{'ingress':[{'ip':val['spec']['virtualServerAddress'],'hostname':"%s.%s.dc1.example.com" %(val['spec']['pool']['service'],val['metadata']['namespace'])}]}}
            try:
                self.v1.replace_namespaced_service_status(val['spec']['pool']['service'],crd_namespace,body)
            except Exception as err:
                logger.error(err)
                continue
            if ip_addr not in self.found_ips:
                self.found_ips.append(ip_addr)

        for key in existing_crds:
            logger.info('deleting %s' %key)
            old_crd = existing_crds[key]
            logger.debug(old_crd)
            if 'labels' in old_crd['metadata'] and 'chenpam' in old_crd['metadata']['labels'] and old_crd['metadata']['labels']['chenpam'] == 'true':

                self.api.delete_namespaced_custom_object(group, version, existing_crds[key]['metadata']['namespace'], 'transportservers', existing_crds[key]['metadata']['name'])

    def save_config(self):
        # update config
        new_next = self.get_next_ip() or self.base_config['next']
        logger.debug(new_next)
        self.base_config['next'] = new_next
        self.base_config['allocated'] = self.found_ips
        self.my_configmap.data['config'] = json.dumps(self.base_config)
        self.v1.replace_namespaced_config_map(self.base_configmap_name, self.base_configmap_namespace, self.my_configmap)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Support type LoadBalancer via F5 TransportServer CRD')
    parser.add_argument('--level', dest='level', choices=['info','debug'],default='info',
                    help='specify logging level [info|debug] (default info)')
    parser.add_argument('--incluster', dest='incluster', action='store_true', default=False)
    parser.add_argument('--config-name', dest='config_name', default='chenpam')
    parser.add_argument('--config-namespace', dest='config_namespace', default='default')
    parser.add_argument('--poll-interval', dest='poll_interval', default=60, type=int)


    args = parser.parse_args()
    logger.setLevel(logging.INFO)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)

    if args.level == 'debug':
        logger.setLevel(logging.DEBUG)
        ch.setLevel(logging.DEBUG)

    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    ch.setFormatter(formatter)
    logger.addHandler(ch)
    try:
        chen = ChenPam(config_name=args.config_name,
                       config_namespace=args.config_namespace,
                       incluster=args.incluster,
                       poll_interval=args.poll_interval)
        #chen.poll_updates()
        chen.watch_updates()
    except Exception as err:
        logger.error(err)



#    chen.update()
