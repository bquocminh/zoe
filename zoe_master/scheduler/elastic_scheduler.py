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

"""
The Elastic scheduler is the implementation of the scheduling algorithm presented in this paper:
https://arxiv.org/abs/1611.09528
"""

import logging
import threading
import time

from zoe_lib.state import Execution, SQLManager, Service  # pylint: disable=unused-import
from zoe_master.exceptions import ZoeException

from zoe_master.backends.interface import terminate_execution, terminate_service, start_elastic, start_essential, update_service_resource_limits
from zoe_master.scheduler.simulated_platform import SimulatedPlatform
from zoe_master.exceptions import UnsupportedSchedulerPolicyError
from zoe_master.stats import NodeStats  # pylint: disable=unused-import
from zoe_master.metrics.base import StatsManager  # pylint: disable=unused-import

log = logging.getLogger(__name__)

SELF_TRIGGER_TIMEOUT = 60  # the scheduler will trigger itself periodically in case platform resources have changed outside its control


def catch_exceptions_and_retry(func):
    """Decorator to catch exceptions in threaded functions."""
    def wrapper(self):
        """The wrapper."""
        while True:
            try:
                func(self)
            except BaseException:  # pylint: disable=broad-except
                log.exception('Unmanaged exception in thread loop')
            else:
                log.debug('Thread terminated')
                break
    return wrapper


class ExecutionProgress:
    """Additional data for tracking execution sizes while in the queue."""
    def __init__(self):
        self.last_time_scheduled = 0
        self.progress_sequence = []


