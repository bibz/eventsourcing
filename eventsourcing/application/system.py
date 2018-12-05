import time
from abc import ABCMeta, abstractmethod
from collections import OrderedDict, deque, defaultdict
from threading import Barrier, BrokenBarrierError, Event, Lock, Thread, Timer
from time import sleep

import six
from six import with_metaclass
from six.moves.queue import Empty, Queue

from eventsourcing.application.process import ProcessApplication, Prompt
from eventsourcing.domain.model.decorators import retry
from eventsourcing.domain.model.events import subscribe, unsubscribe
from eventsourcing.exceptions import CausalDependencyFailed
from eventsourcing.interface.notificationlog import NotificationLogReader

DEFAULT_POLL_INTERVAL = 5


class System(object):
    def __init__(self, *pipeline_exprs, **kwargs):
        """
        Initialises a "process network" system object.

        :param pipeline_exprs: Pipeline expressions involving process application classes.

        Each pipeline expression of process classes shows directly which process
        follows which other process in the system.

        For example, the pipeline expression (A | B | C) shows that B follows A,
        and C follows B.

        The pipeline expression (A | A) shows that A follows A.

        The pipeline expression (A | B | A) shows that B follows A, and A follows B.

        The pipeline expressions ((A | B | A), (A | C | A)) are equivalent to (A | B | A | C | A).
        """
        self.pipelines_exprs = pipeline_exprs
        self.setup_tables = kwargs.get('setup_tables', False)
        self.infrastructure_class = kwargs.get('infrastructure_class', None)

        self.session = kwargs.get('session', None)

        self.process_classes = OrderedDict()
        for pipeline_expr in self.pipelines_exprs:
            for process_class in pipeline_expr:
                process_name = process_class.__name__.lower()
                if process_name not in self.process_classes:
                    self.process_classes[process_name] = process_class

        self.processes = None
        self.is_session_shared = True

        # Determine which process follows which.
        self.followers = OrderedDict()
        # A following is a list of process classes followed by a process class.
        # Todo: Factor this out, it's confusing. (Only used in ActorsRunner now).
        self.followings = OrderedDict()
        for pipeline_expr in self.pipelines_exprs:
            previous_name = None
            for process_class in pipeline_expr:
                process_name = process_class.__name__.lower()
                try:
                    follows = self.followings[process_name]
                except KeyError:
                    follows = []
                    self.followings[process_name] = follows

                try:
                    self.followers[process_name]
                except KeyError:
                    self.followers[process_name] = []

                if previous_name and previous_name not in follows:
                    follows.append(previous_name)
                    followers = self.followers[previous_name]
                    followers.append(process_name)

                previous_name = process_name

    def construct_app(self, process_class, **kwargs):
        kwargs = dict(kwargs)
        if 'setup_table' not in kwargs:
            kwargs['setup_table'] = self.setup_tables
        if 'session' not in kwargs and process_class.is_constructed_with_session:
            kwargs['session'] = self.session

        if self.infrastructure_class:
            process_class = process_class.mixin(self.infrastructure_class)

        process = process_class(**kwargs)

        if process_class.is_constructed_with_session and self.is_session_shared:
            if self.session is None:
                self.session = process.session

        return process

    def is_prompt(self, event):
        return isinstance(event, Prompt)

    def __enter__(self):
        self.__runner = SingleThreadedRunner(self)
        self.__runner.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.__runner.__exit__(exc_type, exc_val, exc_tb)
        del (self.__runner)

    def drop_tables(self):
        for process_class in self.process_classes.values():
            with self.construct_app(process_class, setup_table=False) as process:
                process.drop_table()


class SystemRunner(with_metaclass(ABCMeta)):

    def __init__(self, system: System):
        self.system = system

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @abstractmethod
    def start(self):
        pass

    @abstractmethod
    def close(self):
        pass


