import os
import sys
import socket
import errno
from types import GeneratorType
from heapq import heappop, heappush
from collections import deque
from threading import current_thread, Lock
try:
    import signal
except ImportError:     # pragma    nocover
    signal = None

from pulsar.utils.system import close_on_exec
from pulsar.utils.pep import range
from pulsar.utils.exceptions import StopEventLoop, ImproperlyConfigured

from .access import asyncio, thread_data, LOGGER
from .defer import maybe_async, async, DeferredTask, Failure
from .stream import (create_connection, start_serving, sock_connect,
                     raise_socket_error)
from .udp import create_datagram_endpoint
from .consts import DEFAULT_CONNECT_TIMEOUT
from .pollers import DefaultIO

__all__ = ['EventLoop']


def file_descriptor(fd):
    if hasattr(fd, 'fileno'):
        return fd.fileno()
    else:
        return fd


def setid(self):
    ct = current_thread()
    self.tid = ct.ident
    self.pid = os.getpid()
    return ct


class EventLoopPolicy(asyncio.AbstractEventLoopPolicy):
    '''Pulsar event loop policy'''
    def get_event_loop(self):
        return thread_data('_event_loop')

    def get_request_loop(self):
        return thread_data('_request_loop') or self.get_event_loop()

    def new_event_loop(self):
        return EventLoop()

    def set_event_loop(self, event_loop):
        """Set the event loop."""
        assert event_loop is None or isinstance(event_loop,
                                                asyncio.AbstractEventLoop)
        if getattr(event_loop, 'cpubound', False):
            thread_data('_request_loop', event_loop)
        else:
            thread_data('_event_loop', event_loop)


asyncio.set_event_loop_policy(EventLoopPolicy())

if not getattr(asyncio, 'fallback', False):
    from asyncio.base_events import BaseEventLoop
else:   # pragma    nocover
    BaseEventLoop = asyncio.BaseEventLoop

Handle = asyncio.Handle
TimerHandle = asyncio.TimerHandle


class LoopingCall(object):

    def __init__(self, loop, callback, args, interval=None):
        self._loop = loop
        self.callback = callback
        self.args = args
        self._cancelled = False
        interval = interval or 0
        if interval > 0:
            self.interval = interval
            self.handler = self._loop.call_later(interval, self)
        else:
            self.interval = None
            self.handler = self._loop.call_soon(self)

    @property
    def cancelled(self):
        return self._cancelled

    def cancel(self, result=None):
        '''Attempt to cancel the callback.'''
        self._cancelled = True

    def __call__(self):
        try:
            result = maybe_async(self.callback(*self.args), self._loop,
                                 get_result=False)
        except Exception:
            self.cancel()
            exc_info = sys.exc_info()
        else:
            result.add_callback(self._continue, self.cancel)
            return
        Failure(exc_info).log(msg='Exception in looping callback')

    def _continue(self, result):
        if not self._cancelled:
            handler = self.handler
            loop = self._loop
            if self.interval:
                handler._cancelled = False
                handler._when = loop.time() + self.interval
                loop._add_callback(handler)
            else:
                loop._ready.append(self.handler)


