# -*- coding: utf-8 -*-
"""
    flask.ctx
    ~~~~~~~~~

    Implements the objects required to keep the context.

    :copyright: (c) 2011 by Armin Ronacher.
    :license: BSD, see LICENSE for more details.
"""

import sys

from werkzeug.exceptions import HTTPException

from .globals import _request_ctx_stack, _app_ctx_stack
from .module import blueprint_is_module


class _RequestGlobals(object):
    """A plain object."""
    pass


def after_this_request(f):
    """Executes a function after this request.  This is useful to modify
    response objects.  The function is passed the response object and has
    to return the same or a new one.

    Example::

        @app.route('/')
        def index():
            @after_this_request
            def add_header(response):
                response.headers['X-Foo'] = 'Parachute'
                return response
            return 'Hello World!'

    This is more useful if a function other than the view function wants to
    modify a response.  For instance think of a decorator that wants to add
    some headers without converting the return value into a response object.

    .. versionadded:: 0.9
    """
    _request_ctx_stack.top._after_request_functions.append(f)
    return f


def has_request_context():
    """If you have code that wants to test if a request context is there or
    not this function can be used.  For instance, you may want to take advantage
    of request information if the request object is available, but fail
    silently if it is unavailable.

    ::

        class User(db.Model):

            def __init__(self, username, remote_addr=None):
                self.username = username
                if remote_addr is None and has_request_context():
                    remote_addr = request.remote_addr
                self.remote_addr = remote_addr

    Alternatively you can also just test any of the context bound objects
    (such as :class:`request` or :class:`g` for truthness)::

        class User(db.Model):

            def __init__(self, username, remote_addr=None):
                self.username = username
                if remote_addr is None and request:
                    remote_addr = request.remote_addr
                self.remote_addr = remote_addr

    .. versionadded:: 0.7
    """
    return _request_ctx_stack.top is not None


def has_app_context():
    """Works like :func:`has_request_context` but for the application
    context.  You can also just do a boolean check on the
    :data:`current_app` object instead.

    .. versionadded:: 0.9
    """
    return _app_ctx_stack.top is not None


class AppContext(object):
    """The application context binds an application object implicitly
    to the current thread or greenlet, similar to how the
    :class:`RequestContext` binds request information.  The application
    context is also implicitly created if a request context is created
    but the application is not on top of the individual application
    context.
    """

    def __init__(self, app):
        self.app = app
        self.url_adapter = app.create_url_adapter(None)

        # Like request context, app contexts can be pushed multiple times
        # but there a basic "refcount" is enough to track them.
        self._refcnt = 0

    def push(self):
        """Binds the app context to the current context."""
        self._refcnt += 1
        _app_ctx_stack.push(self)

    def pop(self, exc=None):
        """Pops the app context."""
        self._refcnt -= 1
        if self._refcnt <= 0:
            if exc is None:
                exc = sys.exc_info()[1]
            self.app.do_teardown_appcontext(exc)
        rv = _app_ctx_stack.pop()
        assert rv is self, 'Popped wrong app context.  (%r instead of %r)' \
            % (rv, self)

    def __enter__(self):
        self.push()
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self.pop(exc_value)