class InProcessRunner(SystemRunner):
    """
    Runs a system in the current process,
    either in the current thread, or with
    a thread for each process in the system.
    """

    def start(self):
        assert self.system.processes is None, "Already running"
        self.system.processes = {}

        # Construct the processes.
        for process_class in self.system.process_classes.values():
            process = self.system.construct_app(process_class)
            self.system.processes[process.name] = process

        # Tell each process about the processes it follows.
        for followed_name, followers in self.system.followers.items():
            followed = self.system.processes[followed_name]
            followed_log = followed.notification_log
            for follower_name in followers:
                follower = self.system.processes[follower_name]
                follower.follow(followed_name, followed_log)

        # Do something to propagate prompts.
        subscribe(
            predicate=self.system.is_prompt,
            handler=self.handle_prompt,
        )

    @abstractmethod
    def handle_prompt(self, prompt):
        pass

    def close(self):
        assert self.system.processes is not None, "Not running"
        for process in self.system.processes.values():
            process.close()
        unsubscribe(
            predicate=self.system.is_prompt,
            handler=self.handle_prompt,
        )


class SingleThreadedRunner(InProcessRunner):
    """
    Runs a system in the current thread.
    """

    def __init__(self, *args, **kwargs):
        super(SingleThreadedRunner, self).__init__(*args, **kwargs)
        self.pending_prompts = Queue()
        self.iteration_lock = Lock()

    def handle_prompt(self, prompt):
        self.run_followers(prompt)

    def run_followers(self, prompt):
        """
        First caller adds a prompt to queue and
        runs followers until there are no more
        pending prompts.

        Subsequent callers just add a prompt
        to the queue, avoiding recursion.
        """
        assert isinstance(prompt, Prompt)
        # Put the prompt on the queue.
        self.pending_prompts.put(prompt)

        if self.iteration_lock.acquire(False):
            start_time = time.time()
            i = 0
            try:
                while True:
                    try:
                        prompt = self.pending_prompts.get(False)
                    except Empty:
                        break
                    else:
                        followers = self.system.followers[prompt.process_name]
                        for follower_name in followers:
                            follower = self.system.processes[follower_name]
                            follower.run(prompt)
                            i += 1
                        self.pending_prompts.task_done()
            finally:
                run_frequency = i / (time.time() - start_time)
                # print(f"Run frequency: {run_frequency}")
                self.iteration_lock.release()

    # This is the old way of doing it, with recursion.
    # def run_followers_with_recursion(self, prompt):
    #     followers = self.system.followers[prompt.process_name]
    #     for follower_name in followers:
    #         follower = self.processes[follower_name]
    #         follower.run(prompt)
    #


