# Copyright 2017, David Wilson
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors
# may be used to endorse or promote products derived from this software without
# specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import errno
import fcntl
import getpass
import inspect
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import termios
import textwrap
import threading
import time
import types
import zlib

try:
    from cStringIO import StringIO as BytesIO
except ImportError:
    from io import BytesIO

if sys.version_info < (2, 7, 11):
    from mitogen.compat import tokenize
else:
    import tokenize

try:
    from functools import lru_cache
except ImportError:
    from mitogen.compat.functools import lru_cache

import mitogen.core
from mitogen.core import LOG
from mitogen.core import IOLOG


try:
    SC_OPEN_MAX = os.sysconf('SC_OPEN_MAX')
except:
    SC_OPEN_MAX = 1024


class Argv(object):
    def __init__(self, argv):
        self.argv = argv

    def escape(self, x):
        s = '"'
        for c in x:
            if c in '\\$"`':
                s += '\\'
            s += c
        s += '"'
        return s

    def __str__(self):
        return ' '.join(map(self.escape, self.argv))


def get_log_level():
    return (LOG.level or logging.getLogger().level or logging.INFO)


def is_immediate_child(msg, stream):
    """
    Handler policy that requires messages to arrive only from immediately
    connected children.
    """
    return msg.src_id == stream.remote_id


@lru_cache()
def minimize_source(source):
    """Remove most comments and docstrings from Python source code.
    """
    tokens = tokenize.generate_tokens(BytesIO(source).readline)
    tokens = strip_comments(tokens)
    tokens = strip_docstrings(tokens)
    tokens = reindent(tokens)
    return tokenize.untokenize(tokens)


def strip_comments(tokens):
    """Drop comment tokens from a `tokenize` stream.

    Comments on lines 1-2 are kept, to preserve hashbang and encoding.
    Trailing whitespace is remove from all lines.
    """
    prev_typ = None
    prev_end_col = 0
    for typ, tok, (start_row, start_col), (end_row, end_col), line in tokens:
        if typ in (tokenize.NL, tokenize.NEWLINE):
            if prev_typ in (tokenize.NL, tokenize.NEWLINE):
                start_col = 0
            else:
                start_col = prev_end_col
            end_col = start_col + 1
        elif typ == tokenize.COMMENT and start_row > 2:
            continue
        prev_typ = typ
        prev_end_col = end_col
        yield typ, tok, (start_row, start_col), (end_row, end_col), line


def strip_docstrings(tokens):
    """Replace docstring tokens with NL tokens in a `tokenize` stream.

    Any STRING token not part of an expression is deemed a docstring.
    Indented docstrings are not yet recognised.
    """
    stack = []
    state = 'wait_string'
    for t in tokens:
        typ = t[0]
        if state == 'wait_string':
            if typ in (tokenize.NL, tokenize.COMMENT):
                yield t
            elif typ in (tokenize.DEDENT, tokenize.INDENT, tokenize.STRING):
                stack.append(t)
            elif typ == tokenize.NEWLINE:
                stack.append(t)
                start_line, end_line = stack[0][2][0], stack[-1][3][0]+1
                for i in range(start_line, end_line):
                    yield tokenize.NL, '\n', (i, 0), (i,1), '\n'
                for t in stack:
                    if t[0] in (tokenize.DEDENT, tokenize.INDENT):
                        yield t[0], t[1], (i+1, t[2][1]), (i+1, t[3][1]), t[4]
                del stack[:]
            else:
                stack.append(t)
                for t in stack: yield t
                del stack[:]
                state = 'wait_newline'
        elif state == 'wait_newline':
            if typ == tokenize.NEWLINE:
                state = 'wait_string'
            yield t


def reindent(tokens, indent=' '):
    """Replace existing indentation in a token steam, with `indent`.
    """
    old_levels = []
    old_level = 0
    new_level = 0
    for typ, tok, (start_row, start_col), (end_row, end_col), line in tokens:
        if typ == tokenize.INDENT:
            old_levels.append(old_level)
            old_level = len(tok)
            new_level += 1
            tok = indent * new_level
        elif typ == tokenize.DEDENT:
            old_level = old_levels.pop()
            new_level -= 1
        start_col = max(0, start_col - old_level + new_level)
        if start_row == end_row:
            end_col = start_col + len(tok)
        yield typ, tok, (start_row, start_col), (end_row, end_col), line


