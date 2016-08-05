###############################################################################
#
# The MIT License (MIT)
#
# Copyright (c) Tavendo GmbH
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#
###############################################################################


from __future__ import absolute_import, print_function

import itertools
from functools import partial

from twisted.internet.defer import inlineCallbacks
from twisted.internet.interfaces import IStreamClientEndpoint
from twisted.internet.endpoints import UNIXClientEndpoint
from twisted.internet.endpoints import TCP4ClientEndpoint
from twisted.internet.error import ReactorNotRunning
from twisted.internet.task import react

try:
    _TLS = True
    from twisted.internet.endpoints import SSL4ClientEndpoint
    from twisted.internet.ssl import optionsForClientTLS, CertificateOptions
    from twisted.internet.interfaces import IOpenSSLClientConnectionCreator
    from OpenSSL import SSL
except ImportError as e:
    _TLS = False
    if 'OpenSSL' not in str(e):
        raise

import txaio

from autobahn.twisted.websocket import WampWebSocketClientFactory
from autobahn.twisted.rawsocket import WampRawSocketClientFactory

from autobahn.wamp import component

from autobahn.twisted.util import sleep
from autobahn.twisted.wamp import ApplicationSession
from autobahn.wamp.exception import ApplicationError


__all__ = ('Component')


def _is_ssl_error(e):
    """
    Internal helper.

    This is so we can just return False if we didn't import any
    TLS/SSL libraries. Otherwise, returns True if this is an
    OpenSSL.SSL.Error
    """
    if _TLS:
        return isinstance(e, SSL.Error)
    return False


def _unique_list(seq):
    """
    Return a list with unique elements from sequence, preserving order.
    """
    seen = set()
    return [x for x in seq if x not in seen and not seen.add(x)]


def _create_transport_serializer(serializer_id):
    if serializer_id in [u'msgpack', u'mgspack.batched']:
        # try MsgPack WAMP serializer
        try:
            from autobahn.wamp.serializer import MsgPackSerializer
        except ImportError:
            pass
        else:
            if serializer_id == u'mgspack.batched':
                return MsgPackSerializer(batched=True)
            else:
                return MsgPackSerializer()

    if serializer_id in [u'json', u'json.batched']:
        # try JSON WAMP serializer
        try:
            from autobahn.wamp.serializer import JsonSerializer
        except ImportError:
            pass
        else:
            if serializer_id == u'json.batched':
                return JsonSerializer(batched=True)
            else:
                return JsonSerializer()

    raise RuntimeError('could not create serializer for "{}"'.format(serializer_id))


def _create_transport_serializers(transport_config):
    """
    Create a list of serializers to use with a WAMP protocol factory.
    """
    if u'serializers' in transport_config:
        serializer_ids = _unique_list(transport_config[u'serializers'])
    else:
        serializer_ids = [u'msgpack', u'json']

    serializers = []

    for serializer_id in serializer_ids:
        if serializer_id == u'msgpack':
            # try MsgPack WAMP serializer
            try:
                from autobahn.wamp.serializer import MsgPackSerializer
            except ImportError:
                pass
            else:
                serializers.append(MsgPackSerializer(batched=True))
                serializers.append(MsgPackSerializer())

        elif serializer_id == u'json':
            # try JSON WAMP serializer
            try:
                from autobahn.wamp.serializer import JsonSerializer
            except ImportError:
                pass
            else:
                serializers.append(JsonSerializer(batched=True))
                serializers.append(JsonSerializer())

        else:
            raise RuntimeError(
                "Unknown serializer '{}'".format(serializer_id)
            )

    return serializers


def _create_transport_factory(reactor, transport_config, session_factory):
    """
    Create a WAMP-over-XXX transport factory.
    """
    if transport_config['type'] == 'websocket':
        # FIXME: forward WebSocket options
        serializers = _create_transport_serializers(transport_config)
        return WampWebSocketClientFactory(session_factory, url=transport_config['url'], serializers=serializers)

    elif transport_config['type'] == 'rawsocket':
        # FIXME: forward RawSocket options
        serializer = _create_transport_serializer(transport_config.get('serializer', u'json'))
        return WampRawSocketClientFactory(session_factory, serializer=serializer)

    else:
        assert(False), 'should not arrive here'


