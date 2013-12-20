'''
run on arbiter
~~~~~~~~~~~~~~~~~~~~~~~~

.. autofunction:: run_on_arbiter


sequential
~~~~~~~~~~~~~~~~~~~~~~~~

.. autofunction:: sequential


ActorTestMixin
~~~~~~~~~~~~~~~~~~~~~~~~

.. autoclass:: ActorTestMixin
   :members:
   :member-order: bysource


AsyncAssert
~~~~~~~~~~~~~~~~~~~~~~~~

.. autoclass:: AsyncAssert
   :members:
   :member-order: bysource

run test server
~~~~~~~~~~~~~~~~~~~~~~

.. autofunction:: run_test_server
'''
import gc
from inspect import isclass
from functools import partial
from contextlib import contextmanager

import pulsar
from pulsar import (safe_async, get_actor, send, multi_async,
                    TcpServer, coroutine_return)
from pulsar.async.proxy import ActorProxyDeferred
from pulsar.utils.importer import module_attribute


__all__ = ['run_on_arbiter',
           'sequential',
           'NOT_TEST_METHODS',
           'ActorTestMixin',
           'AsyncAssert',
           'show_leaks',
           'hide_leaks',
           'run_test_server']


NOT_TEST_METHODS = ('setUp', 'tearDown', '_pre_setup', '_post_teardown',
                    'setUpClass', 'tearDownClass', 'run_test_server')


class TestCallable(object):

    def __init__(self, test, method_name, istest, timeout):
        self.test = test
        self.method_name = method_name
        self.istest = istest
        self.timeout = timeout

    def __repr__(self):
        if isclass(self.test):
            return '%s.%s' % (self.test.__name__, self.method_name)
        else:
            return '%s.%s' % (self.test.__class__.__name__, self.method_name)
    __str__ = __repr__

    def __call__(self, actor):
        test = self.test
        if self.istest:
            test = actor.app.runner.before_test_function_run(test)
        inject_async_assert(test)
        test_function = getattr(test, self.method_name)
        return safe_async(test_function).add_both(
            partial(self._end, actor)).set_timeout(self.timeout)

    def _end(self, actor, result):
        if self.istest:
            actor.app.runner.after_test_function_run(self.test, result)
        return result


class SafeTest(object):
    '''Make sure the test object or class is picklable
    '''
    def __init__(self, test):
        self.test = test

    def __getattr__(self, name):
        return getattr(self.test, name)

    def __getstate__(self):
        if isclass(self.test):
            cls = self.test
            data = None
        else:
            cls = self.test.__class__
            data = self.test.__dict__.copy()
        return ('%s.%s' % (cls.__module__, cls.__name__), data)

    def __setstate__(self, state):
        mod, data = state
        test = module_attribute(mod)
        inject_async_assert(test)
        if data is not None:
            test = test.__new__(test)
            test.__dict__.update(data)
        self.test = test


class TestFunction(object):

    def __init__(self, method_name):
        self.method_name = method_name
        self.istest = self.method_name not in NOT_TEST_METHODS

    def __repr__(self):
        return self.method_name
    __str__ = __repr__

    def __call__(self, test, timeout):
        callable = TestCallable(test, self.method_name, self.istest, timeout)
        return callable(get_actor())


class TestFunctionOnArbiter(TestFunction):

    def __call__(self, test, timeout):
        test = SafeTest(test)
        callable = TestCallable(test, self.method_name, self.istest, timeout)
        actor = get_actor()
        if actor.is_monitor():
            return callable(actor)
        else:
            # send the callable to the actor monitor
            return actor.send(actor.monitor, 'run', callable)


def run_on_arbiter(f):
    '''Decorator for running a test function in the :class:`.Arbiter`
    context domain.

    This can be useful to test Arbiter mechanics.
    '''
    f.testfunction = TestFunctionOnArbiter(f.__name__)
    return f


def sequential(cls):
    '''Decorator for a :class:`unittest.TestCase` which cause
    its test functions to run sequentially rather than in an
    asynchronous fashion.
    '''
    cls._sequential_execution = True
    return cls