class RequestContext(object):
    """The request context contains all request relevant information.  It is
    created at the beginning of the request and pushed to the
    `_request_ctx_stack` and removed at the end of it.  It will create the
    URL adapter and request object for the WSGI environment provided.

    Do not attempt to use this class directly, instead use
    :meth:`~flask.Flask.test_request_context` and
    :meth:`~flask.Flask.request_context` to create this object.

    When the request context is popped, it will evaluate all the
    functions registered on the application for teardown execution
    (:meth:`~flask.Flask.teardown_request`).

    The request context is automatically popped at the end of the request
    for you.  In debug mode the request context is kept around if
    exceptions happen so that interactive debuggers have a chance to
    introspect the data.  With 0.4 this can also be forced for requests
    that did not fail and outside of `DEBUG` mode.  By setting
    ``'flask._preserve_context'`` to `True` on the WSGI environment the
    context will not pop itself at the end of the request.  This is used by
    the :meth:`~flask.Flask.test_client` for example to implement the
    deferred cleanup functionality.

    You might find this helpful for unittests where you need the
    information from the context local around for a little longer.  Make
    sure to properly :meth:`~werkzeug.LocalStack.pop` the stack yourself in
    that situation, otherwise your unittests will leak memory.
    """

    def __init__(self, app, environ):
        self.app = app
        self.request = app.request_class(environ)
        self.url_adapter = app.create_url_adapter(self.request)
        self.g = app.request_globals_class()
        self.flashes = None
        self.session = None

        # Request contexts can be pushed multiple times and interleaved with
        # other request contexts.  Now only if the last level is popped we
        # get rid of them.  Additionally if an application context is missing
        # one is created implicitly so for each level we add this information
        self._implicit_app_ctx_stack = []

        # indicator if the context was preserved.  Next time another context
        # is pushed the preserved context is popped.
        self.preserved = False

        # Functions that should be executed after the request on the response
        # object.  These will be called before the regular "after_request"
        # functions.
        self._after_request_functions = []

        self.match_request()

        # XXX: Support for deprecated functionality.  This is going away with
        # Flask 1.0
        blueprint = self.request.blueprint
        if blueprint is not None:
            # better safe than sorry, we don't want to break code that
            # already worked
            bp = app.blueprints.get(blueprint)
            if bp is not None and blueprint_is_module(bp):
                self.request._is_old_module = True

    def match_request(self):
        """Can be overridden by a subclass to hook into the matching
        of the request.
        """
        try:
            url_rule, self.request.view_args = \
                self.url_adapter.match(return_rule=True)
            self.request.url_rule = url_rule
        except HTTPException, e:
            self.request.routing_exception = e

    def push(self):
        """Binds the request context to the current context."""
        # If an exception ocurrs in debug mode or if context preservation is
        # activated under exception situations exactly one context stays
        # on the stack.  The rationale is that you want to access that
        # information under debug situations.  However if someone forgets to
        # pop that context again we want to make sure that on the next push
        # it's invalidated otherwise we run at risk that something leaks
        # memory.  This is usually only a problem in testsuite since this
        # functionality is not active in production environments.
        top = _request_ctx_stack.top
        if top is not None and top.preserved:
            top.pop()

        # Before we push the request context we have to ensure that there
        # is an application context.
        app_ctx = _app_ctx_stack.top
        if app_ctx is None or app_ctx.app != self.app:
            app_ctx = self.app.app_context()
            app_ctx.push()
            self._implicit_app_ctx_stack.append(app_ctx)
        else:
            self._implicit_app_ctx_stack.append(None)

        _request_ctx_stack.push(self)

        # Open the session at the moment that the request context is
        # available. This allows a custom open_session method to use the
        # request context (e.g. flask-sqlalchemy).
        self.session = self.app.open_session(self.request)
        if self.session is None:
            self.session = self.app.make_null_session()

    def pop(self, exc=None):
        """Pops the request context and unbinds it by doing that.  This will
        also trigger the execution of functions registered by the
        :meth:`~flask.Flask.teardown_request` decorator.

        .. versionchanged:: 0.9
           Added the `exc` argument.
        """
        app_ctx = self._implicit_app_ctx_stack.pop()

        clear_request = False
        if not self._implicit_app_ctx_stack:
            self.preserved = False
            if exc is None:
                exc = sys.exc_info()[1]
            self.app.do_teardown_request(exc)
            clear_request = True

        rv = _request_ctx_stack.pop()
        assert rv is self, 'Popped wrong request context.  (%r instead of %r)' \
            % (rv, self)

        # get rid of circular dependencies at the end of the request
        # so that we don't require the GC to be active.
        if clear_request:
            rv.request.environ['werkzeug.request'] = None

        # Get rid of the app as well if necessary.
        if app_ctx is not None:
            app_ctx.pop(exc)

    def __enter__(self):
        self.push()
        return self

    def __exit__(self, exc_type, exc_value, tb):
        # do not pop the request stack if we are in debug mode and an
        # exception happened.  This will allow the debugger to still
        # access the request object in the interactive shell.  Furthermore
        # the context can be force kept alive for the test client.
        # See flask.testing for how this works.
        if self.request.environ.get('flask._preserve_context') or \
           (tb is not None and self.app.preserve_context_on_exception):
            self.preserved = True
        else:
            self.pop(exc_value)

    def __repr__(self):
        return '<%s \'%s\' [%s] of %s>' % (
            self.__class__.__name__,
            self.request.url,
            self.request.method,
            self.app.name
        )
