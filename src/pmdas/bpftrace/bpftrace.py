#
# Copyright (c) 2019 Red Hat.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#
"""bpftrace"""

import subprocess
import signal
import re
from threading import Thread, Lock
from copy import deepcopy
import json
from cpmapi import PM_SEM_INSTANT, PM_SEM_COUNTER, PM_TYPE_U64, PM_TYPE_STRING


class BPFtraceError(Exception):
    """BPFtrace general error"""

class BPFtraceState: # pylint: disable=too-few-public-methods
    """BPFtrace state"""
    def __init__(self):
        self.status = 'stopped' # stopped|starting|started|stopping
        self.reset()

    def reset(self):
        """reset state"""
        self.pid = None
        self.exit_code = None
        self.output = ''
        self.probes = 0
        self.maps = {}

    def __str__(self):
        return str(self.__dict__)

class BPFtraceVarDef: # pylint: disable=too-few-public-methods
    """BPFtrace variable definitions"""
    def __init__(self, single, semantics, datatype):
        self.single = single
        self.semantics = semantics
        self.datatype = datatype

    def __str__(self):
        return str(self.__dict__)

class BPFtrace:
    """class for interacting with bpftrace"""
    def __init__(self, log, script):
        self.log = log
        self.script = script # contains the modified script (added continuous output)

        self.metadata = {}
        self.var_defs = {}
        self.lock = Lock()
        self._state = BPFtraceState()

        self.parse_script()

    def state(self):
        """returns latest state"""
        with self.lock:
            return deepcopy(self._state)

    def process_output_obj(self, obj):
        """process a single JSON object from bpftrace output"""
        with self.lock:
            if self._state.status == 'starting':
                self._state.status = 'started'

            if obj['type'] == 'attached_probes':
                self._state.probes = obj['probes']
            elif obj['type'] == 'map':
                self._state.maps.update(obj['data'])
            elif obj['type'] == 'hist':
                for k, v in obj['data'].items():
                    self._state.maps[k] = {
                        '{}-{}'.format(bucket.get('min', 'inf'), bucket.get('max', 'inf'))
                        :bucket['count']
                        for bucket in v
                    }
            elif obj['type'] in ['printf', 'time']:
                self._state.maps['@printf'] = self._state.maps.get('@printf', '') + obj['msg']

    def process_output(self):
        """process stdout and stderr of running bpftrace process"""
        for line in self.process.stdout:
            if not line or line.isspace():
                continue
            try:
                obj = json.loads(line)
                self.process_output_obj(obj)
            except ValueError:
                with self.lock:
                    self._state.output += line

        # process has exited, set returncode
        self.process.poll()
        with self.lock:
            self._state.status = 'stopped'
            self._state.exit_code = self.process.returncode

    def parse_script(self):
        """parse bpftrace script (read variable semantics, add continuous output)"""
        metadata = re.findall(r'^// (\w+): (.+)$', self.script, re.MULTILINE)
        for key, val in metadata:
            if key == 'name':
                if re.match(r'^[a-zA-Z_]\w+$', val):
                    self.metadata['name'] = val
                else:
                    raise BPFtraceError("invalid value '{}' for script name: must contain only "
                                        "alphanumeric characters and start with a letter".format(
                                            val))
            elif key == 'include':
                self.metadata['include'] = val.split(',')

        self.var_defs = {}
        variables = re.findall(r'(@.*?)(\[.+?\])?\s*=\s*(count|hist)?', self.script)
        if variables:
            for var, key, func in variables:
                if 'include' in self.metadata and var not in self.metadata['include']:
                    continue

                vardef = BPFtraceVarDef(single=True, semantics=PM_SEM_INSTANT, datatype=PM_TYPE_U64)
                if func in ['hist', 'lhist']:
                    vardef.single = False
                    vardef.semantics = PM_SEM_COUNTER
                if func == 'count':
                    vardef.semantics = PM_SEM_COUNTER
                if key:
                    vardef.single = False
                self.var_defs[var] = vardef

            print_st = ' '.join(['print({});'.format(var) for var in self.var_defs])
            self.script = self.script + ' interval:s:1 {{ {} }}'.format(print_st)

        printfs = re.search(r'printf\s*\(', self.script)
        if printfs and ('include' not in self.metadata or '@printf' in self.metadata['include']):
            self.var_defs['@printf'] = BPFtraceVarDef(single=True, semantics=PM_SEM_INSTANT,
                                                      datatype=PM_TYPE_STRING)

        if not self.var_defs:
            raise BPFtraceError("no bpftrace variables or printf statements found, please include "
                                "at least one variable or print statement in your script")

    def start(self):
        """starts bpftrace in the background and reads its stdout in a new thread"""
        with self.lock:
            if self._state.status != 'stopped':
                raise BPFtraceError("cannot start bpftrace, current status: {}".format(
                    self._state.status))
            self._state.reset()
            self._state.status = 'starting'

        self.process = subprocess.Popen(['bpftrace', '-f', 'json', '-e', self.script],
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        encoding='utf8')

        # with daemon=False, atexit doesn't work
        self.process_output_thread = Thread(target=self.process_output, daemon=True)
        self.process_output_thread.start()

        self.log("started bpftrace -e '{}', PID: {}".format(self.script, self.process.pid))
        with self.lock:
            # status will be set to 'started' once the first data arrives
            self._state.pid = self.process.pid

    def stop(self, wait=False):
        """stop bpftrace process"""
        with self.lock:
            if self._state.status != 'started':
                raise BPFtraceError("cannot stop bpftrace, current status: {}".format(
                    self._state.status))
            self._state.status = 'stopping'

        self.log("stopped bpftrace PID {}, wait for termination: {}".format(self.process.pid, wait))
        self.process.send_signal(signal.SIGINT)

        if wait:
            self.process.communicate()
        else:
            self.process.poll()

        with self.lock:
            self._state.status = 'stopped'
            self._state.exit_code = self.process.returncode