class AsyncAssert(object):
    '''A `descriptor`_ added by the :ref:`test-suite` to all python
    :class:`unittest.TestCase` loaded.

    It can be used to invoke the same ``assertXXX`` methods available in
    the :class:`unittest.TestCase` in an asynchronous fashion.

    The descriptor is available via the ``async`` attribute.
    For example::

        class MyTest(unittest.TestCase):

            def test1(self):
                yield self.async.assertEqual(3, Deferred().callback(3))
                ...


    .. _descriptor: http://users.rcn.com/python/download/Descriptor.htm
    '''
    def __init__(self, test=None):
        self.test = test

    def __get__(self, instance, instance_type=None):
        return AsyncAssert(instance)

    def __getattr__(self, name):
        def _(*args, **kwargs):
            __skip_traceback__ = True
            args = yield multi_async(args)
            result = yield getattr(self.test, name)(*args, **kwargs)
            coroutine_return(result)
        return _

    def assertRaises(self, error, callable, *args, **kwargs):
        try:
            yield callable(*args, **kwargs)
        except error:
            coroutine_return(None)
        except Exception:
            raise self.test.failureException('%s not raised by %s'
                                             % (error, callable))
        else:
            raise self.test.failureException('%s not raised by %s'
                                             % (error, callable))


class ActorTestMixin(object):
    '''A mixin for :class:`unittest.TestCase`.

    Useful for classes testing spawning of actors.
    Make sure this is the first class you derive from, before the
    unittest.TestCase, so that the tearDown method is overwritten.

    .. attribute:: concurrency

        The concurrency model used to spawn actors via the :meth:`spawn`
        method.
    '''
    concurrency = 'thread'

    @property
    def all_spawned(self):
        if not hasattr(self, '_spawned'):
            self._spawned = []
        return self._spawned

    def spawn_actor(self, concurrency=None, **kwargs):
        '''Spawn a new actor and perform some tests.'''
        concurrency = concurrency or self.concurrency
        ad = pulsar.spawn(concurrency=concurrency, **kwargs)
        self.assertTrue(ad.aid)
        self.assertTrue(isinstance(ad, ActorProxyDeferred))
        proxy = yield ad
        self.all_spawned.append(proxy)
        self.assertEqual(proxy.aid, ad.aid)
        self.assertEqual(proxy.proxy, proxy)
        self.assertTrue(proxy.cfg)
        coroutine_return(proxy)

    def stop_actors(self, *args):
        all = args or self.all_spawned
        if len(all) == 1:
            return send(all[0], 'stop')
        elif all:
            return multi_async((send(a, 'stop') for a in all))

    def tearDown(self):
        return self.stop_actors()


def inject_async_assert(obj):
    tcls = obj if isclass(obj) else obj.__class__
    if not hasattr(tcls, 'async'):
        tcls.async = AsyncAssert()


def show_leaks(actor, show=True):
    '''Function to show memory leaks on a processed-based actor.'''
    if not actor.is_process():
        return
    gc.collect()
    if gc.garbage:
        MAX_SHOW = 100
        write = actor.stream.writeln if show else lambda msg: None
        write('MEMORY LEAKS REPORT IN %s' % actor)
        write('Created %s uncollectable objects' % len(gc.garbage))
        for obj in gc.garbage[:MAX_SHOW]:
            write('Type: %s' % type(obj))
            write('=================================================')
            write('%s' % obj)
            write('-------------------------------------------------')
            write('')
            write('')
        if len(gc.garbage) > MAX_SHOW:
            write('And %d more' % (len(gc.garbage) - MAX_SHOW))


def hide_leaks(actor):
    show_leaks(actor, False)


@contextmanager
def run_test_server(protocol_factory, loop, address=None, **kw):
    '''A context manager for running a test server::

        with run_test_server(loop, protocol_factory) as server:
            ...

    It creates a :class:`.TcpServer` and invoke
    :meth:`~.TcpServer.stop_serving` on exit.
    '''
    address = address or ('127.0.0.1', 0)
    server = TcpServer(protocol_factory, loop, address, **kw)
    try:
        yield server
    finally:
        server.stop_serving()