def flags(names):
    """Return the result of ORing a set of (space separated) :py:mod:`termios`
    module constants together."""
    return sum(getattr(termios, name) for name in names.split())


def cfmakeraw(tflags):
    """Given a list returned by :py:func:`termios.tcgetattr`, return a list
    that has been modified in the same manner as the `cfmakeraw()` C library
    function."""
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = tflags
    iflag &= ~flags('IGNBRK BRKINT PARMRK ISTRIP INLCR IGNCR ICRNL IXON')
    oflag &= ~flags('OPOST IXOFF')
    lflag &= ~flags('ECHO ECHOE ECHONL ICANON ISIG IEXTEN')
    cflag &= ~flags('CSIZE PARENB')
    cflag |= flags('CS8')

    # TODO: one or more of the above bit twiddles sets or omits a necessary
    # flag. Forcing these fields to zero, as shown below, gets us what we want
    # on Linux/OS X, but it is possibly broken on some other OS.
    iflag = 0
    oflag = 0
    lflag = 0
    return [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]


def disable_echo(fd):
    old = termios.tcgetattr(fd)
    new = cfmakeraw(old)
    flags = (
        termios.TCSAFLUSH |
        getattr(termios, 'TCSASOFT', 0)
    )
    termios.tcsetattr(fd, flags, new)


def close_nonstandard_fds():
    for fd in xrange(3, SC_OPEN_MAX):
        try:
            os.close(fd)
        except OSError:
            pass


def create_socketpair():
    parentfp, childfp = socket.socketpair()
    parentfp.setsockopt(socket.SOL_SOCKET,
                        socket.SO_SNDBUF,
                        mitogen.core.CHUNK_SIZE)
    childfp.setsockopt(socket.SOL_SOCKET,
                       socket.SO_RCVBUF,
                       mitogen.core.CHUNK_SIZE)
    return parentfp, childfp


def create_child(args, merge_stdio=False):
    """
    Create a child process whose stdin/stdout is connected to a socket.

    :param args:
        Argument vector for execv() call.
    :param bool merge_stdio:
        If :data:`True`, arrange for `stderr` to be connected to the `stdout`
        socketpair, rather than inherited from the parent process. This may be
        necessary to ensure that not TTY is connected to any stdio handle, for
        instance when using LXC.
    :returns:
        `(pid, socket_obj, :data:`None`)`
    """
    parentfp, childfp = create_socketpair()
    # When running under a monkey patches-enabled gevent, the socket module
    # yields file descriptors who already have O_NONBLOCK, which is
    # persisted across fork, totally breaking Python. Therefore, drop
    # O_NONBLOCK from Python's future stdin fd.
    mitogen.core.set_block(childfp.fileno())

    if merge_stdio:
        extra = {'stderr': childfp}
    else:
        extra = {}

    proc = subprocess.Popen(
        args=args,
        stdin=childfp,
        stdout=childfp,
        close_fds=True,
        **extra
    )
    childfp.close()
    # Decouple the socket from the lifetime of the Python socket object.
    fd = os.dup(parentfp.fileno())
    parentfp.close()

    LOG.debug('create_child() child %d fd %d, parent %d, cmd: %s',
              proc.pid, fd, os.getpid(), Argv(args))
    return proc.pid, fd, None


def _acquire_controlling_tty():
    os.setsid()
    if sys.platform == 'linux2':
        # On Linux, the controlling tty becomes the first tty opened by a
        # process lacking any prior tty.
        os.close(os.open(os.ttyname(2), os.O_RDWR))
    if sys.platform.startswith('freebsd') or sys.platform == 'darwin':
        # On BSD an explicit ioctl is required.
        fcntl.ioctl(2, termios.TIOCSCTTY)


