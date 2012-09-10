#!/usr/bin/python
#
# node.py - Distributed bond P2P network node
#
# Distributed under the MIT/X11 software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
#

import gevent
from gevent import Greenlet
from gevent import monkey; monkey.patch_all()

import hashlib
import sys
import re
import socket
import time
import struct
import random
import signal

import Log
import codec_pb2

PROTO_VERSION = 10000
MIN_PROTO_VERSION = 10000
MY_SUBVERSION = "/bond-node-0.1/"

settings = {}
debugnet = True

def verbose_sendmsg(command):
    if debugnet:
        return True
    return True

def verbose_recvmsg(command):
    skipmsg = { }
    if debugnet:
        return True
    if command in skipmsg:
        return False
    return True

class MsgNull(object):
    def __init__(self):
        pass
    def SerializeToString(self):
        return ''
    def ParseFromString(self, data):
        pass

class NodeConn(Greenlet):
    messagemap = {
        "version",
        "verack",
        "ping",
        "pong",
        "addr",
        "getaddr",
    }
        
    def __init__(self, log, peermgr, sock=None, dstaddr=None, dstport=None):
        Greenlet.__init__(self)
        self.log = log
        self.peermgr = peermgr
        self.dstaddr = dstaddr
        self.dstport = dstport
        self.recvbuf = ""
        self.ver_send = MIN_PROTO_VERSION
        self.last_sent = 0

        if sock is None:  # Outgoing client mode
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.outbound = True
            #stuff version msg into sendbuf
            vt = codec_pb2.MsgVersion()
            vt.proto_ver = PROTO_VERSION
            vt.client_ver = MY_SUBVERSION

            self.log.write("connecting to " + self.dstaddr)
            try:
                self.sock = socket.socket()
                self.sock.connect((dstaddr, dstport))
            except:
                self.handle_close()
            self.send_message("version", vt, True)

        else:  # Listening mode
            self.sock = sock
            self.outbound = False
            if self.dstaddr is None:
                self.dstaddr = '0.0.0.0'
            if self.dstport is None:
                self.dstport = 0
            self.log.write(self.dstaddr + " connected")


    def _run(self):
        self.log.write(self.dstaddr + " connected")
        while True:
            try:
                t = self.sock.recv(8192)
                if len(t) <= 0: raise ValueError
            except (IOError, ValueError):
                self.handle_close()
                return
            self.recvbuf += t
            self.got_data()

    def handle_close(self):
        self.log.write(self.dstaddr + " close")
        self.recvbuf = ""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except:
            pass

    def got_data(self):
        while True:
            if len(self.recvbuf) < 4:
                return
            if self.recvbuf[:4] != 'BND1':
                raise ValueError("got garbage %s" % repr(self.recvbuf))
            # check checksum
            if len(self.recvbuf) < 4 + 12 + 4 + 4:
                return
            command = self.recvbuf[4:4+12].split("\x00", 1)[0]
            msglen = struct.unpack("<I", self.recvbuf[4+12:4+12+4])[0]
            if msglen > (16 * 1024 * 1024):
                raise ValueError("msglen %u too big" % (msglen,))

            checksum = self.recvbuf[4+12+4:4+12+4+4]
            if len(self.recvbuf) < 4 + 12 + 4 + 4 + msglen:
                return
            msg = self.recvbuf[4+12+4+4:4+12+4+4+msglen]
            th = hashlib.sha256(msg).digest()
            h = hashlib.sha256(th).digest()
            if checksum != h[:4]:
                raise ValueError("got bad checksum %s" % repr(self.recvbuf))
            self.recvbuf = self.recvbuf[4+12+4+4+msglen:]

            if command in self.messagemap:
                if command == "version":
                    t = codec_pb2.MsgVersion()
                elif command == "verack":
                    t = MsgNull()
                elif command == "ping":
                    t = codec_pb2.MsgPingPong()
                elif command == "pong":
                    t = codec_pb2.MsgPingPong()
                elif command == "addr":
                    t = codec_pb2.MsgAddresses()
                elif command == "getaddr":
                    t = MsgNull()

                try:
                    t.ParseFromString(msg)
                except google.protobuf.message.DecodeError:
                    raise ValueError("bad decode %s" % repr(self.recvbuf))

                self.got_message(command, t)
            else:
                self.log.write("UNKNOWN COMMAND %s %s" % (command, repr(msg)))

    def send_message(self, command, message, pushbuf=False):
        if verbose_sendmsg(command):
            self.log.write("send %s %s" % (command, repr(message)))

        data = message.SerializeToString()
        tmsg = 'BND1'
        tmsg += command
        tmsg += "\x00" * (12 - len(command))
        tmsg += struct.pack("<I", len(data))

        # add checksum
        th = hashlib.sha256(data).digest()
        h = hashlib.sha256(th).digest()
        tmsg += h[:4]
        tmsg += data
        self.sock.sendall(tmsg)
        self.last_sent = time.time()

    def got_message(self, command, message):
        if verbose_recvmsg(command):
            self.log.write("recv %s %s" % (command, repr(message)))

        if command == "version":
            self.ver_send = min(PROTO_VERSION, message.proto_ver)
            if self.ver_send < MIN_PROTO_VERSION:
                self.log.write("disconnecting unsupported version")
                self.handle_close()
                return

            # incoming connections send "version" first
            if not self.outbound:
                msgout = codec_pb2.MsgVersion()
                msgout.proto_ver = PROTO_VERSION
                msgout.client_ver = MY_SUBVERSION
                self.send_message("version", msgout)

            self.send_message("verack", MsgNull())

        elif command == "verack":
            self.send_message("getaddr", MsgNull())

        elif command == "ping":
            msgout = codec_pb2.MsgPingPong()
            msgout.cookie = message.cookie
            self.send_message("pong", msgout)

        elif command == "addr":
            self.peermgr.new_addrs(message.peers)

        elif command == "getaddr":
            peers = self.peermgr.random_addrs()
            msgout = codec_pb2.MsgAddresses()

            for peer in peers:
                addr = msgout.peers.add()
                addr.proto_ver = peer.proto_ver
                addr.time = peer.time
                addr.flags = peer.flags
                addr.ip = peer.ip
                addr.port = peer.port

            self.send_message("addr", msgout)