class PromptQueuedMultiThreadedRunner(InProcessRunner):
    """
    Runs a system with a thread for each process.
    """

    def __init__(self, system: System, poll_interval=None, clock_speed=None):
        super(PromptQueuedMultiThreadedRunner, self).__init__(system=system)
        self.poll_interval = poll_interval or DEFAULT_POLL_INTERVAL
        assert isinstance(system, System)
        self.threads = {}
        self.clock_speed = clock_speed
        if self.clock_speed:
            self.clock_event = Event()
            self.stop_clock_event = Event()
        else:
            self.clock_event = None
            self.stop_clock_event = None

    def start(self):
        super(PromptQueuedMultiThreadedRunner, self).start()
        assert not self.threads, "Already started"

        self.inboxes = {}
        self.outboxes = {}
        self.clock_events = []

        # Setup queues.
        for process_name, upstream_names in self.system.followings.items():
            inbox_id = process_name.lower()
            if inbox_id not in self.inboxes:
                self.inboxes[inbox_id] = Queue()
            for upstream_class_name in upstream_names:
                outbox_id = upstream_class_name.lower()
                if outbox_id not in self.outboxes:
                    self.outboxes[outbox_id] = PromptOutbox()
                if inbox_id not in self.outboxes[outbox_id].downstream_inboxes:
                    self.outboxes[outbox_id].downstream_inboxes[inbox_id] = self.inboxes[inbox_id]

        # Construct application threads.
        for process_name, process in self.system.processes.items():
            process_instance_id = process_name
            if self.clock_event:
                process.clock_event = self.clock_event
                process.tick_interval = 1 / self.clock_speed

            thread = PromptQueuedApplicationThread(
                process=process,
                poll_interval=self.poll_interval,
                inbox=self.inboxes[process_instance_id],
                outbox=self.outboxes[process_instance_id],
                # Todo: Is it better to clock the prompts or the notifications?
                # clock_event=clock_event
            )
            self.threads[process_instance_id] = thread

        # Start application threads.
        for thread in self.threads.values():
            thread.start()

        # Start clock.
        if self.clock_speed:
            self.start_clock()

    def start_clock(self):
        tick_interval = 1 / self.clock_speed
        # print(f"Tick interval: {tick_interval:.6f}s")
        self.last_tick = None
        self.this_tick = None
        self.tick_adjustment = 0

        def set_clock_event():
            if self.stop_clock_event.is_set():
                return

            self.this_tick = time.process_time()

            if self.last_tick:
                tick_size = self.this_tick - self.last_tick

                tick_oversize = tick_size - tick_interval
                tick_oversize_percentage = 100 * (tick_oversize) / tick_interval
                if tick_oversize_percentage > 300:
                    print(f"Warning: Tick over size: { tick_size :.6f}s {tick_oversize_percentage:.2f}%")

                if abs(tick_oversize_percentage) < 300:
                    self.tick_adjustment += 0.5 * tick_interval * tick_oversize
                    max_tick_adjustment = 0.5 * tick_interval
                    min_tick_adjustment = 0
                    self.tick_adjustment = min(self.tick_adjustment, max_tick_adjustment)
                    self.tick_adjustment = max(self.tick_adjustment, min_tick_adjustment)

            self.last_tick = self.this_tick

            self.clock_event.set()
            self.clock_event.clear()

            if not self.stop_clock_event.is_set():
                set_timer()

        def set_timer():
            # print(f"Tick adjustment: {self.tick_adjustment:.6f}")
            if self.last_tick is not None:
                time_since_last_tick = time.process_time() - self.last_tick
                time_remaining = tick_interval - time_since_last_tick
                timer_interval = time_remaining - self.tick_adjustment
                if timer_interval < 0:
                    timer_interval = 0
                    # print("Warning: clock thread is running flat out!")
            else:
                timer_interval = 0
            timer = Timer(timer_interval, set_clock_event)
            timer.start()

        set_timer()

    def handle_prompt(self, prompt):
        self.broadcast_prompt(prompt)

    def broadcast_prompt(self, prompt):
        outbox_id = prompt.process_name
        assert outbox_id in self.outboxes, (outbox_id, self.outboxes.keys())
        self.outboxes[outbox_id].put(prompt)

    @staticmethod
    def is_prompt(event):
        return isinstance(event, Prompt)

    def close(self):
        super(PromptQueuedMultiThreadedRunner, self).close()

        if self.clock_event is not None:
            self.clock_event.set()

        if self.stop_clock_event is not None:
            self.stop_clock_event.set()

        for thread in self.threads.values():
            thread.inbox.put('QUIT')

        for thread in self.threads.values():
            thread.join(timeout=10)

        self.threads.clear()


class PromptQueuedApplicationThread(Thread):
    """
    Application thread which uses queues of prompts.

    It loops on an "inbox" queue of prompts, and
    adds its prompts to an "outbox" queue.
    """

    def __init__(self, process, poll_interval=DEFAULT_POLL_INTERVAL,
                 inbox=None, outbox=None, clock_event=None):
        super(PromptQueuedApplicationThread, self).__init__(daemon=True)
        self.process = process
        self.poll_interval = poll_interval
        self.inbox = inbox
        self.outbox = outbox
        self.clock_event = clock_event

    def run(self):
        self.loop_on_prompts()

    @retry(CausalDependencyFailed, max_attempts=100, wait=0.1)
    def loop_on_prompts(self):

        # Loop on getting prompts.
        while True:
            try:
                # Todo: Make the poll interval gradually increase if there are only timeouts?
                prompt = self.inbox.get(timeout=self.poll_interval)
                self.inbox.task_done()

                if prompt == 'QUIT':
                    self.process.close()
                    break

                else:
                    if self.clock_event is not None:
                        self.clock_event.wait()
                    started = time.time()
                    self.process.run(prompt)
                    if self.clock_event is not None:
                        ended = time.time()
                        duration = ended - started
                        if self.clock_event.is_set():
                            print(f"Warning: Process {self.process.name} overran clock cycle: {duration}")
                        else:
                            print(f"Info: Process {self.process.name} ran within clock cycle: {duration}")

            except six.moves.queue.Empty:
                # Basically, we're polling after a timeout.
                if self.clock_event is None:
                    self.process.run()