def tty_create_child(args):
    """
    Return a file descriptor connected to the master end of a pseudo-terminal,
    whose slave end is connected to stdin/stdout/stderr of a new child process.
    The child is created such that the pseudo-terminal becomes its controlling
    TTY, ensuring access to /dev/tty returns a new file descriptor open on the
    slave end.

    :param list args:
        :py:func:`os.execl` argument list.

    :returns:
        `(pid, tty_fd, None)`
    """
    master_fd, slave_fd = os.openpty()
    mitogen.core.set_block(slave_fd)
    disable_echo(master_fd)
    disable_echo(slave_fd)

    proc = subprocess.Popen(
        args=args,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        preexec_fn=_acquire_controlling_tty,
        close_fds=True,
    )

    os.close(slave_fd)
    LOG.debug('tty_create_child() child %d fd %d, parent %d, cmd: %s',
              proc.pid, master_fd, os.getpid(), Argv(args))
    return proc.pid, master_fd, None


def hybrid_tty_create_child(args):
    """
    Like :func:`tty_create_child`, except attach stdin/stdout to a socketpair
    like :func:`create_child`, but leave stderr and the controlling TTY
    attached to a TTY.

    :param list args:
        :py:func:`os.execl` argument list.

    :returns:
        `(pid, socketpair_fd, tty_fd)`
    """
    master_fd, slave_fd = os.openpty()
    parentfp, childfp = create_socketpair()

    mitogen.core.set_block(slave_fd)
    mitogen.core.set_block(childfp)
    disable_echo(master_fd)
    disable_echo(slave_fd)

    proc = subprocess.Popen(
        args=args,
        stdin=childfp,
        stdout=childfp,
        stderr=slave_fd,
        preexec_fn=_acquire_controlling_tty,
        close_fds=True,
    )

    os.close(slave_fd)
    childfp.close()
    # Decouple the socket from the lifetime of the Python socket object.
    stdio_fd = os.dup(parentfp.fileno())
    parentfp.close()

    LOG.debug('hybrid_tty_create_child() pid=%d stdio=%d, tty=%d, cmd: %s',
              proc.pid, stdio_fd, master_fd, Argv(args))
    return proc.pid, stdio_fd, master_fd


def write_all(fd, s, deadline=None):
    timeout = None
    written = 0

    while written < len(s):
        if deadline is not None:
            timeout = max(0, deadline - time.time())
        if timeout == 0:
            raise mitogen.core.TimeoutError('write timed out')

        _, wfds, _ = select.select([], [fd], [], timeout)
        if not wfds:
            continue

        n, disconnected = mitogen.core.io_op(os.write, fd, buffer(s, written))
        if disconnected:
            raise mitogen.core.StreamError('EOF on stream during write')

        written += n


def iter_read(fds, deadline=None):
    fds = list(fds)
    bits = []
    timeout = None

    while fds:
        if deadline is not None:
            timeout = max(0, deadline - time.time())
            if timeout == 0:
                break

        rfds, _, _ = select.select(fds, [], [], timeout)
        if not rfds:
            continue

        for fd in rfds:
            s, disconnected = mitogen.core.io_op(os.read, fd, 4096)
            if disconnected or not s:
                IOLOG.debug('iter_read(%r) -> disconnected', fd)
                fds.remove(fd)
            else:
                IOLOG.debug('iter_read(%r) -> %r', fd, s)
                bits.append(s)
                yield s

    if not fds:
        raise mitogen.core.StreamError(
            'EOF on stream; last 300 bytes received: %r' %
            (''.join(bits)[-300:],)
        )

    raise mitogen.core.TimeoutError('read timed out')


def discard_until(fd, s, deadline):
    for buf in iter_read([fd], deadline):
        if IOLOG.level == logging.DEBUG:
            for line in buf.splitlines():
                IOLOG.debug('discard_until: discarding %r', line)
        if buf.endswith(s):
            return


def upgrade_router(econtext):
    if not isinstance(econtext.router, Router):  # TODO
        econtext.router.__class__ = Router  # TODO
        econtext.router.upgrade(
            importer=econtext.importer,
            parent=econtext.parent,
        )


def make_call_msg(fn, *args, **kwargs):
    if isinstance(fn, types.MethodType) and \
       isinstance(fn.im_self, (type, types.ClassType)):
        klass = fn.im_self.__name__
    else:
        klass = None

    return mitogen.core.Message.pickled(
        (fn.__module__, klass, fn.__name__, args, kwargs),
        handle=mitogen.core.CALL_FUNCTION,
    )