def _create_transport_endpoint(reactor, endpoint_config):
    """
    Create a Twisted client endpoint for a WAMP-over-XXX transport.
    """
    if IStreamClientEndpoint.providedBy(endpoint_config):
        endpoint = IStreamClientEndpoint(endpoint_config)
    else:
        # create a connecting TCP socket
        if endpoint_config['type'] == 'tcp':

            version = int(endpoint_config.get('version', 4))
            host = str(endpoint_config['host'])
            port = int(endpoint_config['port'])
            timeout = int(endpoint_config.get('timeout', 10))  # in seconds
            tls = endpoint_config.get('tls', None)

            # create a TLS enabled connecting TCP socket
            if tls:
                if not _TLS:
                    raise RuntimeError('TLS configured in transport, but TLS support is not installed (eg OpenSSL?)')

                # FIXME: create TLS context from configuration
                if IOpenSSLClientConnectionCreator.providedBy(tls):
                    # eg created from twisted.internet.ssl.optionsForClientTLS()
                    context = IOpenSSLClientConnectionCreator(tls)

                elif isinstance(tls, CertificateOptions):
                    context = tls

                elif tls is True:
                    context = optionsForClientTLS(host)

                else:
                    raise RuntimeError('unknown type {} for "tls" configuration in transport'.format(type(tls)))

                if version == 4:
                    endpoint = SSL4ClientEndpoint(reactor, host, port, context, timeout=timeout)
                elif version == 6:
                    # there is no SSL6ClientEndpoint!
                    raise RuntimeError('TLS on IPv6 not implemented')
                else:
                    assert(False), 'should not arrive here'

            # create a non-TLS connecting TCP socket
            else:
                if version == 4:
                    endpoint = TCP4ClientEndpoint(reactor, host, port, timeout=timeout)
                elif version == 6:
                    try:
                        from twisted.internet.endpoints import TCP6ClientEndpoint
                    except ImportError:
                        raise RuntimeError('IPv6 is not supported (please upgrade Twisted)')
                    endpoint = TCP6ClientEndpoint(reactor, host, port, timeout=timeout)
                else:
                    assert(False), 'should not arrive here'

        # create a connecting Unix domain socket
        elif endpoint_config['type'] == 'unix':
            path = endpoint_config['path']
            timeout = int(endpoint_config.get('timeout', 10))  # in seconds
            endpoint = UNIXClientEndpoint(reactor, path, timeout=timeout)

        else:
            assert(False), 'should not arrive here'

    return endpoint


