
import math
import os
import platform
import subprocess
from typing import Dict, List
from functools import wraps
import pytranscoder
from pytranscoder.media import MediaInfo
from pytranscoder.profile import Profile
from exceptions import ErrorSizeTextConversion, DoesNotExistFilePath, ErrorEmptyFilePath
from enums.Enums import SizeUnit


def filter_threshold(profile: Profile, inpath, outpath):
    if profile.threshold > 0:
        orig_size, new_size = get_sizes(inpath, outpath)
        return is_exceeded_threshold(profile.threshold, orig_size, new_size)
    return True


def get_sizes(inpath, outpath):
    return getsize(inpath), getsize(outpath)

def getsize(_file):
    _size = os.path.getsize(_file)
    return _size

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

def remove_duplicates(lst):
    """preserve order. list(set()) won't preserve order"""
    tmp = []
    for x in lst:
        if x not in tmp:
            tmp.append(x)
    return tmp

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


def add_files_from_dir(queue_file, dirpath, config):
    if os.path.exists(dirpath):
        with open(queue_file, 'w') as f:
            files = [_file[0] for _file in get_files(dirpath, config)]
            f.writelines(files)


def get_files(dirpath, config):
    files = []
    recursive = config.settings.get('recursive', False)
    if os.path.exists(dirpath):
        if os.path.isdir(dirpath):
            exts = list({f'*{profile.extension}' for profile in config.profiles.values() if profile.extension is not None})
            from glob import glob
            if recursive:
                glob_list = lambda path: [new_path for ext in exts for new_path in glob(os.path.join(path[0], ext))]
                files = [(_file, None, None) for _path in os.walk(dirpath) for _file in
                         glob_list(_path) if os.path.isfile(_file)]
            else:
                # os_func = lambda _path: os.listdir(_path)
                root, _, _ = next(os.walk(dirpath))
                files = [(_file, None, None)  for ext in exts for _file in glob(os.path.join(root, ext)) if os.path.isfile(_file)]

        else:
            files = [(dirpath, None, None)]
    return files

def get_overall_size(files):
    if os.path.exists(files):
        if os.path.isdir(files):
            total_size = 0
            for f in files:
                total_size += os.stat(f).st_size
            return auto_convert_unit(total_size)
        else:
            print(f'{files} is not a dir')
    else:
        print(f'{files} not exist')


def convert_unit(size_in_bytes, unit):
    """ Convert the size from bytes to other units like KB, MB or GB"""
    if not size_in_bytes:
        return size_in_bytes
    if unit == SizeUnit.KB:
        return size_in_bytes / 1000
        # return size_in_bytes / 1024
    elif unit == SizeUnit.MB:
        return size_in_bytes / (1000 * 1000)
        # return size_in_bytes / (1024 * 1024)
    elif unit == SizeUnit.GB:
        # return size_in_bytes / (1024 * 1024 * 1024)
        return size_in_bytes / (1000 * 1000 * 1000)
    else:
        return size_in_bytes


def auto_convert_unit(size_in_bytes, text=True):
    if isinstance(size_in_bytes, int):
        sizes = sorted([siz for siz in SizeUnit], reverse=True)
        for _size in sizes:
            result = convert_unit(size_in_bytes, _size)
            if size_in_bytes < 0 or not result:
                result_tuple = (0, SizeUnit(1))  # BYTES
                if text:
                    return get_size_text(result_tuple)
                else:
                    return result_tuple
            if result >= 1:
                result_tuple = round(result, 2), _size
                if text:
                    return get_size_text(result_tuple)
                else:
                    return result_tuple


        from exceptions import SizeNotConvertible
        raise SizeNotConvertible(size_in_bytes)
    else:
        from exceptions import WrongSizeType
        raise WrongSizeType(size_in_bytes)

def get_diff_size(old_size, new_size):
    return auto_convert_unit(old_size - new_size, text=False)

def get_size_text(_size: tuple):
    if len(_size) == 2:
        num, unit = _size
        if isinstance(num, (int, float)) and isinstance(unit, SizeUnit):
            return f'{round(num, 2)} {unit.name}'
        else:
            raise ErrorSizeTextConversion(_size)
    else:
        raise ErrorSizeTextConversion(_size)


if __name__ == '__main__':
    path = '/media/gorilla/Acer/Users/Lucio/dwhelper'
    files = [os.path.join(path, _file) for _file in os.listdir(path)]
    print(get_overall_size(files))