def stream_by_method_name(name):
    """
    Given the name of a Mitogen connection method, import its implementation
    module and return its Stream subclass.
    """
    if name == 'local':
        name = 'parent'
    Stream = None
    exec('from mitogen.%s import Stream' % (name,))
    return Stream


@mitogen.core.takes_econtext
def _proxy_connect(name, method_name, kwargs, econtext):

    mitogen.parent.upgrade_router(econtext)
    try:
        context = econtext.router._connect(
            klass=stream_by_method_name(method_name),
            name=name,
            **kwargs
        )
    except mitogen.core.StreamError:
        return {
            'id': None,
            'name': None,
            'msg': str(sys.exc_info()[1]),
        }

    return {
        'id': context.context_id,
        'name': context.name,
        'msg': None,
    }


class TtyLogStream(mitogen.core.BasicStream):
    """
    For "hybrid TTY/socketpair" mode, after a connection has been setup, a
    spare TTY file descriptor will exist that cannot be closed, and to which
    SSH or sudo may continue writing log messages.

    The descriptor cannot be closed since the UNIX TTY layer will send a
    termination signal to any processes whose controlling TTY is the TTY that
    has been closed.

    TtyLogStream takes over this descriptor and creates corresponding log
    messages for anything written to it.
    """

    def __init__(self, tty_fd, stream):
        self.receive_side = mitogen.core.Side(stream, tty_fd)
        self.transmit_side = self.receive_side
        self.stream = stream

    def __repr__(self):
        return 'mitogen.parent.TtyLogStream(%r)' % (self.stream,)

    def on_receive(self, broker):
        buf = self.receive_side.read()
        if not buf:
            return self.on_disconnect(broker)

        LOG.debug('%r.on_receive(): %r', self, buf)