class EventLoop(BaseEventLoop):
    """A pluggable event loop which conforms with the pep-3156_ API.

    The event loop is the place where most asynchronous operations
    are carried out.

    .. attribute:: poll_timeout

        The timeout in seconds when polling with ``epolL``, ``kqueue``,
        ``select`` and so forth.

        Default: ``0.5``

    .. attribute:: tid

        The thread id where this event loop is running. If the
        event loop is not running this attribute is ``None``.

    """
    poll_timeout = 0.5
    tid = None
    pid = None
    exit_signal = None
    task_factory = DeferredTask

    def __init__(self):
        self.clear()

    def setup_loop(self, io=None, logger=None, poll_timeout=None,
                   iothreadloop=False, noisy=False):
        self._io = io or DefaultIO()
        self._signal_handlers = {}
        self.poll_timeout = poll_timeout if poll_timeout else self.poll_timeout
        self.noisy = noisy
        self.logger = logger or LOGGER
        close_on_exec(self._io.fileno())
        self._iothreadloop = iothreadloop
        self._name = None
        self._num_loops = 0
        self._default_executor = None
        self._waker = self._io.install_waker(self)
        self._lock = Lock()

    def __repr__(self):
        return self.name
    __str__ = __repr__

    @property
    def name(self):
        name = self._name if self._name else '<not running>'
        cpu = 'CPU bound ' if self.cpubound else ''
        return '%s%s %s' % (cpu, name, self.logger.name)

    @property
    def io(self):
        '''The :class:`Poller` for this event loop. If not supplied,
        the best possible implementation available will be used. On posix
        system this is ``epoll`` or ``kqueue`` (Mac OS)
        or else ``select``.'''
        return self._io

    @property
    def iothreadloop(self):
        '''``True`` if this :class:`EventLoop` install itself as the event
loop of the thread where it is run.'''
        return self._iothreadloop

    @property
    def cpubound(self):
        '''If ``True`` this is a CPU bound event loop, otherwise it is an I/O
        event loop. CPU bound loops can block the loop for considerable amount
        of time.'''
        return getattr(self._io, 'cpubound', False)

    @property
    def running(self):
        return bool(self._name)

    @property
    def active(self):
        return bool(self._ready or self._scheduled)

    @property
    def num_loops(self):
        '''Total number of loops.'''
        return self._num_loops

    #################################################    STARTING & STOPPING
    def run(self):
        '''Run the event loop until nothing left to do or stop() called.'''
        if not self.running:
            self._before_run()
            try:
                while self.active:
                    try:
                        self._run_once()
                    except StopEventLoop:
                        break
            finally:
                self._after_run()

    def run_forever(self):
        '''Run the event loop forever.'''
        if not self.running:
            self._before_run()
            try:
                while True:
                    try:
                        self._run_once()
                    except StopEventLoop:
                        break
            finally:
                self._after_run()

    def run_until_complete(self, future):
        """Run until the Future is done.

        If the argument is a coroutine, it is wrapped in a Task.

        XXX TBD: It would be disastrous to call run_until_complete()
        with the same coroutine twice -- it would wrap it in two
        different Tasks and that can't be good.

        Return the Future's result, or raise its exception.
        """
        future = async(future, self)
        future.add_done_callback(self.stop)
        self.run_forever()
        future.remove_done_callback(self.stop)
        if not future.done():
            raise RuntimeError('Event loop stopped before Future completed.')
        return future.result()

    def stop(self, *args):
        '''Stop the loop after the current event loop iteration is complete'''
        self.call_soon_threadsafe(self._raise_stop_event_loop)

    def is_running(self):
        '''``True`` if the loop is running.'''
        return bool(self._name)

    def run_in_executor(self, executor, callback, *args):
        '''Arrange to call ``callback(*args)`` in an ``executor``.

        Return a :class:`.Deferred` called once the callback has finished.'''
        executor = executor or self._default_executor
        if executor is None:
            raise ImproperlyConfigured('No executor available')
        return executor.apply(callback, *args)

    def call_later(self, delay, callback, *args):
        """Arrange for a callback to be called at a given time.

        Return a Handle: an opaque object with a cancel() method that
        can be used to cancel the call.

        The delay can be an int or float, expressed in seconds.  It is
        always a relative time.

        Each callback will be called exactly once.  If two callbacks
        are scheduled for exactly the same time, it undefined which
        will be called first.

        Any positional arguments after the callback will be passed to
        the callback when it is called.
        """
        return self.call_at(self.time() + delay, callback, *args)

    def call_at(self, when, callback, *args):
        '''Like call_later(), but uses an absolute time.

        This method is thread safe.
        '''
        timer = TimerHandle(when, callback, args)
        with self._lock:
            heappush(self._scheduled, timer)
        return timer

    #################################################    INTERNET NAME LOOKUPS
    def getaddrinfo(self, host, port, family=0, type=0, proto=0, flags=0):
        return socket.getaddrinfo(host, port, family, type, proto, flags)

    def getnameinfo(self, sockaddr, flags=0):
        return socket.getnameinfo(sockaddr, flags)

    #################################################    I/O CALLBACKS
    def add_reader(self, fd, callback, *args):
        """Add a reader callback.  Return a Handler instance."""
        handler = Handle(callback, args)
        self._io.add_reader(file_descriptor(fd), handler)
        return handler

    def add_writer(self, fd, callback, *args):
        """Add a reader callback.  Return a Handler instance."""
        handler = Handle(callback, args)
        self._io.add_writer(file_descriptor(fd), handler)
        return handler

    def add_connector(self, fd, callback, *args):
        '''Add a connector callback. Return a Handler instance.'''
        handler = Handle(callback, args)
        fd = file_descriptor(fd)
        self._io.add_writer(fd, handler)
        self._io.add_error(fd, handler)
        return handler

    def remove_reader(self, fd):
        '''Cancels the current read handler for file descriptor ``fd``.

        A no-op if no callback is currently set for the file descriptor.'''
        return self._io.remove_reader(file_descriptor(fd))

    def remove_writer(self, fd):
        '''Cancels the current write callback for file descriptor ``fd``.

        A no-op if no callback is currently set for the file descriptor.
        '''
        return self._io.remove_writer(file_descriptor(fd))

    def remove_connector(self, fd):
        fd = file_descriptor(fd)
        w = self._io.remove_writer(fd)
        e = self._io.remove_error(fd)
        return w or e

    #################################################    SIGNAL CALLBACKS
    def add_signal_handler(self, sig, callback, *args):
        '''Add a signal handler.

        Whenever signal ``sig`` is received, arrange for `callback(*args)` to
        be called. Returns an ``asyncio Handle`` which can be used to
        cancel the signal callback.
        '''
        self._check_signal(sig)
        handle = Handle(callback, args)
        self._signal_handlers[sig] = handle
        try:
            signal.signal(sig, self._handle_signal)
        except OSError as exc:
            del self._signal_handlers[sig]
            if not self._signal_handlers:
                try:
                    signal.set_wakeup_fd(-1)
                except ValueError as nexc:
                    self.logger.info('set_wakeup_fd(-1) failed: %s', nexc)
            if exc.errno == errno.EINVAL:
                raise RuntimeError('sig {} cannot be caught'.format(sig))
            else:
                raise

    def remove_signal_handler(self, sig):
        '''Remove the signal ``sig`` if it was installed and reinstal the
default signal handler ``signal.SIG_DFL``.'''
        self._check_signal(sig)
        try:
            del self._signal_handlers[sig]
        except KeyError:
            return False

        if sig == signal.SIGINT:
            handler = signal.default_int_handler
        else:
            handler = signal.SIG_DFL

        try:
            signal.signal(sig, handler)
        except OSError as exc:
            if exc.errno == errno.EINVAL:
                raise RuntimeError('sig {} cannot be caught'.format(sig))
            else:
                raise

        if not self._signal_handlers:
            try:
                signal.set_wakeup_fd(-1)
            except ValueError as exc:
                self.logger.info('set_wakeup_fd(-1) failed: %s', exc)

        return True

    def _handle_signal(self, sig, frame):
        """Internal helper that is the actual signal handler."""
        handle = self._signal_handlers.get(sig)
        if handle is None:
            return  # Assume it's some race condition.
        if handle._cancelled:
            self.remove_signal_handler(sig)  # Remove it properly.
        else:
            handle._callback(*handle._args)

    #################################################    SOCKET METHODS
    def create_connection(self, protocol_factory, host=None, port=None,
                          ssl=None, family=0, proto=0, flags=0, sock=None,
                          local_addr=None, timeout=None):
        '''Creates a stream connection to a given internet host and port.

        It is the asynchronous equivalent of ``socket.create_connection``.

        :param protocol_factory: The callable to create the
            :class:`Protocol` which handle the connection.
        :param host: If host is an empty string or None all interfaces are
            assumed and a list of multiple sockets will be returned (most
            likely one for IPv4 and another one for IPv6)
        :param port:
        :param ssl:
        :param family:
        :param proto:
        :param flags:
        :param sock:
        :param local_addr: if supplied, it must be a 2-tuple
            ``(host, port)`` for the socket to bind to as its source address
            before connecting.
        :return: a :class:`.Deferred` and its result on success is the
            ``(transport, protocol)`` pair.

        If a failure prevents the creation of a successful connection, an
        appropriate exception will be raised.
        '''
        timeout = timeout or DEFAULT_CONNECT_TIMEOUT
        res = create_connection(self, protocol_factory, host, port,
                                ssl, family, proto, flags, sock, local_addr)
        return async(res, self).set_timeout(timeout)

    def create_server(self, protocol_factory, host=None, port=None, ssl=None,
                      family=socket.AF_UNSPEC, flags=socket.AI_PASSIVE,
                      sock=None, backlog=100, reuse_address=None):
        """Creates a TCP server bound to ``host`` and ``port``.

        :param protocol_factory: The :class:`Protocol` which handle server
            requests.
        :param host: If host is an empty string or None all interfaces are
            assumed and a list of multiple sockets will be returned (most
            likely one for IPv4 and another one for IPv6).
        :param port: integer indicating the port number.
        :param ssl: can be set to an SSLContext to enable SSL over
            the accepted connections.
        :param family: socket family can be set to either ``AF_INET`` or
            ``AF_INET6`` to force the socket to use IPv4 or IPv6.
            If not set it will be determined from host (defaults to
            ``AF_UNSPEC``).
        :param flags: is a bitmask for :meth:`getaddrinfo`.
        :param sock: can optionally be specified in order to use a
            pre-existing socket object.
        :param backlog: is the maximum number of queued connections
            passed to listen() (defaults to 100).
        :param reuse_address: tells the kernel to reuse a local socket in
            ``TIME_WAIT`` state, without waiting for its natural timeout to
            expire. If not specified will automatically be set to ``True``
            on UNIX.
        :return: a :class:`.Deferred` whose result will be a list of socket
            objects which will later be handled by ``protocol_factory``.
        """
        res = start_serving(self, protocol_factory, host, port, ssl,
                            family, flags, sock, backlog, reuse_address)
        return async(res, self)

    def create_datagram_endpoint(self, protocol_factory, local_addr=None,
                                 remote_addr=None, family=socket.AF_UNSPEC,
                                 proto=0, flags=0):
        res = create_datagram_endpoint(self, protocol_factory, local_addr,
                                       remote_addr, family, proto, flags)
        return async(res, self)

    def sock_connect(self, sock, address):
        '''Connect ``sock`` to the given ``address``.

        Returns a :class:`.Deferred` whose result on success will be ``None``.
        '''
        return sock_connect(self, sock, address)

    #################################################    NON PEP METHODS
    def clear(self):
        self._ready = deque()
        self._scheduled = []

    def _write_to_self(self):
        if self.running and self._waker:
            self._waker.wake()

    def call_repeatedly(self, interval, callback, *args):
        """Call a ``callback`` every ``interval`` seconds. It handles
asynchronous results. If an error occur in the ``callback``, the chain is
broken and the ``callback`` won't be called anymore."""
        return LoopingCall(self, callback, args, interval)

    def call_every(self, callback, *args):
        '''Same as :meth:`call_repeatedly` with the only difference that
the ``callback`` is scheduled at every loop. Installing this callback cause
the event loop to poll with a 0 timeout all the times.'''
        return LoopingCall(self, callback, args)

    #################################################    INTERNALS
    def _before_run(self):
        ct = setid(self)
        self._name = ct.name
        if self._iothreadloop:
            asyncio.set_event_loop(self)

    def _after_run(self):
        self._name = None
        self.tid = None

    def _raise_stop_event_loop(self, exc=None):
        if self.is_running():
            raise StopEventLoop

    def _check_signal(self, sig):
        """Internal helper to validate a signal.

        Raise ValueError if the signal number is invalid or uncatchable.
        Raise RuntimeError if there is a problem setting up the handler.
        """
        if signal is None:  # pragma    nocover
            raise RuntimeError('Signals are not supported')
        if not isinstance(sig, int):
            raise TypeError('sig must be an int, not {!r}'.format(sig))
        if not (1 <= sig < signal.NSIG):
            raise ValueError('sig {} out of range(1, {})'.format(sig,
                                                                 signal.NSIG))

    def _run_once(self, timeout=None):
        timeout = timeout or self.poll_timeout
        self._num_loops += 1
        #
        # Compute the desired timeout
        if self._ready:
            timeout = 0
        elif self._scheduled:
            timeout = min(max(0, self._scheduled[0]._when - self.time()),
                          timeout)
        # poll events
        self._poll(timeout)
        #
        # append scheduled callback
        now = self.time()
        while self._scheduled and self._scheduled[0]._when <= now:
            self._ready.append(heappop(self._scheduled))
        #
        # Run callbacks
        callbacks = self._ready
        todo = len(callbacks)
        for i in range(todo):
            exc_info = None
            handle = callbacks.popleft()
            try:
                if not handle._cancelled:
                    value = handle._callback(*handle._args)
                    if isinstance(value, GeneratorType):
                        async(value, self)
            except socket.error as e:
                if raise_socket_error(e) and self.running:
                    exc_info = sys.exc_info()
            except Exception:
                exc_info = sys.exc_info()
            if exc_info:
                Failure(exc_info).log(
                    msg='Unhandled exception in event loop callback.')

    def _poll(self, timeout):
        io = self._io
        try:
            event_pairs = io.poll(timeout)
        except Exception as e:
            if raise_socket_error(e) and self.running:
                raise
        except KeyboardInterrupt:
            raise StopEventLoop
        else:
            for fd, events in event_pairs:
                try:
                    io.handle_events(self, fd, events)
                except KeyError:
                    pass

    def _add_callback(self, handle):
        """Add a Handle to ready or scheduled."""
        assert isinstance(handle, Handle), 'A Handle is required here'
        if handle._cancelled:
            return
        if isinstance(handle, TimerHandle):
            with self._lock:
                heappush(self._scheduled, handle)
        else:
            self._ready.append(handle)