class PromptOutbox(object):
    """
    Has a collection of downstream prompt inboxes.

    """

    def __init__(self):
        self.downstream_inboxes = {}

    def put(self, prompt):
        """
        Puts prompt in each downstream inbox (an actual queue).
        """
        for queue in self.downstream_inboxes.values():
            queue.put(prompt)


class SteppingRunner(InProcessRunner):

    def __init__(self, normal_speed=1, scale_factor=1, is_verbose=False, *args, **kwargs):
        super(SteppingRunner, self).__init__(*args, **kwargs)
        self.normal_speed = normal_speed
        self.scale_factor = scale_factor
        self.is_verbose = is_verbose
        if scale_factor:
            self.tick_interval = 1 / (normal_speed * scale_factor)
        else:
            self.tick_interval = 0
        if self.is_verbose:
            print(f"Tick interval: {self.tick_interval:.6f}s")
        self.clock_thread = None

    def call_in_future(self, cmd, ticks_delay):
        self.clock_thread.call_in_future(cmd, ticks_delay)


class SteppingSingleThreadedRunner(SteppingRunner):

    def __init__(self, *args, **kwargs):
        super(SteppingSingleThreadedRunner, self).__init__(*args, **kwargs)
        self.seen_prompt_events = {}
        self.stop_event = Event()

    def start(self):
        super(SteppingSingleThreadedRunner, self).start()
        # for process_name in self.system.processes:
            # event = Event()
            # event.set()
            # self.seen_prompt_events[process_name] = event

        self.clock_thread = ProcessRunningClockThread(
            normal_speed=self.normal_speed,
            scale_factor=self.scale_factor,
            stop_event=self.stop_event,
            is_verbose=self.is_verbose,
            seen_prompt_events=self.seen_prompt_events,
            processes=self.system.processes
        )
        self.clock_thread.start()

    def handle_prompt(self, prompt):
        pass
        # self.seen_prompt_events[prompt.process_name].set()

    def close(self):
        super(SteppingSingleThreadedRunner, self).close()
        self.stop_event.set()
        tick_interval = 2 * max(1, self.tick_interval)
        self.clock_thread.join(timeout=tick_interval)
        if self.clock_thread.isAlive():
            print(f"Warning: clock thread was still alive")


class ClockThread(Thread):
    def __init__(self, *args, **kwargs):
        super(ClockThread, self).__init__()
        self.future_cmds = defaultdict(list)
        self.tick_count = 0

    def call_in_future(self, cmd, ticks_delay):
        assert ticks_delay > 0
        self.future_cmds[ticks_delay + self.tick_count].append(cmd)

    def call_commands(self):
        for cmd in self.future_cmds.get(self.tick_count, []):
            cmd()