class Stream(mitogen.core.Stream):
    """
    Base for streams capable of starting new slaves.
    """
    #: The path to the remote Python interpreter.
    python_path = 'python2.7'

    #: Maximum time to wait for a connection attempt.
    connect_timeout = 30.0

    #: Derived from :py:attr:`connect_timeout`; absolute floating point
    #: UNIX timestamp after which the connection attempt should be abandoned.
    connect_deadline = None

    #: True to cause context to write verbose /tmp/mitogen.<pid>.log.
    debug = False

    #: True to cause context to write /tmp/mitogen.stats.<pid>.<thread>.log.
    profiling = False

    #: Set to the child's PID by connect().
    pid = None

    #: Passed via Router wrapper methods, must eventually be passed to
    #: ExternalContext.main().
    max_message_size = None

    def __init__(self, *args, **kwargs):
        super(Stream, self).__init__(*args, **kwargs)
        self.sent_modules = set(['mitogen', 'mitogen.core'])
        #: List of contexts reachable via this stream; used to cleanup routes
        #: during disconnection.
        self.routes = set([self.remote_id])

    def construct(self, max_message_size, remote_name=None, python_path=None,
                  debug=False, connect_timeout=None, profiling=False,
                  old_router=None, **kwargs):
        """Get the named context running on the local machine, creating it if
        it does not exist."""
        super(Stream, self).construct(**kwargs)
        self.max_message_size = max_message_size
        if python_path:
            self.python_path = python_path
        if sys.platform == 'darwin' and self.python_path == '/usr/bin/python':
            # OS X installs a craptacular argv0-introspecting Python version
            # switcher as /usr/bin/python. Override attempts to call it with an
            # explicit call to python2.7
            self.python_path = '/usr/bin/python2.7'
        if connect_timeout:
            self.connect_timeout = connect_timeout
        if remote_name is None:
            remote_name = '%s@%s:%d'
            remote_name %= (getpass.getuser(), socket.gethostname(), os.getpid())
        if '/' in remote_name or '\\' in remote_name:
            raise ValueError('remote_name= cannot contain slashes')
        self.remote_name = remote_name
        self.debug = debug
        self.profiling = profiling
        self.max_message_size = max_message_size
        self.connect_deadline = time.time() + self.connect_timeout

    def on_shutdown(self, broker):
        """Request the slave gracefully shut itself down."""
        LOG.debug('%r closing CALL_FUNCTION channel', self)
        self.send(
            mitogen.core.Message(
                src_id=mitogen.context_id,
                dst_id=self.remote_id,
                handle=mitogen.core.SHUTDOWN,
            )
        )

    _reaped = False

    def _reap_child(self):
        """
        Reap the child process during disconnection.
        """
        if self._reaped:
            # on_disconnect() may be invoked more than once, for example, if
            # there is still a pending message to be sent after the first
            # on_disconnect() call.
            return

        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except OSError:
            e = sys.exc_info()[1]
            if e.args[0] == errno.ECHILD:
                LOG.warn('%r: waitpid(%r) produced ECHILD', self.pid, self)
                return
            raise

        self._reaped = True
        if pid:
            LOG.debug('%r: child process exit status was %d', self, status)
            return

        # For processes like sudo we cannot actually send sudo a signal,
        # because it is setuid, so this is best-effort only.
        LOG.debug('%r: child process still alive, sending SIGTERM', self)
        try:
            os.kill(self.pid, signal.SIGTERM)
        except OSError:
            e = sys.exc_info()[1]
            if e.args[0] != errno.EPERM:
                raise

    def on_disconnect(self, broker):
        self._reap_child()
        super(Stream, self).on_disconnect(broker)

    # Minimised, gzipped, base64'd and passed to 'python -c'. It forks, dups
    # file descriptor 0 as 100, creates a pipe, then execs a new interpreter
    # with a custom argv.
    #   * Optimized for minimum byte count after minification & compression.
    #   * 'CONTEXT_NAME', 'PREAMBLE_COMPRESSED_LEN', and 'PREAMBLE_LEN' are
    #     substituted with their respective values.
    #   * CONTEXT_NAME must be prefixed with the name of the Python binary in
    #     order to allow virtualenvs to detect their install prefix.
    @staticmethod
    def _first_stage():
        R,W=os.pipe()
        r,w=os.pipe()
        if os.fork():
            os.dup2(0,100)
            os.dup2(R,0)
            os.dup2(r,101)
            os.close(R)
            os.close(r)
            os.close(W)
            os.close(w)
            os.environ['ARGV0']=sys.executable
            os.execl(sys.executable,sys.executable+'(mitogen:CONTEXT_NAME)')
        os.write(1,'EC0\n')
        C=_(os.fdopen(0,'rb').read(PREAMBLE_COMPRESSED_LEN),'zip')
        os.fdopen(W,'w',0).write(C)
        os.fdopen(w,'w',0).write('PREAMBLE_LEN\n'+C)
        os.write(1,'EC1\n')

    def get_boot_command(self):
        source = inspect.getsource(self._first_stage)
        source = textwrap.dedent('\n'.join(source.strip().split('\n')[2:]))
        source = source.replace('    ', '\t')
        source = source.replace('CONTEXT_NAME', self.remote_name)
        preamble_compressed = self.get_preamble()
        source = source.replace('PREAMBLE_COMPRESSED_LEN',
                                str(len(preamble_compressed)))
        source = source.replace('PREAMBLE_LEN',
                                str(len(zlib.decompress(preamble_compressed))))
        encoded = zlib.compress(source, 9).encode('base64').replace('\n', '')
        # We can't use bytes.decode() in 3.x since it was restricted to always
        # return unicode, so codecs.decode() is used instead. In 3.x
        # codecs.decode() requires a bytes object. Since we must be compatible
        # with 2.4 (no bytes literal), an extra .encode() either returns the
        # same str (2.x) or an equivalent bytes (3.x).
        return [
            self.python_path, '-c',
            'import codecs,os,sys;_=codecs.decode;'
            'exec(_(_("%s".encode(),"base64"),"zip"))' % (encoded,)
        ]

    def get_main_kwargs(self):
        assert self.max_message_size is not None
        parent_ids = mitogen.parent_ids[:]
        parent_ids.insert(0, mitogen.context_id)
        return {
            'parent_ids': parent_ids,
            'context_id': self.remote_id,
            'debug': self.debug,
            'profiling': self.profiling,
            'log_level': get_log_level(),
            'whitelist': self._router.get_module_whitelist(),
            'blacklist': self._router.get_module_blacklist(),
            'max_message_size': self.max_message_size,
            'version': mitogen.__version__,
        }

    def get_preamble(self):
        source = inspect.getsource(mitogen.core)
        source += '\nExternalContext().main(**%r)\n' % (
            self.get_main_kwargs(),
        )
        return zlib.compress(minimize_source(source), 9)

    create_child = staticmethod(create_child)
    create_child_args = {}
    name_prefix = 'local'

    def start_child(self):
        args = self.get_boot_command()
        try:
            return self.create_child(args, **self.create_child_args)
        except OSError:
            e = sys.exc_info()[1]
            msg = 'Child start failed: %s. Command was: %s' % (e, Argv(args))
            raise mitogen.core.StreamError(msg)

    def connect(self):
        LOG.debug('%r.connect()', self)
        self.pid, fd, extra_fd = self.start_child()
        self.name = '%s.%s' % (self.name_prefix, self.pid)
        self.receive_side = mitogen.core.Side(self, fd)
        self.transmit_side = mitogen.core.Side(self, os.dup(fd))
        LOG.debug('%r.connect(): child process stdin/stdout=%r',
                  self, self.receive_side.fd)

        try:
            self._connect_bootstrap(extra_fd)
        except Exception:
            self._reap_child()
            raise

    def _ec0_received(self):
        LOG.debug('%r._ec0_received()', self)
        write_all(self.transmit_side.fd, self.get_preamble())
        discard_until(self.receive_side.fd, 'EC1\n', time.time() + 10.0)

    def _connect_bootstrap(self, extra_fd):
        deadline = time.time() + self.connect_timeout
        discard_until(self.receive_side.fd, 'EC0\n', deadline)
        self._ec0_received()


