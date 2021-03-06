# Copyright (c) 2012 Rackspace Hosting
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

"""
Cells Service Manager
"""
import datetime
import time

from oslo.config import cfg

from nova.cells import messaging
from nova.cells import state as cells_state
from nova.cells import utils as cells_utils
from nova import context
from nova import exception
from nova import manager
from nova.openstack.common import importutils
from nova.openstack.common import log as logging
from nova.openstack.common import timeutils

cell_manager_opts = [
        cfg.StrOpt('driver',
                default='nova.cells.rpc_driver.CellsRPCDriver',
                help='Cells communication driver to use'),
        cfg.IntOpt("instance_updated_at_threshold",
                default=3600,
                help="Number of seconds after an instance was updated "
                        "or deleted to continue to update cells"),
        cfg.IntOpt("instance_update_num_instances",
                default=1,
                help="Number of instances to update per periodic task run")
]


LOG = logging.getLogger(__name__)

CONF = cfg.CONF
CONF.register_opts(cell_manager_opts, group='cells')


class CellsManager(manager.Manager):
    """The nova-cells manager class.  This class defines RPC
    methods that the local cell may call.  This class is NOT used for
    messages coming from other cells.  That communication is
    driver-specific.

    Communication to other cells happens via the messaging module.  The
    MessageRunner from that module will handle routing the message to
    the correct cell via the communications driver.  Most methods below
    create 'targeted' (where we want to route a message to a specific cell)
    or 'broadcast' (where we want a message to go to multiple cells)
    messages.

    Scheduling requests get passed to the scheduler class.
    """
    RPC_API_VERSION = '1.4'

    def __init__(self, *args, **kwargs):
        # Mostly for tests.
        cell_state_manager = kwargs.pop('cell_state_manager', None)
        super(CellsManager, self).__init__(*args, **kwargs)
        if cell_state_manager is None:
            cell_state_manager = cells_state.CellStateManager
        self.state_manager = cell_state_manager()
        self.msg_runner = messaging.MessageRunner(self.state_manager)
        cells_driver_cls = importutils.import_class(
                CONF.cells.driver)
        self.driver = cells_driver_cls()
        self.instances_to_heal = iter([])

    def post_start_hook(self):
        """Have the driver start its consumers for inter-cell communication.
        Also ask our child cells for their capacities and capabilities so
        we get them more quickly than just waiting for the next periodic
        update.  Receiving the updates from the children will cause us to
        update our parents.  If we don't have any children, just update
        our parents immediately.
        """
        # FIXME(comstud): There's currently no hooks when services are
        # stopping, so we have no way to stop consumers cleanly.
        self.driver.start_consumers(self.msg_runner)
        ctxt = context.get_admin_context()
        if self.state_manager.get_child_cells():
            self.msg_runner.ask_children_for_capabilities(ctxt)
            self.msg_runner.ask_children_for_capacities(ctxt)
        else:
            self._update_our_parents(ctxt)

    @manager.periodic_task
    def _update_our_parents(self, ctxt):
        """Update our parent cells with our capabilities and capacity
        if we're at the bottom of the tree.
        """
        self.msg_runner.tell_parents_our_capabilities(ctxt)
        self.msg_runner.tell_parents_our_capacities(ctxt)

    @manager.periodic_task
    def _heal_instances(self, ctxt):
        """Periodic task to send updates for a number of instances to
        parent cells.

        On every run of the periodic task, we will attempt to sync
        'CONF.cells.instance_update_num_instances' number of instances.
        When we get the list of instances, we shuffle them so that multiple
        nova-cells services aren't attempting to sync the same instances
        in lockstep.

        If CONF.cells.instance_update_at_threshold is set, only attempt
        to sync instances that have been updated recently.  The CONF
        setting defines the maximum number of seconds old the updated_at
        can be.  Ie, a threshold of 3600 means to only update instances
        that have modified in the last hour.
        """

        if not self.state_manager.get_parent_cells():
            # No need to sync up if we have no parents.
            return

        info = {'updated_list': False}

        def _next_instance():
            try:
                instance = self.instances_to_heal.next()
            except StopIteration:
                if info['updated_list']:
                    return
                threshold = CONF.cells.instance_updated_at_threshold
                updated_since = None
                if threshold > 0:
                    updated_since = timeutils.utcnow() - datetime.timedelta(
                            seconds=threshold)
                self.instances_to_heal = cells_utils.get_instances_to_sync(
                        ctxt, updated_since=updated_since, shuffle=True,
                        uuids_only=True)
                info['updated_list'] = True
                try:
                    instance = self.instances_to_heal.next()
                except StopIteration:
                    return
            return instance

        rd_context = ctxt.elevated(read_deleted='yes')

        for i in xrange(CONF.cells.instance_update_num_instances):
            while True:
                # Yield to other greenthreads
                time.sleep(0)
                instance_uuid = _next_instance()
                if not instance_uuid:
                    return
                try:
                    instance = self.db.instance_get_by_uuid(rd_context,
                            instance_uuid)
                except exception.InstanceNotFound:
                    continue
                self._sync_instance(ctxt, instance)
                break

    def _sync_instance(self, ctxt, instance):
        """Broadcast an instance_update or instance_destroy message up to
        parent cells.
        """
        if instance['deleted']:
            self.instance_destroy_at_top(ctxt, instance)
        else:
            self.instance_update_at_top(ctxt, instance)

    def schedule_run_instance(self, ctxt, host_sched_kwargs):
        """Pick a cell (possibly ourselves) to build new instance(s)
        and forward the request accordingly.
        """
        # Target is ourselves first.
        our_cell = self.state_manager.get_my_state()
        self.msg_runner.schedule_run_instance(ctxt, our_cell,
                                              host_sched_kwargs)

    def get_cell_info_for_neighbors(self, _ctxt):
        """Return cell information for our neighbor cells."""
        return self.state_manager.get_cell_info_for_neighbors()

    def run_compute_api_method(self, ctxt, cell_name, method_info, call):
        """Call a compute API method in a specific cell."""
        response = self.msg_runner.run_compute_api_method(ctxt,
                                                          cell_name,
                                                          method_info,
                                                          call)
        if call:
            return response.value_or_raise()

    def instance_update_at_top(self, ctxt, instance):
        """Update an instance at the top level cell."""
        self.msg_runner.instance_update_at_top(ctxt, instance)

    def instance_destroy_at_top(self, ctxt, instance):
        """Destroy an instance at the top level cell."""
        self.msg_runner.instance_destroy_at_top(ctxt, instance)

    def instance_delete_everywhere(self, ctxt, instance, delete_type):
        """This is used by API cell when it didn't know what cell
        an instance was in, but the instance was requested to be
        deleted or soft_deleted.  So, we'll broadcast this everywhere.
        """
        self.msg_runner.instance_delete_everywhere(ctxt, instance,
                                                   delete_type)

    def instance_fault_create_at_top(self, ctxt, instance_fault):
        """Create an instance fault at the top level cell."""
        self.msg_runner.instance_fault_create_at_top(ctxt, instance_fault)

    def bw_usage_update_at_top(self, ctxt, bw_update_info):
        """Update bandwidth usage at top level cell."""
        self.msg_runner.bw_usage_update_at_top(ctxt, bw_update_info)

    def sync_instances(self, ctxt, project_id, updated_since, deleted):
        """Force a sync of all instances, potentially by project_id,
        and potentially since a certain date/time.
        """
        self.msg_runner.sync_instances(ctxt, project_id, updated_since,
                                       deleted)

    def service_get_all(self, ctxt, filters):
        """Return services in this cell and in all child cells."""
        responses = self.msg_runner.service_get_all(ctxt, filters)
        ret_services = []
        # 1 response per cell.  Each response is a list of services.
        for response in responses:
            services = response.value_or_raise()
            for service in services:
                cells_utils.add_cell_to_service(service, response.cell_name)
                ret_services.append(service)
        return ret_services

    def service_get_by_compute_host(self, ctxt, host_name):
        """Return a service entry for a compute host in a certain cell."""
        cell_name, host_name = cells_utils.split_cell_and_item(host_name)
        response = self.msg_runner.service_get_by_compute_host(ctxt,
                                                               cell_name,
                                                               host_name)
        service = response.value_or_raise()
        cells_utils.add_cell_to_service(service, response.cell_name)
        return service

    def proxy_rpc_to_manager(self, ctxt, topic, rpc_message, call, timeout):
        """Proxy an RPC message as-is to a manager."""
        compute_topic = CONF.compute_topic
        cell_and_host = topic[len(compute_topic) + 1:]
        cell_name, host_name = cells_utils.split_cell_and_item(cell_and_host)
        response = self.msg_runner.proxy_rpc_to_manager(ctxt, cell_name,
                host_name, topic, rpc_message, call, timeout)
        return response.value_or_raise()

    def task_log_get_all(self, ctxt, task_name, period_beginning,
                         period_ending, host=None, state=None):
        """Get task logs from the DB from all cells or a particular
        cell.

        If 'host' is not None, host will be of the format 'cell!name@host',
        with '@host' being optional.  The query will be directed to the
        appropriate cell and return all task logs, or task logs matching
        the host if specified.

        'state' also may be None.  If it's not, filter by the state as well.
        """
        if host is None:
            cell_name = None
        else:
            result = cells_utils.split_cell_and_item(host)
            cell_name = result[0]
            if len(result) > 1:
                host = result[1]
            else:
                host = None
        responses = self.msg_runner.task_log_get_all(ctxt, cell_name,
                task_name, period_beginning, period_ending,
                host=host, state=state)
        # 1 response per cell.  Each response is a list of task log
        # entries.
        ret_task_logs = []
        for response in responses:
            task_logs = response.value_or_raise()
            for task_log in task_logs:
                cells_utils.add_cell_to_task_log(task_log,
                                                 response.cell_name)
                ret_task_logs.append(task_log)
        return ret_task_logs

    def compute_node_get(self, ctxt, compute_id):
        """Get a compute node by ID in a specific cell."""
        cell_name, compute_id = cells_utils.split_cell_and_item(
                compute_id)
        response = self.msg_runner.compute_node_get(ctxt, cell_name,
                                                    compute_id)
        node = response.value_or_raise()
        cells_utils.add_cell_to_compute_node(node, cell_name)
        return node

    def compute_node_get_all(self, ctxt, hypervisor_match=None):
        """Return list of compute nodes in all cells."""
        responses = self.msg_runner.compute_node_get_all(ctxt,
                hypervisor_match=hypervisor_match)
        # 1 response per cell.  Each response is a list of compute_node
        # entries.
        ret_nodes = []
        for response in responses:
            nodes = response.value_or_raise()
            for node in nodes:
                cells_utils.add_cell_to_compute_node(node,
                                                     response.cell_name)
                ret_nodes.append(node)
        return ret_nodes

    def compute_node_stats(self, ctxt):
        """Return compute node stats totals from all cells."""
        responses = self.msg_runner.compute_node_stats(ctxt)
        totals = {}
        for response in responses:
            data = response.value_or_raise()
            for key, val in data.iteritems():
                totals.setdefault(key, 0)
                totals[key] += val
        return totals