class ProcessRunningClockThread(ClockThread):
    def __init__(self, normal_speed, scale_factor, stop_event: Event,
                 is_verbose=False, seen_prompt_events=None, processes=None):
        super(ProcessRunningClockThread, self).__init__(daemon=True)
        self.normal_speed = normal_speed
        self.scale_factor = scale_factor
        self.stop_event = stop_event
        self.seen_prompt_events = seen_prompt_events
        self.processes = processes
        self.last_tick_time = None
        self.last_process_time = None
        self.all_tick_durations = deque()
        self.tick_adjustment = 0.0
        self.is_verbose = is_verbose
        if normal_speed and scale_factor:
            self.tick_interval = 1 / normal_speed / scale_factor
        else:
            self.tick_interval = None
        if self.tick_interval:

            self.tick_durations_window_size = max(100, int(round(1 / self.tick_interval, 0)))
        else:
            self.tick_durations_window_size = 1000
        # Construct lists of followers for each process.
        self.followers = {}
        for process_name, process in self.processes.items():
            self.followers[process_name] = []
        for process_name, process in self.processes.items():
            for upstream_process_name in process.readers:
                self.followers[upstream_process_name].append(process_name)

        # Construct a notification log reader for each process.
        self.readers = {}
        for process_name, process in self.processes.items():
            reader = NotificationLogReader(
                notification_log=process.notification_log,
                use_direct_query_if_available=True
            )
            self.readers[process_name] = reader

    @property
    def actual_clock_speed(self):
        if self.all_tick_durations:
            durations = self.all_tick_durations
            return len(durations) / sum(durations)
        else:
            return 0

    def run(self):
        # Get new notifications once.

        while not self.stop_event.is_set():
            try:
                # Get all notifications.
                all_notifications = {}
                for process_name in self.processes:
                    # seen_prompt = self.seen_prompt_events[process_name]
                    # if seen_prompt.is_set():
                    #     seen_prompt.clear()
                    reader = self.readers[process_name]
                    notifications = reader.read_list()
                    all_notifications[process_name] = notifications

                # Process all notifications.
                all_events = {}
                for process_name, notifications in all_notifications.items():
                    events = []
                    for notification in notifications:
                        process = self.processes[process_name]
                        # It's not the follower process, but the method does the same thing.
                        event = process.get_event_from_notification(notification)
                        notification_id = notification['id']
                        events.append((notification_id, event))
                    all_events[process_name] = events

                for process_name, events in all_events.items():
                    # print(f"Process: {process_name}")
                    for follower_name in self.followers[process_name]:
                        follower_process = self.processes[follower_name]
                        # print(f"Follower: {follower_name}")
                        for notification_id, event in events:
                            # print(f"Notification: {notification_id}, {event}")
                            follower_process.process_upstream_event(event, notification_id, process_name)
                # Call commands delayed until this clock tick.
                self.call_commands()

            except:
                self.stop_event.set()
                raise
            else:
                tick_time = time.time()
                process_time = time.process_time()
                if self.last_tick_time is not None:
                    tick_duration = tick_time - self.last_tick_time
                    self.all_tick_durations.append(tick_duration)
                    if len(self.all_tick_durations) > self.tick_durations_window_size:
                        self.all_tick_durations.popleft()

                    if self.is_verbose:
                        process_duration = process_time - self.last_process_time
                        intensity = 100 * process_duration / tick_duration
                        clock_speed = 1 / tick_duration
                        real_time = self.tick_count / self.normal_speed
                        print(f"Tick {self.tick_count:4}: {real_time:4.2f}s  {tick_duration:.6f}s, "
                              f"{intensity:6.2f}%, {clock_speed:6.1f}Hz, "
                              f"{self.actual_clock_speed:6.1f}Hz, {self.tick_adjustment:.6f}s"
                              )

                    if self.tick_interval:
                        tick_oversize = tick_duration - self.tick_interval
                        tick_oversize_percentage = 100 * (tick_oversize) / self.tick_interval
                        # if tick_oversize_percentage > 300:
                        #     print(f"Warning: Tick over size: { tick_duration :.6f}s {tick_oversize_percentage:.2f}%")

                        if abs(tick_oversize_percentage) < 300:
                            # Weight falls from 1 as reciprocal of count, to tick interval.
                            # weight = max(1 / self.tick_count, min(.1, self.tick_interval))
                            weight = 1 / (1 + self.tick_count * self.tick_interval) ** 2
                            # print(f"Weight: {weight:.4f}")
                            self.tick_adjustment += weight * tick_oversize
                            max_tick_adjustment = 1.0 * self.tick_interval
                            min_tick_adjustment = 0
                            self.tick_adjustment = min(self.tick_adjustment, max_tick_adjustment)
                            self.tick_adjustment = max(self.tick_adjustment, min_tick_adjustment)

                self.last_tick_time = tick_time
                self.last_process_time = process_time
                self.tick_count += 1

                if self.tick_interval:
                    sleep_interval = self.tick_interval - self.tick_adjustment
                    sleep(max(sleep_interval, 0))