class ChildIdAllocator(object):
    def __init__(self, router):
        self.router = router
        self.lock = threading.Lock()
        self.it = iter(xrange(0))

    def allocate(self):
        self.lock.acquire()
        try:
            for id_ in self.it:
                return id_

            master = mitogen.core.Context(self.router, 0)
            start, end = master.send_await(
                mitogen.core.Message(dst_id=0, handle=mitogen.core.ALLOCATE_ID)
            )
            self.it = iter(xrange(start, end))
        finally:
            self.lock.release()

        return self.allocate()


class Context(mitogen.core.Context):
    via = None

    def __eq__(self, other):
        return (isinstance(other, mitogen.core.Context) and
                (other.context_id == self.context_id) and
                (other.router == self.router))

    def __hash__(self):
        return hash((self.router, self.context_id))

    def call_async(self, fn, *args, **kwargs):
        LOG.debug('%r.call_async(%r, *%r, **%r)',
                  self, fn, args, kwargs)
        return self.send_async(make_call_msg(fn, *args, **kwargs))

    def call(self, fn, *args, **kwargs):
        receiver = self.call_async(fn, *args, **kwargs)
        return receiver.get().unpickle(throw_dead=False)

    def shutdown(self, wait=False):
        LOG.debug('%r.shutdown() sending SHUTDOWN', self)
        latch = mitogen.core.Latch()
        mitogen.core.listen(self, 'disconnect', lambda: latch.put(None))

        self.send(
            mitogen.core.Message(
                handle=mitogen.core.SHUTDOWN,
            )
        )

        if wait:
            latch.get()


