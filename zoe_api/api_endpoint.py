# Copyright (c) 2016, Daniele Venzano
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

"""The real API, exposed as web pages or REST API."""

import logging
import os

import zoe_api.exceptions
import zoe_api.master_api
import zoe_lib.applications
import zoe_lib.exceptions
import zoe_lib.state
from zoe_lib.config import get_conf

log = logging.getLogger(__name__)


class APIEndpoint:
    """
    The APIEndpoint class.

    :type master: zoe_api.master_api.APIManager
    :type sql: zoe_lib.sql_manager.SQLManager
    """
    def __init__(self, master_api, sql_manager: zoe_lib.state.SQLManager):
        self.master = master_api
        self.sql = sql_manager

    def execution_by_id(self, user: zoe_lib.state.User, execution_id: int) -> zoe_lib.state.Execution:
        """Lookup an execution by its ID."""
        e = self.sql.executions.select(id=execution_id, only_one=True)
        if e is None:
            raise zoe_api.exceptions.ZoeNotFoundException('No such execution')
        assert isinstance(e, zoe_lib.state.Execution)
        if e.user_id != user.id and not user.role.can_operate_others:
            raise zoe_api.exceptions.ZoeAuthException()
        return e

    def execution_list(self, user: zoe_lib.state.User, **filters):
        """Generate a optionally filtered list of executions."""
        if not user.role.can_operate_others:
            filters['user_id'] = user.id
        execs = self.sql.executions.select(**filters)
        return execs

    def execution_count(self, user: zoe_lib.state.User, **filters):
        """Count the number of executions optionally filtered."""
        if not user.role.can_operate_others:
            filters['user_id'] = user.id
        return self.sql.executions.count(**filters)

    def zapp_validate(self, application_description):
        """Validates the passed ZApp description against the supported schema."""
        try:
            zoe_lib.applications.app_validate(application_description)
        except zoe_lib.exceptions.InvalidApplicationDescription as e:
            raise zoe_api.exceptions.ZoeException('Invalid application description: ' + e.message)

    def _check_quota(self, user: zoe_lib.state.User, application_description):  # pylint: disable=unused-argument
        """Check quota for given user and execution."""
        quota = self.sql.quota.select(only_one=True, **{'id': user.quota_id})

        running_execs = self.sql.executions.select(**{'status': 'running', 'user_id': user.id})
        running_execs += self.sql.executions.select(**{'status': 'starting', 'user_id': user.id})
        running_execs += self.sql.executions.select(**{'status': 'scheduled', 'user_id': user.id})
        running_execs += self.sql.executions.select(**{'status': 'image download', 'user_id': user.id})
        running_execs += self.sql.executions.select(**{'status': 'submitted', 'user_id': user.id})
        if len(running_execs) >= quota.concurrent_executions:
            raise zoe_api.exceptions.ZoeException('You cannot run more than {} executions at a time, quota exceeded.'.format(quota.concurrent_executions))

        # TODO: implement core and memory quotas

    def execution_start(self, user: zoe_lib.state.User, exec_name, application_description):
        """Start an execution."""
        try:
            zoe_lib.applications.app_validate(application_description)
        except zoe_lib.exceptions.InvalidApplicationDescription as e:
            raise zoe_api.exceptions.ZoeException('Invalid application description: ' + e.message)

        self._check_quota(user, application_description)

        new_id = self.sql.executions.insert(exec_name, user.id, application_description)
        success, message = self.master.execution_start(new_id)
        if not success:
            raise zoe_api.exceptions.ZoeException('The Zoe master is unavailable, execution will be submitted automatically when the master is back up ({}).'.format(message))

        return new_id

    def execution_terminate(self, user: zoe_lib.state.User, exec_id: int):
        """Terminate an execution."""
        e = self.sql.executions.select(id=exec_id, only_one=True)
        assert isinstance(e, zoe_lib.state.Execution)
        if e is None:
            raise zoe_api.exceptions.ZoeNotFoundException('No such execution')

        if e.user_id != user.id and not user.role.can_operate_others:
            raise zoe_api.exceptions.ZoeException('You are not authorized to terminate this execution')

        if e.is_active:
            return self.master.execution_terminate(exec_id)
        else:
            raise zoe_api.exceptions.ZoeException('Execution is not running')

    def execution_delete(self, user: zoe_lib.state.User, exec_id: int):
        """Delete an execution."""
        if not user.role.can_delete_executions:
            raise zoe_api.exceptions.ZoeAuthException()

        e = self.sql.executions.select(id=exec_id, only_one=True)
        assert isinstance(e, zoe_lib.state.Execution)
        if e is None:
            raise zoe_api.exceptions.ZoeNotFoundException('No such execution')

        if e.user_id != user.id and not user.role.can_operate_others:
            raise zoe_api.exceptions.ZoeException('You are not authorized to terminate this execution')

        if e.is_active:
            raise zoe_api.exceptions.ZoeException('Cannot delete an active execution')

        status, message = self.master.execution_delete(exec_id)
        if status:
            self.sql.executions.delete(exec_id)
            return True, ''
        else:
            raise zoe_api.exceptions.ZoeException(message)

    def service_by_id(self, user: zoe_lib.state.User, service_id: int) -> zoe_lib.state.Service:
        """Lookup a service by its ID."""
        service = self.sql.services.select(id=service_id, only_one=True)
        if service is None:
            raise zoe_api.exceptions.ZoeNotFoundException('No such execution')
        if service.user_id != user.id and not user.role.can_operate_others:
            raise zoe_api.exceptions.ZoeAuthException()
        return service

    def service_list(self, user: zoe_lib.state.User, **filters):
        """Generate a optionally filtered list of services."""
        if not user.role.can_operate_others:
            filters['user_id'] = user.id
        return self.sql.services.select(**filters)

    def service_logs(self, user: zoe_lib.state.User, service_id):
        """Retrieve the logs for the given service.
        If stream is True, a file object is returned, otherwise the log contents as a str object.
        """
        service = self.sql.services.select(id=service_id, only_one=True)
        if service is None:
            raise zoe_api.exceptions.ZoeNotFoundException('No such service')
        if service.user_id != user.id and not user.role.can_operate_others:
            raise zoe_api.exceptions.ZoeAuthException()

        path = os.path.join(get_conf().service_logs_base_path, get_conf().deployment_name, str(service.execution_id), service.name + '.txt')
        if not os.path.exists(path):
            raise zoe_api.exceptions.ZoeNotFoundException('Service log not available')
        return open(path, encoding='utf-8')

    def statistics_scheduler(self):
        """Retrieve statistics about the scheduler."""
        success, message = self.master.scheduler_statistics()
        if success:
            for node in message['platform_stats']['nodes']:  # JSON does not like hash keys to be integers, so we need to convert manually
                for str_service_id in list(node['service_stats'].keys()):
                    node['service_stats'][int(str_service_id)] = node['service_stats'][str_service_id]
                    del node['service_stats'][str_service_id]
            return message
        else:
            raise zoe_api.exceptions.ZoeException(message=message)

    def execution_endpoints(self, user: zoe_lib.state.User, execution: zoe_lib.state.Execution):
        """Return a list of the services and public endpoints available for a certain execution."""
        services_info = []
        endpoints = []
        for service in execution.services:
            services_info.append(self.service_by_id(user, service.id))
            for port in service.description['ports']:
                port_key = str(port['port_number']) + "/" + port['protocol']
                backend_port = self.sql.ports.select(only_one=True, service_id=service.id, internal_name=port_key)
                if backend_port is not None and backend_port.external_ip is not None:
                    endpoint = port['url_template'].format(**{"ip_port": backend_port.external_ip + ":" + str(backend_port.external_port)})
                    endpoints.append((port['name'], endpoint))

        return services_info, endpoints

    def user_by_name(self, username) -> zoe_lib.state.User:
        """Finds a user in the database looking it up by its username."""
        return self.sql.user.select(only_one=True, **{'username': username})

    def user_by_id(self, user: zoe_lib.state.User, user_id: int) -> zoe_lib.state.User:
        """Finds a user in the database looking it up by its username."""
        if user.id == user_id:
            return user
        if not user.role.can_operate_others:
            raise zoe_api.exceptions.ZoeAuthException()

        return self.sql.user.select(only_one=True, id=user_id)

    def user_delete(self, user: zoe_lib.state.User, user_id: int):
        """Deletes the user identified by the ID."""
        if not user.role.can_change_config:
            raise zoe_api.exceptions.ZoeAuthException()

        self.sql.user.delete(user_id)
