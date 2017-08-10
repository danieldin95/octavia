#    Copyright 2014 Rackspace
#    Copyright 2016 Blue Box, an IBM Company
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

from oslo_config import cfg
from oslo_db import exception as odb_exceptions
from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import strutils
import pecan
from wsme import types as wtypes
from wsmeext import pecan as wsme_pecan

from octavia.api.v2.controllers import base
from octavia.api.v2.controllers import listener
from octavia.api.v2.controllers import pool
from octavia.api.v2.types import load_balancer as lb_types
from octavia.common import constants
from octavia.common import data_models
from octavia.common import exceptions
from octavia.common import stats
from octavia.common import utils
import octavia.common.validate as validate
from octavia.db import api as db_api
from octavia.db import prepare as db_prepare
from octavia.i18n import _


CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class LoadBalancersController(base.BaseController):
    RBAC_TYPE = constants.RBAC_LOADBALANCER

    def __init__(self):
        super(LoadBalancersController, self).__init__()
        self.handler = self.handler.load_balancer

    @wsme_pecan.wsexpose(lb_types.LoadBalancerRootResponse, wtypes.text)
    def get_one(self, id):
        """Gets a single load balancer's details."""
        context = pecan.request.context.get('octavia_context')
        load_balancer = self._get_db_lb(context.session, id)

        self._auth_validate_action(context, load_balancer.project_id,
                                   constants.RBAC_GET_ONE)

        result = self._convert_db_to_type(
            load_balancer, lb_types.LoadBalancerResponse)
        return lb_types.LoadBalancerRootResponse(loadbalancer=result)

    @wsme_pecan.wsexpose(lb_types.LoadBalancersRootResponse, wtypes.text,
                         [wtypes.text], ignore_extra_args=True)
    def get_all(self, project_id=None, fields=None):
        """Lists all load balancers."""
        pcontext = pecan.request.context
        context = pcontext.get('octavia_context')

        query_filter = self._auth_get_all(context, project_id)

        load_balancers, links = self.repositories.load_balancer.get_all(
            context.session, show_deleted=False,
            pagination_helper=pcontext.get(constants.PAGINATION_HELPER),
            **query_filter)
        result = self._convert_db_to_type(
            load_balancers, [lb_types.LoadBalancerResponse])
        if fields is not None:
            result = self._filter_fields(result, fields)
        return lb_types.LoadBalancersRootResponse(
            loadbalancers=result, loadbalancers_links=links)

    def _test_lb_status(self, session, id, lb_status=constants.PENDING_UPDATE):
        """Verify load balancer is in a mutable state."""
        lb_repo = self.repositories.load_balancer
        if not lb_repo.test_and_set_provisioning_status(
                session, id, lb_status):
            prov_status = lb_repo.get(session, id=id).provisioning_status
            LOG.info("Invalid state %(state)s of loadbalancer resource %(id)s",
                     {"state": prov_status, "id": id})
            raise exceptions.LBPendingStateError(
                state=prov_status, id=id)

    @staticmethod
    def _validate_network_and_fill_or_validate_subnet(load_balancer):
        network = validate.network_exists_optionally_contains_subnet(
            network_id=load_balancer.vip_network_id,
            subnet_id=load_balancer.vip_subnet_id)
        # If subnet is not provided, pick the first subnet, preferring ipv4
        if not load_balancer.vip_subnet_id:
            network_driver = utils.get_network_driver()
            for subnet_id in network.subnets:
                # Use the first subnet, in case there are no ipv4 subnets
                if not load_balancer.vip_subnet_id:
                    load_balancer.vip_subnet_id = subnet_id
                subnet = network_driver.get_subnet(subnet_id)
                if subnet.ip_version == 4:
                    load_balancer.vip_subnet_id = subnet_id
                    break
            if not load_balancer.vip_subnet_id:
                raise exceptions.ValidationException(detail=_(
                    "Supplied network does not contain a subnet."
                ))

    def _validate_vip_request_object(self, load_balancer):
        allowed_network_objects = []
        if CONF.networking.allow_vip_port_id:
            allowed_network_objects.append('vip_port_id')
        if CONF.networking.allow_vip_network_id:
            allowed_network_objects.append('vip_network_id')
        if CONF.networking.allow_vip_subnet_id:
            allowed_network_objects.append('vip_subnet_id')

        msg = _("use of %(object)s is disallowed by this deployment's "
                "configuration.")
        if (load_balancer.vip_port_id and
                not CONF.networking.allow_vip_port_id):
            raise exceptions.ValidationException(
                detail=msg % {'object': 'vip_port_id'})
        if (load_balancer.vip_network_id and
                not CONF.networking.allow_vip_network_id):
            raise exceptions.ValidationException(
                detail=msg % {'object': 'vip_network_id'})
        if (load_balancer.vip_subnet_id and
                not CONF.networking.allow_vip_subnet_id):
            raise exceptions.ValidationException(
                detail=msg % {'object': 'vip_subnet_id'})

        if not (load_balancer.vip_port_id or
                load_balancer.vip_network_id or
                load_balancer.vip_subnet_id):
            raise exceptions.VIPValidationException(
                objects=', '.join(allowed_network_objects))

        # Validate the port id
        if load_balancer.vip_port_id:
            port = validate.port_exists(port_id=load_balancer.vip_port_id)
            load_balancer.vip_network_id = port.network_id
        # If no port id, validate the network id (and subnet if provided)
        elif load_balancer.vip_network_id:
            self._validate_network_and_fill_or_validate_subnet(load_balancer)
        # Validate just the subnet id
        elif load_balancer.vip_subnet_id:
            subnet = validate.subnet_exists(
                subnet_id=load_balancer.vip_subnet_id)
            load_balancer.vip_network_id = subnet.network_id

        validate.network_allowed_by_config(load_balancer.vip_network_id)

    def _create_vip_port_if_not_exist(self, load_balancer_db):
        """Create vip port."""
        network_driver = utils.get_network_driver()
        vip = network_driver.allocate_vip(load_balancer_db)
        return vip

    @wsme_pecan.wsexpose(lb_types.LoadBalancerFullRootResponse,
                         body=lb_types.LoadBalancerRootPOST, status_code=201)
    def post(self, load_balancer):
        """Creates a load balancer."""
        load_balancer = load_balancer.loadbalancer
        context = pecan.request.context.get('octavia_context')

        if not load_balancer.project_id and context.project_id:
            load_balancer.project_id = context.project_id

        if not load_balancer.project_id:
            raise exceptions.ValidationException(detail=_(
                "Missing project ID in request where one is required."))

        self._auth_validate_action(context, load_balancer.project_id,
                                   constants.RBAC_POST)

        self._validate_vip_request_object(load_balancer)

        lock_session = db_api.get_session(autocommit=False)
        if self.repositories.check_quota_met(
                context.session,
                lock_session,
                data_models.LoadBalancer,
                load_balancer.project_id):
            lock_session.rollback()
            raise exceptions.QuotaException

        db_lb, db_pools, db_lists = None, None, None
        try:
            lb_dict = db_prepare.create_load_balancer(load_balancer.to_dict(
                render_unsets=False
            ))
            vip_dict = lb_dict.pop('vip', {})

            # NoneType can be weird here, have to force type a second time
            listeners = lb_dict.pop('listeners', []) or []
            pools = lb_dict.pop('pools', []) or []

            # TODO(johnsom) Remove provider and flavor from the lb_dict
            # as they have not been implemented beyond the API yet.
            # Remove these lines as they are implemented.
            if 'provider' in lb_dict:
                del lb_dict['provider']
            if 'flavor' in lb_dict:
                del lb_dict['flavor']

            db_lb = self.repositories.create_load_balancer_and_vip(
                lock_session, lb_dict, vip_dict)

            # create vip port if not exist
            vip = self._create_vip_port_if_not_exist(db_lb)
            self.repositories.vip.update(
                lock_session, db_lb.id,
                ip_address=vip.ip_address,
                port_id=vip.port_id,
                network_id=vip.network_id,
                subnet_id=vip.subnet_id
            )

            if listeners or pools:
                db_pools, db_lists = self._graph_create(
                    context.session, lock_session, db_lb, listeners, pools)

            lock_session.commit()
        except odb_exceptions.DBDuplicateEntry:
            lock_session.rollback()
            raise exceptions.IDAlreadyExists()
        except Exception:
            with excutils.save_and_reraise_exception():
                lock_session.rollback()

        # Handler will be responsible for sending to controller
        try:
            LOG.info("Sending created Load Balancer %s to the handler",
                     db_lb.id)
            self.handler.create(db_lb)
        except Exception:
            with excutils.save_and_reraise_exception(reraise=False):
                self.repositories.load_balancer.update(
                    context.session, db_lb.id,
                    provisioning_status=constants.ERROR)

        db_lb = self._get_db_lb(context.session, db_lb.id)

        result = self._convert_db_to_type(
            db_lb, lb_types.LoadBalancerFullResponse)
        return lb_types.LoadBalancerFullRootResponse(loadbalancer=result)

    def _graph_create(self, session, lock_session, db_lb, listeners, pools):
        # Track which pools must have a full specification
        pools_required = set()
        # Look through listeners and find any extra pools, and move them to the
        # top level so they are created first.
        for l in listeners:
            default_pool = l.get('default_pool')
            pool_name = (
                default_pool.get('name') if default_pool else None)
            # All pools need to have a name so they can be referenced
            if default_pool and not pool_name:
                raise exceptions.ValidationException(
                    detail='Pools must be named when creating a fully '
                           'populated loadbalancer.')
            # If a pool has more than a name, assume it's a full specification
            # (but use >2 because it will also have "enabled" as default)
            if default_pool and len(default_pool) > 2:
                pools.append(default_pool)
                l['default_pool'] = {'name': pool_name}
            # Otherwise, it's a reference and we record it and move on
            elif default_pool:
                pools_required.add(pool_name)
            # We also need to check policy redirects
            for policy in l.get('l7policies'):
                redirect_pool = policy.get('redirect_pool')
                pool_name = (
                    redirect_pool.get('name') if redirect_pool else None)
                # All pools need to have a name so they can be referenced
                if default_pool and not pool_name:
                    raise exceptions.ValidationException(
                        detail='Pools must be named when creating a fully '
                               'populated loadbalancer.')
                # If a pool has more than a name, assume it's a full spec
                # (but use >2 because it will also have "enabled" as default)
                if redirect_pool and len(redirect_pool) > 2:
                    pool_name = redirect_pool['name']
                    policy['redirect_pool'] = {'name': pool_name}
                    pools.append(redirect_pool)
                # Otherwise, it's a reference and we record it and move on
                elif default_pool:
                    pools_required.add(pool_name)

        # Make sure all pool names are unique.
        pool_names = [p.get('name') for p in pools]
        if len(set(pool_names)) != len(pool_names):
            raise exceptions.ValidationException(
                detail="Pool names must be unique when creating a fully "
                       "populated loadbalancer.")
        # Make sure every reference is present in our spec list
        for pool_ref in pools_required:
            if pool_ref not in pool_names:
                raise exceptions.ValidationException(
                    detail="Pool '{name}' was referenced but no full "
                           "definition was found.".format(name=pool_ref))

        # Check quotas for pools.
        if pools and self.repositories.check_quota_met(
                session, lock_session, data_models.Pool, db_lb.project_id,
                count=len(pools)):
            raise exceptions.QuotaException

        # Now create all of the pools ahead of the listeners.
        new_pools = []
        pool_name_ids = {}
        for p in pools:
            # Check that pools have mandatory attributes, since we have to
            # bypass the normal validation layer to allow for name-only
            for attr in ('protocol', 'lb_algorithm'):
                if attr not in p:
                    raise exceptions.ValidationException(
                        detail="Pool definition for '{name}' missing required "
                               "attribute: {attr}".format(name=p['name'],
                                                          attr=attr))
            p['load_balancer_id'] = db_lb.id
            p['project_id'] = db_lb.project_id
            new_pool, new_hm, new_members = (
                pool.PoolsController()._graph_create(
                    session, lock_session, p))
            new_pools.append(new_pool)
            pool_name_ids[new_pool.name] = new_pool.id

        # Now check quotas for listeners
        if listeners and self.repositories.check_quota_met(
                session, lock_session, data_models.Listener, db_lb.project_id,
                count=len(listeners)):
            raise exceptions.QuotaException

        # Now create all of the listeners
        new_lists = []
        for l in listeners:
            default_pool = l.pop('default_pool', None)
            # If there's a default pool, replace it with the ID
            if default_pool:
                pool_name = default_pool['name']
                pool_id = pool_name_ids.get(pool_name)
                if not pool_id:
                    raise exceptions.SingleCreateDetailsMissing(
                        type='Pool', name=pool_name)
                l['default_pool_id'] = pool_id
            l['load_balancer_id'] = db_lb.id
            l['project_id'] = db_lb.project_id
            new_lists.append(listener.ListenersController()._graph_create(
                lock_session, l, pool_name_ids=pool_name_ids))

        return new_pools, new_lists

    @wsme_pecan.wsexpose(lb_types.LoadBalancerRootResponse,
                         wtypes.text, status_code=200,
                         body=lb_types.LoadBalancerRootPUT)
    def put(self, id, load_balancer):
        """Updates a load balancer."""
        load_balancer = load_balancer.loadbalancer
        context = pecan.request.context.get('octavia_context')
        db_lb = self._get_db_lb(context.session, id)

        self._auth_validate_action(context, db_lb.project_id,
                                   constants.RBAC_PUT)

        self._test_lb_status(context.session, id)
        try:
            LOG.info("Sending updated Load Balancer %s to the handler", id)
            self.handler.update(db_lb, load_balancer)
        except Exception:
            with excutils.save_and_reraise_exception(reraise=False):
                self.repositories.load_balancer.update(
                    context.session, id, provisioning_status=constants.ERROR)
        db_lb = self._get_db_lb(context.session, id)
        result = self._convert_db_to_type(db_lb, lb_types.LoadBalancerResponse)
        return lb_types.LoadBalancerRootResponse(loadbalancer=result)

    @wsme_pecan.wsexpose(None, wtypes.text, wtypes.text, status_code=204)
    def delete(self, id, cascade=False):
        """Deletes a load balancer."""
        context = pecan.request.context.get('octavia_context')
        cascade = strutils.bool_from_string(cascade)
        db_lb = self._get_db_lb(context.session, id)

        self._auth_validate_action(context, db_lb.project_id,
                                   constants.RBAC_DELETE)

        with db_api.get_lock_session() as lock_session:
            self._test_lb_status(lock_session, id,
                                 lb_status=constants.PENDING_DELETE)
            if (db_lb.listeners or db_lb.pools) and not cascade:
                msg = _("Cannot delete Load Balancer %s - "
                        "it has children") % id
                LOG.warning(msg)
                raise exceptions.ValidationException(detail=msg)

        try:
            LOG.info("Sending deleted Load Balancer %s to the handler", id)
            self.handler.delete(db_lb, cascade)
        except Exception:
            with excutils.save_and_reraise_exception(reraise=False):
                self.repositories.load_balancer.update(
                    context.session, id,
                    provisioning_status=constants.ERROR)

    @pecan.expose()
    def _lookup(self, id, *remainder):
        """Overridden pecan _lookup method for custom routing.

        Currently it checks if this was a statuses request and routes
        the request to the StatusesController.
        """
        if id and len(remainder) and (remainder[0] == 'status' or
                                      remainder[0] == 'stats'):
            controller = remainder[0]
            remainder = remainder[1:]
            if controller == 'status':
                return StatusController(lb_id=id), remainder
            elif controller == 'stats':
                return StatisticsController(lb_id=id), remainder