class SteppingMultiThreadedRunner(SteppingRunner):
    """
    Has a clock thread, and a thread for each application process
    in the system. The clock thread loops until stopped, waiting
    for a barrier, after sleeping for remaining tick interval timer.
    Application threads loop until stopped, waiting for the same
    barrier. Then, after all threads are waiting at the barrier,
    the barrier is lifted. The clock thread proceeds by sleeping
    for the clock tick interval. The application threads proceed by
    getting new notifications and processing all of them.

    There are actually two barriers, so that each application thread
    waits before getting notifications, and then waits for all processes
    to complete getting notification before processing the notifications
    through the application policy. This avoids events created by a process
    application "bleeding" into the notifications of another process
    application in the same clock cycle.


    Todo:
    Receive prompts, but set an event for the prompting process, to avoid unnecessary runs.

    Allow commands to be scheduled at future clock tick number, and execute when reached.

    """
    def __init__(self, *args, **kwargs):
        super(SteppingMultiThreadedRunner, self).__init__(*args, **kwargs)
        self.seen_prompt_events = {}
        self.fetch_barrier = None
        self.execute_barrier = None
        self.application_threads = {}
        self.clock_thread = None
        self.stop_event = Event()

    def handle_prompt(self, prompt):
        seen_prompt = self.seen_prompt_events[prompt.process_name]
        seen_prompt.set()

    def start(self):
        super(SteppingMultiThreadedRunner, self).start()
        parties = 1 + len(self.system.processes)
        self.fetch_barrier = Barrier(parties)
        self.execute_barrier = Barrier(parties)

        # Create an event for each process.
        for process_name in self.system.processes:
            self.seen_prompt_events[process_name] = Event()

        # Construct application threads.
        for process_name, process in self.system.processes.items():
            process_instance_id = process_name

            thread = BarrierControlledApplicationThread(
                process=process,
                fetch_barrier=self.fetch_barrier,
                execute_barrier=self.execute_barrier,
                stop_event=self.stop_event,
            )
            self.application_threads[process_instance_id] = thread

        # Start application threads.
        for thread in self.application_threads.values():
            thread.start()

        # Start clock thread.
        self.clock_thread = BarrierControllingClockThread(
            normal_speed=self.normal_speed,
            scale_factor=self.scale_factor,
            tick_interval=self.tick_interval,
            fetch_barrier=self.fetch_barrier,
            execute_barrier=self.execute_barrier,
            stop_event=self.stop_event,
            is_verbose=self.is_verbose,
        )
        self.clock_thread.start()

    def close(self):
        super(SteppingMultiThreadedRunner, self).close()
        self.stop_event.set()
        self.execute_barrier.abort()
        self.fetch_barrier.abort()

        for thread in self.application_threads.values():
            thread.join(timeout=1)
            if thread.isAlive():
                print(f"Warning: application thread '{thread.process.name}' was still alive: {thread.state}")

        self.application_threads.clear()

        self.clock_thread.join(timeout=1)
        if self.clock_thread.isAlive():
            print(f"Warning: clock thread was still alive")


class BarrierControlledApplicationThread(Thread):
    def __init__(self, process: ProcessApplication, fetch_barrier: Barrier,
                 execute_barrier: Barrier, stop_event: Event):
        super(BarrierControlledApplicationThread, self).__init__(daemon=True)
        self.process_application = process
        self.fetch_barrier = fetch_barrier
        self.execute_barrier = execute_barrier
        self.stop_event = stop_event

    def run(self):
        while not self.stop_event.is_set():
            # Isolate "fetch" and "execute" steps, to avoid
            # events created in one application being processed by
            # another application in the same tick. Race condition
            # where one process writes new events before another has
            # read all notifications from last tick. Actually, need
            # to get all notifications from all upstream applications
            # and then process the notifications. The run() method
            # gets notifications from one, then processes, then gets
            # from another, which makes the race condition probable.
            all_notifications = []
            try:
                self.fetch_barrier.wait()
            except BrokenBarrierError:
                self.abort()
            else:
                try:
                    # Get all notifications.
                    for upstream_name in self.process_application.readers:
                        notifications = list(self.process_application.read_reader(upstream_name))
                        all_notifications.append((upstream_name, notifications))

                except:
                    self.abort()
                    raise

            if self.stop_event.is_set():
                break

            try:
                self.execute_barrier.wait()
            except BrokenBarrierError:
                self.abort()
            else:
                try:
                    # Process all notifications.
                    for upstream_name, notifications in all_notifications:
                        for notification in notifications:
                            event = self.process_application.get_event_from_notification(notification)
                            self.process_application.process_upstream_event(event, notification['id'], upstream_name)
                except:
                    self.abort()
                    raise

            try:
                self.execute_barrier.wait()
            except BrokenBarrierError:
                self.abort()

    def abort(self):
        self.stop_event.set()
        self.fetch_barrier.abort()
        self.execute_barrier.abort()