class ZoeElasticScheduler:
    """The Scheduler class for size-based scheduling. Policy can be "FIFO" or "SIZE"."""
    def __init__(self, state: SQLManager, policy, metrics: StatsManager):
        if policy != 'FIFO' and policy != 'SIZE' and policy != 'DYNSIZE':
            raise UnsupportedSchedulerPolicyError
        self.metrics = metrics
        self.trigger_semaphore = threading.Semaphore(0)
        self.policy = policy
        self.queue = []
        self.queue_running = []
        self.additional_exec_state = {}
        self.async_threads = []
        self.loop_quit = False
        self.loop_th = threading.Thread(target=self.loop_start_th, name='scheduler')
        self.core_limit_recalc_trigger = threading.Event()
        self.core_limit_th = threading.Thread(target=self._adjust_core_limits, name='adjust_core_limits')
        self.state = state
        for execution in self.state.executions.select(status='running'):
            if execution.all_services_running:
                self.queue_running.append(execution)
            else:
                self.queue.append(execution)
                self.additional_exec_state[execution.id] = ExecutionProgress()
        self.loop_th.start()
        self.core_limit_th.start()

    def trigger(self):
        """Trigger a scheduler run."""
        self.trigger_semaphore.release()

    def incoming(self, execution: Execution):
        """
        This method adds the execution to the end of the queue and triggers the scheduler.
        :param execution: The execution
        :return:
        """
        exec_data = ExecutionProgress()
        self.additional_exec_state[execution.id] = exec_data
        self.queue.append(execution)
        self.trigger()

    def terminate(self, execution: Execution) -> None:
        """
        Inform the master that an execution has been terminated. This can be done asynchronously.
        :param execution: the terminated execution
        :return: None
        """
        def async_termination(e):
            """Actual termination runs in a thread."""
            with e.termination_lock:
                try:
                    terminate_execution(e)
                except ZoeException as ex:
                    log.error('Error in termination thread: {}'.format(ex))
                    return
                self.trigger()
            log.debug('Execution {} terminated successfully'.format(e.id))

        try:
            self.queue.remove(execution)
        except ValueError:
            try:
                self.queue_running.remove(execution)
            except ValueError:
                log.error('Cannot terminate execution {}, it is not in any queue'.format(execution.id))
                return

        try:
            del self.additional_exec_state[execution.id]
        except KeyError:
            pass
        self.core_limit_recalc_trigger.set()

        th = threading.Thread(target=async_termination, name='termination_{}'.format(execution.id), args=(execution,))
        th.start()
        self.async_threads.append(th)

    def _cleanup_async_threads(self):
        counter = len(self.async_threads)
        while counter > 0:
            if len(self.async_threads) == 0:
                break
            th = self.async_threads.pop(0)
            th.join(0.1)
            if th.isAlive():  # join failed
                # log.debug('Thread {} join failed'.format(th.name))
                self.async_threads.append(th)
            counter -= 1

    def _refresh_execution_sizes(self):
        if self.policy == "FIFO":
            return
        elif self.policy == "SIZE":
            return
        elif self.policy == "DYNSIZE":
            for execution in self.queue:  # type: Execution
                exec_data = self.additional_exec_state[execution.id]
                if exec_data.last_time_scheduled == 0:
                    continue
                elif execution.size <= 0:
                    execution.set_size(execution.total_reservations.cores.min * execution.total_reservations.memory.min)
                    continue
                new_size = execution.size - (time.time() - exec_data.last_time_scheduled) * (256 * 1024 ** 2)  # to be tuned
                execution.set_size(new_size)

    def _pop_all(self):
        out_list = []
        for execution in self.queue:  # type: Execution
            ret = execution.termination_lock.acquire(blocking=False)
            if ret and execution.status != Execution.TERMINATED_STATUS:
                out_list.append(execution)
            else:
                log.debug('While popping, throwing away execution {} that has the termination lock held'.format(execution.id))

        return out_list

    def _requeue(self, execution: Execution):
        execution.termination_lock.release()
        self.additional_exec_state[execution.id].last_time_scheduled = time.time()
        if execution not in self.queue:  # sanity check: the execution should be in the queue
            log.warning("Execution {} wants to be re-queued, but it is not in the queue".format(execution.id))

    @catch_exceptions_and_retry
    def loop_start_th(self):  # pylint: disable=too-many-locals
        """The Scheduler thread loop."""
        auto_trigger = SELF_TRIGGER_TIMEOUT
        while True:
            ret = self.trigger_semaphore.acquire(timeout=1)
            if not ret:  # Semaphore timeout, do some thread cleanup
                self._cleanup_async_threads()
                auto_trigger -= 1
                if auto_trigger == 0:
                    auto_trigger = SELF_TRIGGER_TIMEOUT
                    self.trigger()
                continue
            if self.loop_quit:
                break

            self._check_dead_services()
            if len(self.queue) == 0:
                log.debug("Scheduler loop has been triggered, but the queue is empty")
                self.core_limit_recalc_trigger.set()
                continue
            log.debug("Scheduler loop has been triggered")

            while True:  # Inner loop will run until no new executions can be started or the queue is empty
                self._refresh_execution_sizes()

                if self.policy == "SIZE" or self.policy == "DYNSIZE":
                    self.queue.sort(key=lambda execution: execution.size)

                jobs_to_attempt_scheduling = self._pop_all()
                log.debug('Scheduler inner loop, jobs to attempt scheduling:')
                for job in jobs_to_attempt_scheduling:
                    log.debug("-> {} ({})".format(job, job.size))

                try:
                    platform_state = self.metrics.current_stats
                except ZoeException:
                    log.error('Cannot retrieve platform state, cannot schedule')
                    for job in jobs_to_attempt_scheduling:
                        self._requeue(job)
                    break

                cluster_status_snapshot = SimulatedPlatform(platform_state)

                jobs_to_launch = []
                free_resources = cluster_status_snapshot.aggregated_free_memory()

                # Try to find a placement solution using a snapshot of the platform status
                for job in jobs_to_attempt_scheduling:  # type: Execution
                    jobs_to_launch_copy = jobs_to_launch.copy()

                    # remove all elastic services from the previous simulation loop
                    for job_aux in jobs_to_launch:  # type: Execution
                        cluster_status_snapshot.deallocate_elastic(job_aux)

                    job_can_start = False
                    if not job.is_running:
                        job_can_start = cluster_status_snapshot.allocate_essential(job)

                    if job_can_start or job.is_running:
                        jobs_to_launch.append(job)

                    # Try to put back the elastic services
                    for job_aux in jobs_to_launch:
                        cluster_status_snapshot.allocate_elastic(job_aux)

                    current_free_resources = cluster_status_snapshot.aggregated_free_memory()
                    if current_free_resources >= free_resources:
                        jobs_to_launch = jobs_to_launch_copy
                        break
                    free_resources = current_free_resources

                placements = cluster_status_snapshot.get_service_allocation()
                log.debug('Allocation after simulation: {}'.format(placements))

                # We port the results of the simulation into the real cluster
                for job in jobs_to_launch:  # type: Execution
                    if not job.essential_services_running:
                        ret = start_essential(job, placements)
                        if ret == "fatal":
                            jobs_to_attempt_scheduling.remove(job)
                            self.queue.remove(job)
                            job.termination_lock.release()
                            continue  # trow away the execution
                        elif ret == "requeue":
                            self._requeue(job)
                            continue
                        elif ret == "ok":
                            job.set_running()

                        assert ret == "ok"

                    start_elastic(job, placements)

                    if job.all_services_active:
                        log.debug('execution {}: all services are active'.format(job.id))
                        job.termination_lock.release()
                        jobs_to_attempt_scheduling.remove(job)
                        self.queue.remove(job)
                        self.queue_running.append(job)

                self.core_limit_recalc_trigger.set()

                for job in jobs_to_attempt_scheduling:
                    self._requeue(job)

                if len(self.queue) == 0:
                    log.debug('empty queue, exiting inner loop')
                    break
                if len(jobs_to_launch) == 0:
                    log.debug('No executions could be started, exiting inner loop')
                    break

    def quit(self):
        """Stop the scheduler thread."""
        self.loop_quit = True
        self.trigger()
        self.core_limit_recalc_trigger.set()
        self.loop_th.join()
        self.core_limit_th.join()

    def stats(self):
        """Scheduler statistics."""
        if self.policy == "SIZE":
            queue = sorted(self.queue, key=lambda execution: execution.size)
        else:
            queue = self.queue

        return {
            'queue_length': len(self.queue),
            'running_length': len(self.queue_running),
            'termination_threads_count': len(self.async_threads),
            'queue': [s.id for s in queue],
            'running_queue': [s.id for s in self.queue_running]
        }

    @catch_exceptions_and_retry
    def _adjust_core_limits(self):
        self.core_limit_recalc_trigger.clear()
        while not self.loop_quit:
            self.core_limit_recalc_trigger.wait()
            if self.loop_quit:
                break
            stats = self.metrics.current_stats
            for node in stats.nodes:  # type: NodeStats
                new_core_allocations = {}
                node_services = self.state.services.select(backend_host=node.name, backend_status=Service.BACKEND_START_STATUS)
                if len(node_services) == 0:
                    continue

                for service in node_services:
                    new_core_allocations[service.id] = service.resource_reservation.cores.min

                if node.cores_reserved < node.cores_total:
                    cores_free = node.cores_total - node.cores_reserved
                    cores_to_add = cores_free / len(node_services)
                else:
                    cores_to_add = 0

                for service in node_services:
                    update_service_resource_limits(service, cores=new_core_allocations[service.id] + cores_to_add)

            self.core_limit_recalc_trigger.clear()

    def _check_dead_services(self):
        # Check for executions that are no longer viable since an essential service died
        for execution in self.queue_running:
            for service in execution.services:
                if service.essential and service.backend_status == service.BACKEND_DIE_STATUS:
                    log.info("Essential service {} ({}) of execution {} died, terminating execution".format(service.id, service.name, execution.id))
                    service.restarted()
                    execution.set_cleaning_up()
                    self.terminate(execution)
                    break
        # Check for executions that need to be rescheduled because one of the elastic components died
        # Do it in two loops to prevent rescheduling executions that need to be terminated
        for execution in self.queue_running:
            for service in execution.services:
                if not service.essential and service.backend_status == service.BACKEND_DIE_STATUS:
                    log.info("Elastic service {} ({}) of execution {} died, rescheduling".format(service.id, service.name, execution.id))
                    terminate_service(service)
                    service.restarted()
                    self.queue_running.remove(execution)
                    self.queue.append(execution)
                    break
