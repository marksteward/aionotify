import asyncio
import asyncio.streams
import collections
import ctypes
import struct
import os

from . import aioutils

Event = collections.namedtuple('Event', ['flags', 'cookie', 'name', 'alias'])


_libc = ctypes.cdll.LoadLibrary('libc.so.6')

class LibC:
    """Proxy to C functions for inotify"""
    @classmethod
    def inotify_init(cls):
        return _libc.inotify_init()

    @classmethod
    def inotify_add_watch(cls, fd, path, flags):
        return _libc.inotify_add_watch(fd, path.encode('utf-8'), flags)


PREFIX = struct.Struct('iIII')


class Watcher:

    def __init__(self):
        self.requests = {}
        self.descriptors = {}
        self.aliases = {}
        self._stream = None
        self._transport = None
        self._fd = None
        self._loop = None

    def watch(self, path, flags, *, alias=None):
        if alias is None:
            alias = path
        if alias in self.requests:
            raise ValueError("A watch request is already scheduled for alias %s" % alias)
        self.requests[alias] = (path, flags)
        if self._fd is not None:
            # We've started
            self._setup_watch(alias, path, flags)

    def unwatch(self, alias):
        wd = self.descriptors[alias]
        errno = LibC.inotify_rm_watch(self._fd, wd)
        if errno != 0:
            raise IOError("Failed to close watcher %d: errno=%d" % (wd, errno))
        del self.descriptors[alias]
        del self.requests[alias]
        del self.aliases[wd]

    def _setup_watch(self, alias, path, flags):
        assert alias not in self.descriptors, "Registering alias %s twice!" % alias
        wd = LibC.inotify_add_watch(self._fd, path, flags)
        if wd < 0:
            raise IOError("Got wd %s" % wd)
        self.descriptors[alias] = wd
        self.aliases[wd] = alias

    @asyncio.coroutine
    def setup(self, loop):
        self._loop = loop

        self._fd = LibC.inotify_init()
        for alias, (path, flags) in self.requests.items():
            self._setup_watch(alias, path, flags)

        self._stream, self._transport = yield from aioutils.stream_from_fd(self._fd, loop)

    def close(self):
        self._transport.close()

    @asyncio.coroutine
    def get_event(self):
        prefix = yield from self._stream.readexactly(PREFIX.size)
        if prefix == b'':
            return
        wd, flags, cookie, length = PREFIX.unpack(prefix)
        # assert wd == self._wd, "Received an event for another watch descriptor, %s"
        path = yield from self._stream.readexactly(length)
        if path == b'':
            return
        decoded_path = struct.unpack('%ds' % length, path)[0].rstrip(b'\x00').decode('utf-8')
        return Event(
            flags=flags,
            cookie=cookie,
            name=decoded_path,
            alias=self.aliases[wd],
        )