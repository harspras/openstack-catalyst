# Copyright (c) 2012 OpenStack, LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import random

import netaddr
from sqlalchemy import orm
from sqlalchemy.orm import exc

from quantum.api.v2 import router as api_router
from quantum.common import exceptions as q_exc
from quantum.db import api as db
from quantum.db import models_v2
from quantum import quantum_plugin_base_v2


LOG = logging.getLogger(__name__)


class QuantumDbPluginV2(quantum_plugin_base_v2.QuantumPluginBaseV2):
    """ A class that implements the v2 Quantum plugin interface
        using SQLAlchemy models.  Whenever a non-read call happens
        the plugin will call an event handler class method (e.g.,
        network_created()).  The result is that this class can be
        sub-classed by other classes that add custom behaviors on
        certain events.
    """

    def __init__(self):
        # NOTE(jkoelker) This is an incomlete implementation. Subclasses
        #                must override __init__ and setup the database
        #                and not call into this class's __init__.
        #                This connection is setup as memory for the tests.
        sql_connection = 'sqlite:///:memory:'
        db.configure_db({'sql_connection': sql_connection,
                         'base': models_v2.model_base.BASEV2})

    def _get_tenant_id_for_create(self, context, resource):
        if context.is_admin and 'tenant_id' in resource:
            tenant_id = resource['tenant_id']
        elif ('tenant_id' in resource and
              resource['tenant_id'] != context.tenant_id):
            reason = _('Cannot create resource for another tenant')
            raise q_exc.AdminRequired(reason=reason)
        else:
            tenant_id = context.tenant_id
        return tenant_id

    def _model_query(self, context, model):
        query = context.session.query(model)

        # NOTE(jkoelker) non-admin queries are scoped to their tenant_id
        if not context.is_admin and hasattr(model.tenant_id):
            query = query.filter(tenant_id=context.tenant_id)

        return query

    def _get_by_id(self, context, model, id, joins=(), verbose=None):
        query = self._model_query(context, model)
        if verbose:
            if verbose and isinstance(verbose, list):
                options = [orm.joinedload(join) for join in joins
                           if join in verbose]
            else:
                options = [orm.joinedload(join) for join in joins]
            query = query.options(*options)
        return query.filter_by(id=id).one()

    def _get_network(self, context, id, verbose=None):
        try:
            network = self._get_by_id(context, models_v2.Network, id,
                                      joins=('subnets',), verbose=verbose)
        except exc.NoResultFound:
            raise q_exc.NetworkNotFound(net_id=id)
        except exc.MultipleResultsFound:
            LOG.error('Multiple networks match for %s' % id)
            raise q_exc.NetworkNotFound(net_id=id)
        return network

    def _get_subnet(self, context, id, verbose=None):
        try:
            subnet = self._get_by_id(context, models_v2.Subnet, id,
                                     verbose=verbose)
        except exc.NoResultFound:
            raise q_exc.SubnetNotFound(subnet_id=id)
        except exc.MultipleResultsFound:
            LOG.error('Multiple subnets match for %s' % id)
            raise q_exc.SubnetNotFound(subnet_id=id)
        return subnet

    def _get_port(self, context, id, verbose=None):
        try:
            port = self._get_by_id(context, models_v2.Port, id,
                                   verbose=verbose)
        except exc.NoResultFound:
            # NOTE(jkoelker) The PortNotFound exceptions requires net_id
            #                kwarg in order to set the message correctly
            raise q_exc.PortNotFound(port_id=id, net_id=None)
        except exc.MultipleResultsFound:
            LOG.error('Multiple ports match for %s' % id)
            raise q_exc.PortNotFound(port_id=id)
        return port

    def _fields(self, resource, fields):
        if fields:
            return dict(((key, item) for key, item in resource.iteritems()
                         if key in fields))
        return resource

    def _get_collection(self, context, model, dict_func, filters=None,
                        fields=None, verbose=None):
        collection = self._model_query(context, model)
        if filters:
            for key, value in filters.iteritems():
                column = getattr(model, key, None)
                if column:
                    collection = collection.filter(column.in_(value))
        return [dict_func(c, fields) for c in collection.all()]

    @staticmethod
    def _generate_mac(context, network_id):
        # TODO(garyk) read from configuration file (CONF)
        max_retries = 16
        for i in range(max_retries):
            # TODO(garyk) read base mac from configuration file (CONF)
            mac = [0xfa, 0x16, 0x3e, random.randint(0x00, 0x7f),
                   random.randint(0x00, 0xff), random.randint(0x00, 0xff)]
            mac_address = ':'.join(map(lambda x: "%02x" % x, mac))
            if QuantumDbPluginV2._check_unique_mac(context, network_id,
                                                   mac_address):
                LOG.debug("Generated mac for network %s is %s",
                          network_id, mac_address)
                return mac_address
            else:
                LOG.debug("Generated mac %s exists. Remaining attempts %s.",
                          mac_address, max_retries - (i + 1))
        LOG.error("Unable to generate mac address after %s attempts",
                  max_retries)
        raise q_exc.MacAddressGenerationFailure(net_id=network_id)

    @staticmethod
    def _check_unique_mac(context, network_id, mac_address):
        mac_qry = context.session.query(models_v2.Port)
        try:
            mac_qry.filter_by(network_id=network_id,
                              mac_address=mac_address).one()
        except exc.NoResultFound:
            return True
        return False

    def _make_network_dict(self, network, fields=None):
        res = {'id': network['id'],
               'name': network['name'],
               'tenant_id': network['tenant_id'],
               'admin_state_up': network['admin_state_up'],
               'status': network['status'],
               'subnets': [subnet['id']
                           for subnet in network['subnets']]}

        return self._fields(res, fields)

    def _make_subnet_dict(self, subnet, fields=None):
        res = {'id': subnet['id'],
               'network_id': subnet['network_id'],
               'ip_version': subnet['ip_version'],
               'cidr': subnet['cidr'],
               'gateway_ip': subnet['gateway_ip']}
        return self._fields(res, fields)

    def _make_port_dict(self, port, fields=None):
        res = {"id": port["id"],
               "network_id": port["network_id"],
               'tenant_id': port['tenant_id'],
               "mac_address": port["mac_address"],
               "admin_state_up": port["admin_state_up"],
               "status": port["status"],
               "fixed_ips": [ip["address"] for ip in port["fixed_ips"]],
               "device_id": port["device_id"]}
        return self._fields(res, fields)

    def create_network(self, context, network):
        n = network['network']

        # NOTE(jkoelker) Get the tenant_id outside of the session to avoid
        #                unneeded db action if the operation raises
        tenant_id = self._get_tenant_id_for_create(context, n)
        with context.session.begin():
            network = models_v2.Network(tenant_id=tenant_id,
                                        name=n['name'],
                                        admin_state_up=n['admin_state_up'],
                                        status="ACTIVE")
            context.session.add(network)
        return self._make_network_dict(network)

    def update_network(self, context, id, network):
        n = network['network']
        with context.session.begin():
            network = self._get_network(context, id)
            network.update(n)
        return self._make_network_dict(network)

    def delete_network(self, context, id):
        with context.session.begin():
            network = self._get_network(context, id)

            filter = {'network_id': [id]}
            ports = self.get_ports(context, filters=filter)
            if ports:
                raise q_exc.NetworkInUse(net_id=id)

            subnets_qry = context.session.query(models_v2.Subnet)
            subnets_qry.filter_by(network_id=id).delete()
            context.session.delete(network)

    def get_network(self, context, id, fields=None, verbose=None):
        network = self._get_network(context, id, verbose=verbose)
        return self._make_network_dict(network, fields)

    def get_networks(self, context, filters=None, fields=None, verbose=None):
        return self._get_collection(context, models_v2.Network,
                                    self._make_network_dict,
                                    filters=filters, fields=fields,
                                    verbose=verbose)

    def create_subnet(self, context, subnet):
        s = subnet['subnet']

        if s['gateway_ip'] == api_router.ATTR_NOT_SPECIFIED:
            net = netaddr.IPNetwork(s['cidr'])
            s['gateway_ip'] = str(netaddr.IPAddress(net.first + 1))

        with context.session.begin():
            network = self._get_network(context, s["network_id"])
            subnet = models_v2.Subnet(network_id=s['network_id'],
                                      ip_version=s['ip_version'],
                                      cidr=s['cidr'],
                                      gateway_ip=s['gateway_ip'])

            context.session.add(subnet)
        return self._make_subnet_dict(subnet)

    def update_subnet(self, context, id, subnet):
        s = subnet['subnet']
        with context.session.begin():
            subnet = self._get_subnet(context, id)
            subnet.update(s)
        return self._make_subnet_dict(subnet)

    def delete_subnet(self, context, id):
        with context.session.begin():
            subnet = self._get_subnet(context, id)

            allocations_qry = context.session.query(models_v2.IPAllocation)
            allocations_qry.filter_by(subnet_id=id).delete()

            context.session.delete(subnet)

    def get_subnet(self, context, id, fields=None, verbose=None):
        subnet = self._get_subnet(context, id, verbose=verbose)
        return self._make_subnet_dict(subnet, fields)

    def get_subnets(self, context, filters=None, fields=None, verbose=None):
        return self._get_collection(context, models_v2.Subnet,
                                    self._make_subnet_dict,
                                    filters=filters, fields=fields,
                                    verbose=verbose)

    def create_port(self, context, port):
        p = port['port']
        # NOTE(jkoelker) Get the tenant_id outside of the session to avoid
        #                unneeded db action if the operation raises
        tenant_id = self._get_tenant_id_for_create(context, p)

        with context.session.begin():
            network = self._get_network(context, p["network_id"])

            # Ensure that a MAC address is defined and it is unique on the
            # network
            if p['mac_address'] == api_router.ATTR_NOT_SPECIFIED:
                p['mac_address'] = QuantumDbPluginV2._generate_mac(
                    context, p["network_id"])
            else:
                # Ensure that the mac on the network is unique
                if not QuantumDbPluginV2._check_unique_mac(context,
                                                           p["network_id"],
                                                           p['mac_address']):
                    raise q_exc.MacAddressInUse(net_id=p["network_id"],
                                                mac=p['mac_address'])

            port = models_v2.Port(tenant_id=tenant_id,
                                  network_id=p['network_id'],
                                  mac_address=p['mac_address'],
                                  admin_state_up=p['admin_state_up'],
                                  status="ACTIVE",
                                  device_id=p['device_id'])
            context.session.add(port)

            # TODO(anyone) ip allocation
            #for subnet in network["subnets"]:
            #    pass

        return self._make_port_dict(port)

    def update_port(self, context, id, port):
        p = port['port']
        with context.session.begin():
            port = self._get_port(context, id)
            port.update(p)
        return self._make_port_dict(port)

    def delete_port(self, context, id):
        with context.session.begin():
            port = self._get_port(context, id)

            allocations_qry = context.session.query(models_v2.IPAllocation)
            allocations_qry.filter_by(port_id=id).delete()

            context.session.delete(port)

    def get_port(self, context, id, fields=None, verbose=None):
        port = self._get_port(context, id, verbose=verbose)
        return self._make_port_dict(port, fields)

    def get_ports(self, context, filters=None, fields=None, verbose=None):
        return self._get_collection(context, models_v2.Port,
                                    self._make_port_dict,
                                    filters=filters, fields=fields,
                                    verbose=verbose)
