# Copyright (c) 2017, Daniele Venzano
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

"""Ajax API for the Zoe web interface."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor

import tornado.websocket
import tornado.iostream
import tornado.gen

import zoe_api.exceptions
from zoe_api.api_endpoint import APIEndpoint  # pylint: disable=unused-import
from zoe_api.web.utils import get_auth, catch_exceptions

log = logging.getLogger(__name__)

THREAD_POOL = ThreadPoolExecutor(20)


class WebSocketEndpointWeb(tornado.websocket.WebSocketHandler):
    """Handler class"""
    def initialize(self, **kwargs):
        """Initializes the request handler."""
        super().initialize()
        self.api_endpoint = kwargs['api_endpoint']  # type: APIEndpoint
        self.uid = None
        self.role = None
        self.connection_closed = None

    @catch_exceptions
    def open(self, *args, **kwargs):
        """Invoked when a new WebSocket is opened."""
        log.debug('WebSocket opened')
        uid, role = get_auth(self)
        if uid is None:
            self.close(401, "Unauthorized")
        else:
            self.uid = uid
            self.role = role

    @catch_exceptions
    @tornado.gen.coroutine
    def on_message(self, message):
        """WebSocket message handler."""

        if message is None:
            return

        request = json.loads(message)

        if request['command'] == 'query_status':
            try:
                execution = self.api_endpoint.execution_by_id(self.uid, self.role, request['exec_id'])
            except zoe_api.exceptions.ZoeNotFoundException:
                response = {
                    'status': 'ok',
                    'exec_status': 'none'
                }
            else:
                response = {
                    'status': 'ok',
                    'exec_status': execution.status
                }
                if execution.status == execution.RUNNING_STATUS:
                    services_info_, endpoints = self.api_endpoint.execution_endpoints(self.uid, self.role, execution)
                    response['endpoints'] = endpoints
                elif execution.status == execution.ERROR_STATUS or execution.status == execution.TERMINATED_STATUS:
                    self.api_endpoint.execution_delete(self.uid, self.role, execution.id)
            self.write_message(response)
        elif request['command'] == 'service_logs':
            log_obj = self.api_endpoint.service_logs(self.uid, self.role, request['service_id'])

            while not self.connection_closed:
                try:
                    log_line = yield THREAD_POOL.submit(next, log_obj)
                except StopIteration:
                    yield tornado.gen.sleep(0.2)
                    continue

                self.write_message(log_line)
        elif request['command'] == 'system_status':
            stats = self.api_endpoint.statistics_scheduler(self.uid, self.role)
            self.write_message(json.dumps(stats))
        else:
            response = {
                'status': 'error',
                'message': 'unknown request type'
            }
            self.write_message(response)

    def _stream_log_line(self, log_line):
        self.write_message(log_line)
        self.stream.read_until(b'\n', callback=self._stream_log_line)

    def on_close(self):
        """Invoked when the WebSocket is closed."""
        log.debug("WebSocket closed")
        self.connection_closed = True

    def data_received(self, chunk):
        """Not implemented as we do not use stream uploads"""
        pass