class RouteMonitor(object):
    def __init__(self, router, parent=None):
        self.router = router
        self.parent = parent
        self.router.add_handler(
            fn=self._on_add_route,
            handle=mitogen.core.ADD_ROUTE,
            persist=True,
            policy=is_immediate_child,
        )
        self.router.add_handler(
            fn=self._on_del_route,
            handle=mitogen.core.DEL_ROUTE,
            persist=True,
            policy=is_immediate_child,
        )

    def propagate(self, handle, target_id, name=None):
        # self.parent is None in the master.
        if not self.parent:
            return

        data = str(target_id)
        if name:
            data = '%s:%s' % (target_id, mitogen.core.b(name))
        self.parent.send(
            mitogen.core.Message(
                handle=handle,
                data=data,
            )
        )

    def notice_stream(self, stream):
        """
        When this parent is responsible for a new directly connected child
        stream, we're also responsible for broadcasting DEL_ROUTE upstream
        if/when that child disconnects.
        """
        self.propagate(mitogen.core.ADD_ROUTE, stream.remote_id, stream.name)
        mitogen.core.listen(
            obj=stream,
            name='disconnect',
            func=lambda: self._on_stream_disconnect(stream),
        )

    def _on_stream_disconnect(self, stream):
        """
        Respond to disconnection of a local stream by 
        """
        LOG.debug('%r is gone; propagating DEL_ROUTE for %r',
                  stream, stream.routes)
        for target_id in stream.routes:
            self.router.del_route(target_id)
            self.propagate(mitogen.core.DEL_ROUTE, target_id)

            context = self.router.context_by_id(target_id, create=False)
            if context:
                mitogen.core.fire(context, 'disconnect')

    def _on_add_route(self, msg):
        if msg.is_dead:
            return

        target_id_s, _, target_name = msg.data.partition(':')
        target_id = int(target_id_s)
        self.router.context_by_id(target_id).name = target_name
        stream = self.router.stream_by_id(msg.auth_id)
        current = self.router.stream_by_id(target_id)
        if current and current.remote_id != mitogen.parent_id:
            LOG.error('Cannot add duplicate route to %r via %r, '
                      'already have existing route via %r',
                      target_id, stream, current)
            return

        LOG.debug('Adding route to %d via %r', target_id, stream)
        stream.routes.add(target_id)
        self.router.add_route(target_id, stream)
        self.propagate(mitogen.core.ADD_ROUTE, target_id, target_name)

    def _on_del_route(self, msg):
        if msg.is_dead:
            return

        target_id = int(msg.data)
        registered_stream = self.router.stream_by_id(target_id)
        stream = self.router.stream_by_id(msg.auth_id)
        if registered_stream != stream:
            LOG.error('Received DEL_ROUTE for %d from %r, expected %r',
                      target_id, stream, registered_stream)
            return

        LOG.debug('Deleting route to %d via %r', target_id, stream)
        stream.routes.discard(target_id)
        self.router.del_route(target_id)
        self.propagate(mitogen.core.DEL_ROUTE, target_id)
        context = self.router.context_by_id(target_id, create=False)
        if context:
            mitogen.core.fire(context, 'disconnect')