class NodeServer(Greenlet):
    def __init__(self, host, port, log, peermgr):
        Greenlet.__init__(self)
        self.log = log
        self.peermgr = peermgr        
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(25)

    def _run(self):
        while True:
            self.handle_accept()

    def handle_accept(self):
        pair = self.sock.accept()
        if pair is None:
            pass
        else:
            sock, addr = pair
            self.log.write('Incoming connection from %s' % repr(addr))
            handler = NodeConn(self.log, self.peermgr, sock=sock,
                               dstaddr=addr[0], dstport=addr[1])
            handler.start()

class PeerManager(object):
    def __init__(self, log):
        self.log = log
        self.peers = []
        self.addrs = {}
        self.tried = {}

    def add(self, host, port):
        self.log.write("PeerManager: connecting to %s:%d" %
                   (host, port))
        self.tried[host] = True
        c = NodeConn(self.log, self, dstaddr=host, dstport=port)
        self.peers.append(c)
        return c

    def new_addrs(self, addrs):
        for addr in addrs:
            if addr.ip in self.addrs:
                continue
            self.addrs[addr.ip] = addr

        self.log.write("PeerManager: Received %d new addresses (%d addrs, %d tried)" %
                (len(addrs), len(self.addrs),
                 len(self.tried)))

    def random_addrs(self):
        ips = self.addrs.keys()
        random.shuffle(ips)
        if len(ips) > 1000:
            del ips[1000:]

        vaddr = []
        for ip in ips:
            vaddr.append(self.addrs[ip])

        return vaddr

    def closeall(self):
        for peer in self.peers:
            peer.handle_close()
        self.peers = []

def getboolarg(s):
    if not s:
        return False
    if s == '1' or s == 'yes' or s == 'YES' or s == 'Yes':
        return True
    return False

if __name__ == '__main__':
    if len(sys.argv) != 2:
        print "Usage: node.py CONFIG-FILE"
        sys.exit(1)

    f = open(sys.argv[1])
    for line in f:
        m = re.search('^(\w+)\s*=\s*(\S.*)$', line)
        if m is None:
            continue
        settings[m.group(1)] = m.group(2)
    f.close()

    if 'rpcport' not in settings:
        settings['rpcport'] = 9332
    if 'db' not in settings:
        settings['db'] = '/tmp/chaindb'
    if 'chain' not in settings:
        settings['chain'] = 'mainnet'
    if 'listen' not in settings:
        settings['listen'] = False
    else:
        settings['listen'] = getboolarg(settings['listen'])
    chain = settings['chain']
    if 'log' not in settings or (settings['log'] == '-'):
        settings['log'] = None

    if 'port' in settings:
        settings['port'] = int(settings['port'])
    if 'listen_port' in settings:
        settings['listen_port'] = int(settings['listen_port'])

    addnode = ('host' in settings and 'port' in settings)

    if ('rpcuser' not in settings or
        'rpcpass' not in settings):
        print "You must set the following in config: rpcuser, rpcpass"
        sys.exit(1)

    if 'port' in settings:
        settings['port'] = int(settings['port'])
    settings['rpcport'] = int(settings['rpcport'])

    log = Log.Log(settings['log'])

    log.write("\n\n\n\n")

    peermgr = PeerManager(log)

    threads = []

    # connect to specified remote node
    if addnode:
        c = peermgr.add(settings['host'], settings['port'])
        c.start()
        threads.append(c)

    # start HTTP server for JSON-RPC
    #s = httpsrv.Server('', settings['rpcport'], rpc.RPCRequestHandler,
    #          (log, peermgr,
    #           settings['rpcuser'], settings['rpcpass']))

    if settings['listen']:
        p2pserver = NodeServer(settings['listen_host'],
                               settings['listen_port'],
                               log, peermgr)

        # program main loop
        p2pserver.start()
        threads.append(p2pserver)

    def shutdown():
        for t in threads: t.kill()
    gevent.signal(signal.SIGINT, shutdown)
    gevent.joinall(threads)