class BarrierControllingClockThread(ClockThread):
    def __init__(self, normal_speed, scale_factor, tick_interval,
                 fetch_barrier: Barrier, execute_barrier: Barrier,
                 stop_event: Event, is_verbose=False):
        super(BarrierControllingClockThread, self).__init__(daemon=True)
        # Todo: Remove the redundancy here.
        self.normal_speed = normal_speed
        self.scale_factor = scale_factor
        self.tick_interval = tick_interval
        self.fetch_barrier = fetch_barrier
        self.execute_barrier = execute_barrier
        self.stop_event = stop_event
        self.last_tick_time = None
        self.last_process_time = None
        self.all_tick_durations = deque()
        self.tick_adjustment = 0.0
        self.is_verbose = is_verbose
        if self.tick_interval:

            self.tick_durations_window_size = max(1, int(round(1 / self.tick_interval, 0)))
        else:
            self.tick_durations_window_size = 100

    @property
    def actual_clock_speed(self):
        if self.all_tick_durations:
            durations = self.all_tick_durations
            return len(durations) / sum(durations)
        else:
            return 0

    def run(self):
        while not self.stop_event.is_set():

            try:
                self.fetch_barrier.wait()
                self.execute_barrier.wait()
                self.execute_barrier.wait()
                self.call_commands()
            except BrokenBarrierError:
                self.fetch_barrier.abort()
                self.execute_barrier.abort()
                self.stop_event.set()
            else:
                tick_time = time.time()
                process_time = time.process_time()
                if self.last_tick_time is not None:
                    tick_duration = tick_time - self.last_tick_time
                    self.all_tick_durations.append(tick_duration)
                    if len(self.all_tick_durations) > self.tick_durations_window_size:
                        self.all_tick_durations.popleft()

                    if self.is_verbose:
                        process_duration = process_time - self.last_process_time
                        intensity = 100 * process_duration / tick_duration
                        clock_speed = 1 / tick_duration
                        real_time = self.tick_count / self.normal_speed
                        print(f"Tick {self.tick_count:4}: {real_time:4.2f}s  {tick_duration:.6f}s, "
                              f"{intensity:6.2f}%, {clock_speed:6.1f}Hz, "
                              f"{self.actual_clock_speed:6.1f}Hz, {self.tick_adjustment:.6f}s"
                              )

                    if self.tick_interval:
                        tick_oversize = tick_duration - self.tick_interval
                        tick_oversize_percentage = 100 * (tick_oversize) / self.tick_interval
                        # if tick_oversize_percentage > 300:
                        #     print(f"Warning: Tick over size: { tick_duration :.6f}s {tick_oversize_percentage:.2f}%")

                        if abs(tick_oversize_percentage) < 300:
                            # Weight falls from 1 as reciprocal of count, to tick interval.
                            # weight = max(1 / self.tick_count, min(.1, self.tick_interval))
                            weight = 1 / (1 + self.tick_count * self.tick_interval) ** 2
                            # print(f"Weight: {weight:.4f}")
                            self.tick_adjustment += weight * tick_oversize
                            max_tick_adjustment = 1.0 * self.tick_interval
                            min_tick_adjustment = 0
                            self.tick_adjustment = min(self.tick_adjustment, max_tick_adjustment)
                            self.tick_adjustment = max(self.tick_adjustment, min_tick_adjustment)

                self.last_tick_time = tick_time
                self.last_process_time = process_time
                self.tick_count += 1

                if self.tick_interval:
                    sleep_interval = self.tick_interval - self.tick_adjustment
                    sleep(max(sleep_interval, 0))
