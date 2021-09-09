
import math
import os
import platform
import subprocess
from typing import Dict, List
from functools import wraps
import pytranscoder
from pytranscoder.media import MediaInfo
from pytranscoder.profile import Profile


def filter_threshold(profile: Profile, inpath, outpath):
    if profile.threshold > 0:
        orig_size = os.path.getsize(inpath)
        new_size = os.path.getsize(outpath)
        return is_exceeded_threshold(profile.threshold, orig_size, new_size)
    return True


def is_exceeded_threshold(pct_threshold: int, orig_size: int, new_size: int) -> bool:
    pct_savings = 100 - math.floor((new_size * 100) / orig_size)
    if pct_savings < pct_threshold:
        return False
    return True


def files_from_file(queuepath) -> list:
    if not os.path.exists(queuepath):
        print(f'Queue file {queuepath} not found')
        return []
    with open(queuepath, 'r') as qf:
        _files = [fn.rstrip() for fn in qf.readlines()]
        return _files


def get_local_os_type():
    return {'Windows': 'win10', 'Linux': 'linux', 'Darwin': 'macos'}.get(platform.system(), 'unknown')


def calculate_progress(info: MediaInfo, stats: Dict) -> (int, int):
    # pct done calculation only works if video duration >= 1 minute
    if info.runtime > 0:
        pct_done = int((stats['time'] / info.runtime) * 100)
    else:
        pct_done = 0

    # extrapolate current compression %

    filesize = info.filesize_mb * 1024000
    pct_source = int(filesize * (pct_done / 100.0))
    if pct_source <= 0:
        return 0, 0
    pct_dest = int((stats['size'] / pct_source) * 100)
    pct_comp = 100 - pct_dest

    return pct_done, pct_comp


def run(cmd):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False)
    output = p.communicate()[0].decode('utf-8')
    return p.returncode, output


def dump_stats(completed):

    if pytranscoder.dry_run:
        return

    paths = [p for p, _ in completed]
    max_width = len(max(paths, key=len))
    print("-" * (max_width + 9))
    for path, elapsed in completed:
        pathname = path.rjust(max_width)
        _min = int(elapsed / 60)
        _sec = int(elapsed % 60)
        print(f"{pathname}  ({_min:3}m {_sec:2}s)")
    print()


def add_files_from_dir(queue_file, dirpath):
    if os.path.exists(dirpath):
        with open(queue_file, 'w') as f:
            f.writelines(get_files(dirpath))

def get_files(dirpath, config):
    files = []
    recursive = config.settings.get('recursive', False)
    if os.path.exists(dirpath):
        if os.path.isdir(dirpath):
            from glob import glob
            if recursive:
                os_func = lambda _path: os.walk(_path)
            else:
                os_func = lambda _path: os.listdir(_path)
            exts = list({f'*{profile.extension}' for profile in config.profiles.values() if profile.extension is not None})
            glob_list = lambda path: [new_path for ext in exts for new_path in glob(os.path.join(path[0], ext))]
            files = [(_file, None, None) for _path in os_func(dirpath) for _file in
                     glob_list(_path) if os.path.isfile(_file)]
        elif os.path.isfile(dirpath):
            files = [(dirpath, None, None)]
        return files