class StatusController(base.BaseController):
    RBAC_TYPE = constants.RBAC_LOADBALANCER

    def __init__(self, lb_id):
        super(StatusController, self).__init__()
        self.id = lb_id

    @wsme_pecan.wsexpose(lb_types.StatusRootResponse, wtypes.text,
                         status_code=200)
    def get(self):
        context = pecan.request.context.get('octavia_context')
        load_balancer = self._get_db_lb(context.session, self.id)
        if not load_balancer:
            LOG.info("Load balancer %s not found.", id)
            raise exceptions.NotFound(
                resource=data_models.LoadBalancer._name(),
                id=id)

        self._auth_validate_action(context, load_balancer.project_id,
                                   constants.RBAC_GET_STATUS)

        result = self._convert_db_to_type(
            load_balancer, lb_types.LoadBalancerStatusResponse)
        result = lb_types.StatusResponse(loadbalancer=result)
        return lb_types.StatusRootResponse(statuses=result)


class StatisticsController(base.BaseController, stats.StatsMixin):
    RBAC_TYPE = constants.RBAC_LOADBALANCER

    def __init__(self, lb_id):
        super(StatisticsController, self).__init__()
        self.id = lb_id

    @wsme_pecan.wsexpose(lb_types.StatisticsRootResponse, wtypes.text,
                         status_code=200)
    def get(self):
        context = pecan.request.context.get('octavia_context')
        load_balancer = self._get_db_lb(context.session, self.id)
        if not load_balancer:
            LOG.info("Load balancer %s not found.", id)
            raise exceptions.NotFound(
                resource=data_models.LoadBalancer._name(),
                id=id)

        self._auth_validate_action(context, load_balancer.project_id,
                                   constants.RBAC_GET_STATS)

        lb_stats = self.get_loadbalancer_stats(context.session, self.id)

        result = self._convert_db_to_type(
            lb_stats, lb_types.LoadBalancerStatisticsResponse)
        return lb_types.StatisticsRootResponse(stats=result)
