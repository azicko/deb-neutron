# Copyright (c) 2014 OpenStack Foundation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os

import eventlet
from oslo_config import cfg
from oslo_log import log as logging
import webob

from neutron.agent.linux import keepalived
from neutron.agent.linux import utils as agent_utils
from neutron.common import constants as l3_constants
from neutron.i18n import _LE, _LI

LOG = logging.getLogger(__name__)

HA_DEV_PREFIX = 'ha-'
KEEPALIVED_STATE_CHANGE_SERVER_BACKLOG = 4096

OPTS = [
    cfg.StrOpt('ha_confs_path',
               default='$state_path/ha_confs',
               help=_('Location to store keepalived/conntrackd '
                      'config files')),
    cfg.StrOpt('ha_vrrp_auth_type',
               default='PASS',
               choices=keepalived.VALID_AUTH_TYPES,
               help=_('VRRP authentication type')),
    cfg.StrOpt('ha_vrrp_auth_password',
               help=_('VRRP authentication password'),
               secret=True),
    cfg.IntOpt('ha_vrrp_advert_int',
               default=2,
               help=_('The advertisement interval in seconds')),
]


class KeepalivedStateChangeHandler(object):
    def __init__(self, agent):
        self.agent = agent

    @webob.dec.wsgify(RequestClass=webob.Request)
    def __call__(self, req):
        router_id = req.headers['X-Neutron-Router-Id']
        state = req.headers['X-Neutron-State']
        self.enqueue(router_id, state)

    def enqueue(self, router_id, state):
        LOG.debug('Handling notification for router '
                  '%(router_id)s, state %(state)s', {'router_id': router_id,
                                                     'state': state})
        self.agent.enqueue_state_change(router_id, state)


class L3AgentKeepalivedStateChangeServer(object):
    def __init__(self, agent, conf):
        self.agent = agent
        self.conf = conf

        agent_utils.ensure_directory_exists_without_file(
            self.get_keepalived_state_change_socket_path(self.conf))

    @classmethod
    def get_keepalived_state_change_socket_path(cls, conf):
        return os.path.join(conf.state_path, 'keepalived-state-change')

    def run(self):
        server = agent_utils.UnixDomainWSGIServer(
            'neutron-keepalived-state-change')
        server.start(KeepalivedStateChangeHandler(self.agent),
                     self.get_keepalived_state_change_socket_path(self.conf),
                     workers=0,
                     backlog=KEEPALIVED_STATE_CHANGE_SERVER_BACKLOG)
        server.wait()


class AgentMixin(object):
    def __init__(self, host):
        self._init_ha_conf_path()
        super(AgentMixin, self).__init__(host)
        eventlet.spawn(self._start_keepalived_notifications_server)

    def _start_keepalived_notifications_server(self):
        state_change_server = (
            L3AgentKeepalivedStateChangeServer(self, self.conf))
        state_change_server.run()

    def enqueue_state_change(self, router_id, state):
        LOG.info(_LI('Router %(router_id)s transitioned to %(state)s'),
                 {'router_id': router_id,
                  'state': state})
        self._update_metadata_proxy(router_id, state)

    def _update_metadata_proxy(self, router_id, state):
        try:
            ri = self.router_info[router_id]
        except AttributeError:
            LOG.info(_LI('Router %s is not managed by this agent. It was '
                         'possibly deleted concurrently.'), router_id)
            return

        if state == 'master':
            LOG.debug('Spawning metadata proxy for router %s', router_id)
            self.metadata_driver.spawn_monitored_metadata_proxy(
                self.process_monitor, ri.ns_name, self.conf.metadata_port,
                self.conf, router_id=ri.router_id)
        else:
            LOG.debug('Closing metadata proxy for router %s', router_id)
            self.metadata_driver.destroy_monitored_metadata_proxy(
                self.process_monitor, ri.router_id, ri.ns_name, self.conf)

    def _init_ha_conf_path(self):
        ha_full_path = os.path.dirname("/%s/" % self.conf.ha_confs_path)
        agent_utils.ensure_dir(ha_full_path)

    def process_ha_router_added(self, ri):
        ha_port = ri.router.get(l3_constants.HA_INTERFACE_KEY)
        if not ha_port:
            LOG.error(_LE('Unable to process HA router %s without ha port'),
                      ri.router_id)
            return

        ri._set_subnet_info(ha_port)
        ri.ha_port = ha_port
        ri._init_keepalived_manager(self.process_monitor)
        ri.ha_network_added(ha_port['network_id'],
                            ha_port['id'],
                            ha_port['ip_cidr'],
                            ha_port['mac_address'])

        ri.update_initial_state(self.enqueue_state_change)
        ri.spawn_state_change_monitor(self.process_monitor)

    def process_ha_router_removed(self, ri):
        ri.destroy_state_change_monitor(self.process_monitor)
        ri.ha_network_removed()