class Component(component.Component):
    """
    A component establishes a transport and attached a session
    to a realm using the transport for communication.

    The transports a component tries to use can be configured,
    as well as the auto-reconnect strategy.
    """

    log = txaio.make_logger()

    session_factory = ApplicationSession
    """
    The factory of the session we will instantiate.
    """

    def _check_native_endpoint(self, endpoint):
        if IStreamClientEndpoint.providedBy(endpoint):
            pass
        elif isinstance(endpoint, dict):
            if 'tls' in endpoint:
                tls = endpoint['tls']
                if isinstance(tls, (dict, bool)):
                    pass
                elif IOpenSSLClientConnectionCreator.providedBy(tls):
                    pass
                elif isinstance(tls, CertificateOptions):
                    pass
                else:
                    raise ValueError(
                        "'tls' configuration must be a dict, CertificateOptions or"
                        " IOpenSSLClientConnectionCreator provider"
                    )
        else:
            raise ValueError(
                "'endpoint' configuration must be a dict or IStreamClientEndpoint"
                " provider"
            )

    def _connect_transport(self, reactor, transport_config, session_factory):
        """
        Create and connect a WAMP-over-XXX transport.
        """
        transport_factory = _create_transport_factory(reactor, transport_config, session_factory)
        transport_endpoint = _create_transport_endpoint(reactor, transport_config['endpoint'])
        return transport_endpoint.connect(transport_factory)


    # XXX think: is it okay to use inlineCallbacks (in this
    # twisted-only file) even though we're using txaio?
    @inlineCallbacks
    def start(self, reactor):
        """
        This starts the Component, which means it will start connecting
        (and re-connecting) to its configured transports. A Component
        runs until it is "done", which means one of:

        - There was a "main" function defined, and it completed successfully;
        - Something called ``.leave()`` on our session, and we left successfully;
        - ``.stop()`` was called, and completed successfully;
        - none of our transports were able to connect successfully (failure);

        :returns: a Deferred that fires (with ``None``) when we are
            "done" or with a Failure if something went wrong.
        """
        if reactor is None:
            self.log.warn("Using default reactor")
            from twisted.internet import reactor

        yield self.fire('start', reactor, self)

        # transports to try again and again ..
        transport_gen = itertools.cycle(self._transports)

        reconnect = True

        self.log.debug('Entering re-connect loop')

        while reconnect:
            # cycle through all transports forever ..
            transport = next(transport_gen)

            # only actually try to connect using the transport,
            # if the transport hasn't reached max. connect count
            if transport.can_reconnect():
                delay = transport.next_delay()
                self.log.debug(
                    'trying transport {transport_idx} using connect delay {transport_delay}',
                    transport_idx=transport.idx,
                    transport_delay=delay,
                )
                yield sleep(delay)
                try:
                    yield self._connect_once(reactor, transport)
                    self.log.info('Component completed successfully')

                except Exception as e:
                    # need concept of "fatal errors", for which a
                    # connection is *never* going to work. Might want
                    # to also add, for example, things like
                    # SyntaxError
                    if isinstance(e, ApplicationError):
                        if e.error in [u'wamp.error.no_such_realm']:
                            reconnect = False
                            self.log.error(u"Fatal error, not reconnecting")
                            raise
                        # self.log.error(u"{error}: {message}", error=e.error, message=e.message)
                    elif _is_ssl_error(e):
                        # Quoting pyOpenSSL docs: "Whenever
                        # [SSL.Error] is raised directly, it has a
                        # list of error messages from the OpenSSL
                        # error queue, where each item is a tuple
                        # (lib, function, reason). Here lib, function
                        # and reason are all strings, describing where
                        # and what the problem is. See err(3) for more
                        # information."
                        for (lib, fn, reason) in e.args[0]:
                            self.log.error(u"TLS failure: {reason}", reason=reason)
                            self.log.error(u"Marking this transport as failed")
                            transport.failed()
                    else:
                        f = txaio.create_failure()
                        self.log.error(u'Connection failed: {error}', error=txaio.failure_message(f))
                        # some types of errors should probably have
                        # stacktraces logged immediately at error
                        # level, e.g. SyntaxError?
                        self.log.debug(u'{tb}', tb=txaio.failure_format_traceback(f))
                        raise
                else:
                    reconnect = False
            else:
                # check if there is any transport left we can use
                # to connect
                if not self._can_reconnect():
                    self.log.info("No remaining transports to try")
                    reconnect = False


def _run(reactor, components):
    if isinstance(components, Component):
        components = [components]

    if type(components) != list:
        raise ValueError(
            '"components" must be a list of Component objects - encountered'
            ' {0}'.format(type(components))
        )

    for c in components:
        if not isinstance(c, Component):
            raise ValueError(
                '"components" must be a list of Component objects - encountered'
                'item of type {0}'.format(type(c))
            )

    log = txaio.make_logger()

    def component_success(c, arg):
        log.debug("Component {c} successfully completed: {arg}", c=c, arg=arg)
        return arg

    def component_failure(f):
        log.error("Component error: {msg}", msg=txaio.failure_message(f))
        log.debug("Component error: {tb}", tb=txaio.failure_format_traceback(f))
        return None

    # all components are started in parallel
    dl = []
    for c in components:
        # a component can be of type MAIN or SETUP
        d = c.start(reactor)
        txaio.add_callbacks(d, partial(component_success, c), component_failure)
        dl.append(d)
    d = txaio.gather(dl, consume_exceptions=False)

    def all_done(arg):
        log.debug("All components ended; stopping reactor")
        try:
            reactor.stop()
        except ReactorNotRunning:
            pass

    txaio.add_callbacks(d, all_done, all_done)

    return d


def run(components):
    react(_run, [components])
