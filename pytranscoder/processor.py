import subprocess
import sys
from pathlib import PurePath
from typing import Optional

from pytranscoder.media import MediaInfo


class Processor:

    def __init__(self, path: str):
        self.path = path
        self.log_path: PurePath = None
        self.last_command = ''

    @property
    def is_available(self) -> bool:
        return self.path is not None

    def is_ffmpeg(self) -> bool:
        return False

    def is_hbcli(self) -> bool:
        return False

    def fetch_details(self, _path: str) -> MediaInfo:
        return None

    def run(self, params, event_callback) -> Optional[int]:
        return None

    def run_remote(self, sshcli: str, user: str, ip: str, params: list, event_callback) -> Optional[int]:
        return None

    def execute_and_monitor(self, params, event_callback, monitor) -> Optional[int]:
        self.last_command = ' '.join([self.path, *params])
        with subprocess.Popen([self.path,
                               *params],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT,
                              universal_newlines=True,
                              shell=False) as p:

            for stats in monitor(p):
                if event_callback is not None:
                    veto = event_callback(stats)
                    if veto:
                        p.kill()
                        return None
            return p.returncode

    def remote_execute_and_monitor(self, sshcli: str, user: str, ip: str, params: list, event_callback, monitor) -> Optional[int]:
        cli = [sshcli, '-v', user + '@' + ip, self.path, *params]
        self.last_command = ' '.join(cli)
        with subprocess.Popen(cli,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT,
                              universal_newlines=True,
                              shell=False) as p:
            try:
                for stats in monitor(p):
                    if event_callback is not None:
                        veto = event_callback(stats)
                        if veto:
                            p.kill()
                            return None
                return p.returncode
            except KeyboardInterrupt:
                p.kill()

    def popen(self, params):
        with subprocess.Popen(params,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT,
                              universal_newlines=False,
                              shell=False) as p:
            return p.communicate()

    def check_result(self, res):
        if isinstance(res, (tuple, list)):
            if res:
                res = self.check_result(res[0])
        return res

    def popen_concatenated(self, procs):
        result = ''
        for proc in procs:
            if result:
                result = self.check_result(result)
            result = [self.popen(proc + [result])]
            if result and isinstance(result, str) and result.startswith('(b\\'):
                result = result[3:-1]
        return result

    @staticmethod
    def parse_demuxer_list(demuxers_list):
        string = demuxers_list[0][0].decode("utf-8")
        return [line.split()[1] for line in string.replace('\n','#').split('#')[4:] if line and line.split()[1]]

    def get_all_extensions(self):

        params_str =u"-demuxers; -hide_banner; |; tail; -n; +5; |; cut -d' '-f4; |; xargs -i{}; ffmpeg; -hide_banner; -h; demuxer={}; |; grep; 'Common extensions' |; cut -d' ' -f7; |; tr ',' $'\n'; |; tr -d '.'"
        # params = [arg.strip() for arg in params_str.split(';')]
        hide_b_param = ['ffmpeg','-demuxers','-hide_banner']
        procs = [hide_b_param]
        # tail_param = ['tail', '-n', '+5']
        # cut_f4_param = ['cut', '-d', "' '", '-f4']
        # xargs_param = ['xargs', '-i{}', 'ffmpeg', "-hide_banner", "-h", "demuxer={}"]
        # comm_ext_param = ['grep', "'Common extensions'"]
        # cut_f7_param = cut_f4_param[:]
        # cut_f7_param[-1] = '-f7'
        # tr_1_param = ['tr', "','", "$'\\n'"]
        # tr_2_param = ['tr', "-d", "."]
        # procs = [hide_b_param, tail_param, cut_f4_param, xargs_param, comm_ext_param, cut_f7_param, tr_1_param, tr_2_param]

        return self.parse_demuxer_list(self.popen_concatenated(procs))
