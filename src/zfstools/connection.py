'''
ZFS connection classes
'''

import subprocess
import os
from zfstools.models import PoolSet
from zfstools.util import progressbar, SpecialPopen
from Queue import Queue
from threading import Thread


class ZFSConnection:
    host = None
    _poolset = None
    _dirty = True
    _trust = False
    def __init__(self,host="localhost", trust=False):
        self.host = host
        self._trust = trust
        self._poolset= PoolSet()
        if host in ['localhost','127.0.0.1']:
            self.command = ["zfs"]
        else:
            self.command = ["ssh","-o","BatchMode yes","-a","-x","-c","arcfour"]
            if self._trust:
                self.command.extend(["-o","CheckHostIP no"])
                self.command.extend(["-o","StrictHostKeyChecking no"])
            self.command.extend([self.host,"zfs"])

    def _get_poolset(self):
        if self._dirty:
            stdout2 = subprocess.check_output(self.command + ["get", "-Hpr", "-o", "name,value", "creation"])
            self._poolset.parse_zfs_r_output(stdout2)
            self._dirty = False
        return self._poolset
    pools = property(_get_poolset)

    def create_dataset(self,name):
        subprocess.check_call(self.command + ["create", name])
        self._dirty = True
        return self.pools.lookup(name)

    def destroy_dataset(self, name):
        subprocess.check_call(self.command + ["destroy", name])
        self._dirty = True

    def destroy_recursively(self, name):
        subprocess.check_call(self.command + ["destroy", '-r', name])
        self._dirty = True

    def snapshot_recursively(self,name,snapshotname):
        subprocess.check_call(self.command + ["snapshot", "-r", "%s@%s" % (name, snapshotname)])
        self._dirty = True

    def send(self,name,opts=None,bufsize=-1,compression=False):
        if not opts: opts = []
        cmd = list(self.command)
        if compression and cmd[0] == 'ssh': cmd.insert(1,"-C")
        cmd = cmd + ["send"] + opts + [name]
        p = SpecialPopen(cmd,stdin=file(os.devnull),stdout=subprocess.PIPE,bufsize=bufsize)
        return p

    def receive(self,name,pipe,opts=None,bufsize=-1,compression=False):
        if not opts: opts = []
        cmd = list(self.command)
        if compression and cmd[0] == 'ssh': cmd.insert(1,"-C")
        cmd = cmd + ["receive"] + opts + [name]
        p = SpecialPopen(cmd,stdin=pipe,bufsize=bufsize)
        return p

    def transfer(self, dst_conn, s, d, fromsnapshot=None, showprogress=False, bufsize=-1, send_opts=None, receive_opts=None, ratelimit=-1, compression=False):
        if send_opts is None: send_opts = []
        if receive_opts is None: receive_opts = []
        
        queue_of_killables = Queue()

        if fromsnapshot: fromsnapshot=["-i",fromsnapshot]
        else: fromsnapshot = []
        sndprg = self.send(s, opts=[] + fromsnapshot + send_opts, bufsize=bufsize, compression=compression)
        sndprg_supervisor = Thread(target=lambda: queue_of_killables.put((sndprg, sndprg.wait())))
        sndprg_supervisor.start()

        if showprogress:
            try:
                        barprg = progressbar(pipe=sndprg.stdout,bufsize=bufsize,ratelimit=ratelimit)
                        barprg_supervisor = Thread(target=lambda: queue_of_killables.put((barprg, barprg.wait())))
                        barprg_supervisor.start()
                        sndprg.stdout.close()
            except OSError:
                        os.kill(sndprg.pid,15)
                        raise
        else:
            barprg = sndprg

        try:
                        rcvprg = dst_conn.receive(d,pipe=barprg.stdout,opts=["-Fu"]+receive_opts,bufsize=bufsize,compression=compression)
                        rcvprg_supervisor = Thread(target=lambda: queue_of_killables.put((rcvprg, rcvprg.wait())))
                        rcvprg_supervisor.start()
                        barprg.stdout.close()
        except OSError:
                os.kill(sndprg.pid, 15)
                if sndprg.pid != barprg.pid: os.kill(barprg.pid, 15)
                raise

        dst_conn._dirty = True
        allprocesses = set([rcvprg, sndprg]) | ( set([barprg]) if showprogress else set() )
        while allprocesses:
            diedprocess, retcode = queue_of_killables.get()
            allprocesses = allprocesses - set([diedprocess])
            if retcode != 0:
                [ p.kill() for p in allprocesses ]
                raise subprocess.CalledProcessError(retcode, diedprocess._saved_args)