class Router(mitogen.core.Router):
    context_class = Context
    debug = False
    profiling = False

    id_allocator = None
    responder = None
    log_forwarder = None
    route_monitor = None

    def upgrade(self, importer, parent):
        LOG.debug('%r.upgrade()', self)
        self.id_allocator = ChildIdAllocator(router=self)
        self.responder = ModuleForwarder(
            router=self,
            parent_context=parent,
            importer=importer,
        )
        self.route_monitor = RouteMonitor(self, parent)

    def stream_by_id(self, dst_id):
        return self._stream_by_id.get(dst_id,
            self._stream_by_id.get(mitogen.parent_id))

    def add_route(self, target_id, stream):
        LOG.debug('%r.add_route(%r, %r)', self, target_id, stream)
        assert isinstance(target_id, int)
        assert isinstance(stream, Stream)
        try:
            self._stream_by_id[target_id] = stream
        except KeyError:
            LOG.error('%r: cant add route to %r via %r: no such stream',
                      self, target_id, stream)

    def del_route(self, target_id):
        LOG.debug('%r.del_route(%r)', self, target_id)
        try:
            del self._stream_by_id[target_id]
        except KeyError:
            LOG.error('%r: cant delete route to %r: no such stream',
                      self, target_id)

    def get_module_blacklist(self):
        if mitogen.context_id == 0:
            return self.responder.blacklist
        return self.importer.blacklist

    def get_module_whitelist(self):
        if mitogen.context_id == 0:
            return self.responder.whitelist
        return self.importer.whitelist

    def allocate_id(self):
        return self.id_allocator.allocate()

    def context_by_id(self, context_id, via_id=None, create=True):
        context = self._context_by_id.get(context_id)
        if create and not context:
            context = self.context_class(self, context_id)
            if via_id is not None:
                context.via = self.context_by_id(via_id)
            self._context_by_id[context_id] = context
        return context

    def _connect(self, klass, name=None, **kwargs):
        context_id = self.allocate_id()
        context = self.context_class(self, context_id)
        kwargs['old_router'] = self
        kwargs['max_message_size'] = self.max_message_size
        stream = klass(self, context_id, **kwargs)
        if name is not None:
            stream.name = name
        stream.connect()
        context.name = stream.name
        self.route_monitor.notice_stream(stream)
        self.register(context, stream)
        return context

    def connect(self, method_name, name=None, **kwargs):
        klass = stream_by_method_name(method_name)
        kwargs.setdefault('debug', self.debug)
        kwargs.setdefault('profiling', self.profiling)

        via = kwargs.pop('via', None)
        if via is not None:
            return self.proxy_connect(via, method_name, name=name, **kwargs)
        return self._connect(klass, name=name, **kwargs)

    def proxy_connect(self, via_context, method_name, name=None, **kwargs):
        resp = via_context.call(_proxy_connect,
            name=name,
            method_name=method_name,
            kwargs=kwargs
        )
        if resp['msg'] is not None:
            raise mitogen.core.StreamError(resp['msg'])

        name = '%s.%s' % (via_context.name, resp['name'])
        context = self.context_class(self, resp['id'], name=name)
        context.via = via_context
        self._context_by_id[context.context_id] = context
        return context

    def docker(self, **kwargs):
        return self.connect('docker', **kwargs)

    def fork(self, **kwargs):
        return self.connect('fork', **kwargs)

    def jail(self, **kwargs):
        return self.connect('jail', **kwargs)

    def local(self, **kwargs):
        return self.connect('local', **kwargs)

    def lxc(self, **kwargs):
        return self.connect('lxc', **kwargs)

    def ssh(self, **kwargs):
        return self.connect('ssh', **kwargs)

    def sudo(self, **kwargs):
        return self.connect('sudo', **kwargs)


class ProcessMonitor(object):
    def __init__(self):
        # pid -> callback()
        self.callback_by_pid = {}
        signal.signal(signal.SIGCHLD, self._on_sigchld)

    def _on_sigchld(self, _signum, _frame):
        for pid, callback in self.callback_by_pid.items():
            pid, status = os.waitpid(pid, os.WNOHANG)
            if pid:
                callback(status)
                del self.callback_by_pid[pid]

    def add(self, pid, callback):
        self.callback_by_pid[pid] = callback

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance


class ModuleForwarder(object):
    """
    Respond to GET_MODULE requests in a slave by forwarding the request to our
    parent context, or satisfying the request from our local Importer cache.
    """
    def __init__(self, router, parent_context, importer):
        self.router = router
        self.parent_context = parent_context
        self.importer = importer
        router.add_handler(
            fn=self._on_get_module,
            handle=mitogen.core.GET_MODULE,
            persist=True,
            policy=is_immediate_child,
        )

    def __repr__(self):
        return 'ModuleForwarder(%r)' % (self.router,)

    def _on_get_module(self, msg):
        LOG.debug('%r._on_get_module(%r)', self, msg)
        if msg.is_dead:
            return

        fullname = msg.data
        callback = lambda: self._on_cache_callback(msg, fullname)
        self.importer._request_module(fullname, callback)

    def _send_one_module(self, msg, tup):
        self.router._async_route(
            mitogen.core.Message.pickled(
                tup,
                dst_id=msg.src_id,
                handle=mitogen.core.LOAD_MODULE,
            )
        )

    def _on_cache_callback(self, msg, fullname):
        LOG.debug('%r._on_get_module(): sending %r', self, fullname)
        tup = self.importer._cache[fullname]
        if tup is not None:
            for related in tup[4]:
                rtup = self.importer._cache.get(related)
                if not rtup:
                    LOG.debug('%r._on_get_module(): skipping absent %r',
                               self, related)
                    continue
                self._send_one_module(msg, rtup)

        self._send_one_module(msg, tup)
