#!/usr/bin/python3
import datetime
import glob
import os
import shutil
import sys
from pathlib import Path, PurePath
from typing import Set, List, Optional

import logging

from queue import Queue, Empty
from threading import Thread, Lock
import crayons

import pytranscoder

from pytranscoder import __version__
from pytranscoder.cluster import manage_clusters
from pytranscoder.config import ConfigFile
from pytranscoder.media import MediaInfo
from pytranscoder.profile import Profile
from pytranscoder.utils import get_files, filter_threshold, files_from_file,\
                                calculate_progress, dump_stats, get_sizes,\
                                get_diff_size, get_size_text, remove_duplicates, getsize, auto_convert_unit
from traceback import format_exc

the_main_filename = sys.argv[0]
MAIN_DIR = os.path.dirname(the_main_filename)
config_path = os.path.join(MAIN_DIR, 'trascodes', 'transcode.yml')
if not os.path.exists(config_path):
    import platform
    system = platform.system()
    if system not in ['Linux']: #todo add mcosx
        config_path = os.path.join(*sys.executable.split(os.sep)[:-2], 'share', 'doc', 'pytranscoder', 'transcode.yml')
    else:
        config_path = os.path.join('/'.join(sys.executable.split(os.sep)[:-2]), 'share', 'doc', 'pytranscoder', 'transcode.yml')
# DEFAULT_CONFIG = os.path.expanduser('~/.transcode.yml')
if not os.path.exists(config_path):
    import logging
    logger = logging.getLogger('pytranscode.py')
    logger.critical(f'missing configuration file: {config_path}')
    sys.exit(-1)

DEFAULT_CONFIG = config_path
DEFAULT_PROCESSES_SUFFIX_SEPARATOR = '_'
DEFAULT_PROCESSED_SUFFIX = f'{DEFAULT_PROCESSES_SUFFIX_SEPARATOR}cuda'
class LocalJob:
    """One file with matched profile to be encoded"""

    def __init__(self, inpath: str, profile: Profile, mixins: List[str], info: MediaInfo):
        self.inpath = Path(os.path.abspath(inpath))
        self.profile = profile
        self.info = info
        self.mixins = mixins


class QueueThread(Thread):
    """One transcoding thread associated to a queue"""

    def __init__(self, queuename, queue: Queue, configfile: ConfigFile, manager):
        """
        :param queuename:   Name of the queue, for thread naming purposes only
        :param queue:       Thread-safe queue containing files to be encoded
        :param configfile:  Instance of the parsed configuration (transcode.yml)
        :param manager:     Reference to object that manages this thread
        """
        super().__init__(name=queuename, group=None, daemon=True)

        self.queue = queue
        self.config = configfile
        self._manager = manager
        self.basename = ''
        # self.total_orig_size, self.total_new_size, self.total_session_time = 0, 0, datetime.timedelta()
        self.total_orig_size, self.total_new_size, self.total_session_time = self.init_stats()

    @property
    def lock(self):
        return self._manager.lock

    def complete(self, path: Path, elapsed_seconds):
        self._manager.complete.append((str(path), elapsed_seconds))

    def start_test(self):
        self.go()

    def run(self):
        self.go()

    def log(self, logger, logger_func, message, flush=False, only_console=False):
        self.lock.acquire()

        if only_console:
            logger.debug(message)
        else:
            logger_func(message)
        if flush:
            sys.stdout.flush()
        self.lock.release()


    def init_stats(self):
        total_orig_size, total_new_size, total_session_time = 0, 0, datetime.timedelta()
        return total_orig_size, total_new_size, total_session_time

    def go(self):

        logger = None
        while not self.queue.empty():
            self.total_orig_size, self.total_new_size, self.total_session_time = self.init_stats()
            errors = []
            files_to_remove = []
            originals_to_remove = []
            errors = []
            orig_size = new_size  = 0
            session_time = datetime.timedelta()
            job = outpath = None
            try:
                job: LocalJob = self.queue.get()
            except Exception as err:
                if logger is None:
                    logger = logging.getLogger('Spoiling queue')
                    logger.warning('The queue is empty')
                    break
            try:
                self.basename = basename = job.inpath.name
                input_opt = job.profile.input_options.as_shell_params()
                output_opt = self.config.output_from_profile(job.profile, job.mixins)
                logger = logging.getLogger(f'Process {self.basename}')
                keep_orig = self.config.keep_orig()
                if self.config.tmp_dir():
                    # lets write output to local storage, for efficiency
                    outpath = PurePath(self.config.tmp_dir(), job.inpath.with_suffix(job.profile.extension).name)
                    fls = True
                else:
                    outpath = job.inpath.with_suffix(job.profile.extension + '.tmp')

                #
                # check if we need to exclude any streams
                #
                processor = self.config.get_processor_by_name(job.profile.processor)
                if job.profile.is_ffmpeg:
                    if job.info.is_multistream() and self.config.automap and job.profile.automap:
                        output_opt = output_opt + job.info.ffmpeg_streams(job.profile)
                    # cli = ['-y', *input_opt, '-i', str(job.inpath), *output_opt, str(outpath)]
                    overwrite_flag = '-y'
                    # overwrite_flag = '-y' if keep_orig else '-n'
                    cli = [ overwrite_flag, '-i', str(job.inpath), *input_opt,  str(outpath), *output_opt]
                else:
                    cli = ['-i', str(job.inpath), *input_opt, *output_opt, '-o', str(outpath)]
                try:
                    self.log(logger, logger.info, f"Profile  : {job.profile.name} {'{:<6}   : '.format(job.profile.processor) + ' '.join(cli)}")
                except Exception as err:
                    self.log(logger, logger.critical, f'{err}')

                if pytranscoder.dry_run:
                    continue



                def log_callback(stats):
                    pct_done, pct_comp = calculate_progress(job.info, stats)
                    pytranscoder.status_queue.put({ 'host': 'local',
                                                    'file': basename,
                                                    'speed': stats['speed'],
                                                    'comp': pct_comp,
                                                    'done': pct_done})

                    self.log(logger, logger.info, f'speed: {stats["speed"]}x, comp: {pct_comp}%, done: {pct_done:3}%', only_console=True)
                    if pct_comp < 0 and pct_done > 5:
                        self.log(logger, logger.warning,
                                 f'Encoding of {basename} cancelled and skipped due negative compression ratio')
                        return True
                    if job.profile.threshold_check < 100:
                        if pct_done >= job.profile.threshold_check and pct_comp < job.profile.threshold:
                            # compression goal (threshold) not met, kill the job and waste no more time...
                            self.log(logger, logger.warning, f'Encoding of {basename} cancelled and skipped due to threshold not met')
                            return True
                    return False

                def hbcli_callback(stats):
                    self.log(logger, logger.info, f'{basename}: avg fps: {stats["fps"]}, ETA: {stats["eta"]}')
                    return False

                def add_processed_suffix(_output):
                    base, ext = os.path.splitext(_output)
                    suffix = self.config.settings.get('completed_suffix', DEFAULT_PROCESSED_SUFFIX)
                    base += suffix

                    return PurePath(base + ext)

                orig_size = getsize(job.inpath)
                self.log(logger, logger.info,
                         f'Original size: {auto_convert_unit(orig_size, text=True)}')

                job_start = datetime.datetime.now()
                if processor.is_ffmpeg():
                    code = processor.run(cli, log_callback)
                else:
                    code = processor.run(cli, hbcli_callback)
                job_stop = datetime.datetime.now()
                elapsed = job_stop - job_start

                if code == 0:
                    if not filter_threshold(job.profile, str(job.inpath), outpath):
                        # oops, this transcode didn't do so well, lets keep the original and scrap this attempt
                        self.log(logger, logger.warning, f'Transcoded file {job.inpath} did not meet minimum savings threshold, skipped')
                        self.complete(job.inpath, (job_stop - job_start).seconds)
                        self.log(logger, logger.info, f'completed: {job.inpath} in {(job_stop - job_start).seconds}')
                        files_to_remove.append(str(outpath))
                        # os.unlink(str(outpath))
                        self.log(logger, logger.info, f'{outpath} removed')
                        continue

                    self.complete(job.inpath, elapsed.seconds)
                    session_time = datetime.timedelta(seconds=elapsed.seconds)
                    destination = self.config.dest_dir()
                    if destination:
                        try:
                            os.makedirs(destination,exist_ok=True)
                        except Exception as err:
                            self.log(logger, logger.error,str(err))
                            self.log(logger, logger.warning, f'The destination folder {destination} does not exist and can not be created')
                            self.log(logger, logger.info, f'Changing the invalid destination folder to the temp output {self.config.tmp_dir()}')
                            destination = outpath.parent
                        if destination == os.path.dirname(job.inpath):
                            if keep_orig:
                                completed_path = add_processed_suffix(os.path.join(destination, os.path.basename(
                                    job.inpath.with_suffix(job.profile.extension))))
                            else:
                                completed_path = os.path.join(destination,
                                                              os.path.basename(
                                                                  job.inpath.with_suffix(job.profile.extension)))
                        else:
                            completed_path = os.path.join(destination,
                                                          os.path.basename(
                                                              job.inpath.with_suffix(job.profile.extension)))
                            if not keep_orig:
                                originals_to_remove.append(job.inpath)
                                # job.inpath.unlink(missing_ok=True)
                                self.log(logger, logger.info, f'ORIGINAL IS GOING TO BE REMOVED')

                    else:
                        if keep_orig:
                            completed_path = add_processed_suffix( job.inpath.with_suffix(job.profile.extension))
                        else:
                            completed_path = job.inpath.with_suffix(job.profile.extension)
                            self.log(logger, logger.info, f'ORIGINAL OVERWRITTEN')
                    new_size = getsize(outpath)
                    diff_size = get_diff_size(orig_size, new_size)
                    if not os.path.exists(os.path.dirname(completed_path)):
                        try:
                            os.makedirs(os.path.dirname(completed_path))
                            self.log(logger, logger.info, f'The destination folder has been created {os.path.dirname(completed_path)}')
                        except Exception as err:
                            self.log(logger, logger.critical, str(err))
                    #     os.remove(completed_path)
                    shutil.move(str(outpath), str(completed_path))
                    self.log(logger, logger.info, f'{outpath} moved to {completed_path}')
                            # outpath.rename(job.inpath.with_suffix(job.profile.extension))
                    self.log(logger, logger.info,
                         f'New size: {auto_convert_unit(new_size, text=True)}')
                    self.total_orig_size += orig_size
                    self.total_new_size += new_size

                    self.log(logger, logger.info, crayons.yellow(f'Finished {outpath}, {"original file unchanged" if keep_orig else ""}'))
                    self.log(logger, logger.info, f'{get_size_text(diff_size)} {"SAVED" if int(diff_size[0]) >= 0 else "LOOSE"}')

                elif code is not None:
                    self.log(logger, logger.critical, f' Did not complete normally: {processor.last_command}')
                    self.log(logger, logger.info, f'Output can be found in {processor.log_path}')
                    try:
                        outpath.unlink()

                        self.log(logger, logger.info, f'{outpath} removed')

                    except Exception as err:
                        self.log(logger, logger.warning, f'{outpath} NOT removed')
                        self.log(logger, logger.error, f'{err}')
                else:
                    self.log(logger, logger.warning,f'{self.basename} aborted')
                    try:
                        os.unlink(outpath)
                        self.log(logger, logger.warning, f'{outpath} removed')
                    except Exception as err:
                        self.log(logger, logger.error, f'error {outpath} not removed')


            except Exception as err:
                stack = format_exc()
                if logger is not None:
                    logger = logging.getLogger(__name__)
                self.log(logger, logger.debug, f'{stack}', only_console=True)
                errors.append(err)
            finally:
                errors.extend(errors)
                self.total_session_time += session_time
                try:
                    # orig_size, new_size = get_sizes(job.inpath, completed_path)
                    if not errors:
                        errors = self.removing_files(originals_to_remove, logger, errors)
                    self.removing_files(files_to_remove, logger, errors)
                    errors = remove_duplicates(errors)
                except Exception as err:
                    stack = format_exc()
                    self.log(logger, logger.debug, f'{stack}', only_console=True)
                    errors.append(err)

                self.queue.task_done()





    def removing_files(self, files, logger, errors):
        for _file in files:
            try:
                os.unlink(str(_file))
                self.log(logger, logger.info, f'actually removing {_file}')
            except Exception as err:
                stack = format_exc()
                self.log(logger, logger.debug, f'{stack}', only_console=True)
                self.log(logger, logger.critical, str(err))
                errors.append(err)
        return errors

class LocalHost:
    """Encapsulates functionality for local encoding"""

    lock:       Lock = Lock()
    complete:   List = list()            # list of completed files, shared across threads

    def __init__(self, configfile: ConfigFile):
        self.queues = dict()
        self.configfile = configfile
        self.total_orig_size, self.total_new_size, self.total_session_time, self.errors = 0, 0, datetime.timedelta(), []
        #
        # initialize the queues
        #
        self.queues['_default_'] = Queue()
        for qname in configfile.queues.keys():
            self.queues[qname] = Queue()

    def start(self):
        """After initialization this is where processing begins"""
        #
        # all files are listed in the queues so start the threads
        #
        self.total_orig_size, self.total_new_size, self.total_session_time, self.errors = 0, 0, datetime.timedelta(), []
        jobs = list()
        for name, queue in self.queues.items():

            # determine the number of threads to allocate for each queue, minimum of defined max and queued jobs

            if name == '_default_':
                concurrent_max = 1
            else:
                concurrent_max = min(self.configfile.queues[name], queue.qsize())

            #
            # Create (n) threads and assign them a queue
            #
            for _ in range(concurrent_max):
                t = QueueThread(name, queue, self.configfile, self)
                jobs.append(t)
                t.start()

        busy = True
        while busy:
            try:
                report = pytranscoder.status_queue.get(block=True, timeout=2)
                basename = report['file']
                speed = report['speed']
                comp = report['comp']
                done = report['done']

                # self.lock.acquire()
                # # print(f'{basename}: speed: {speed}x, comp: {comp}%, done: {done:3}%')
                # sys.stdout.flush()
                # self.lock.release()
                pytranscoder.status_queue.task_done()
            except Empty:
                busy = False
                for job in jobs:
                    if job.is_alive():
                        busy = True
                    else:
                        self.total_orig_size += job.total_orig_size
                        self.total_new_size += job.total_new_size
                        self.total_session_time += job.total_session_time
                        # self.errors.extend(job.errors)

        # wait for all queues to drain and all jobs to complete
#        for _, queue in self.queues.items():
#            queue.join()

    def enqueue_files(self, files: list):
        """Add requested files to the appropriate queue

        :param files: list of (path,profile) tuples
        :return:
        """
        logger = logging.getLogger('Enqueue files')
        number_files_added = 0
        for path, forced_profile, mixins in files:
            #
            # do some prechecks...
            #
            if forced_profile is not None and not self.configfile.has_profile(forced_profile):
                # print(f'profile "{forced_profile}" referenced from command line not found')
                logger.critical(f'profile "{forced_profile}" referenced from command line not found')
                sys.exit(1)

            if len(path) == 0:
                continue

            if not os.path.isfile(path):
                # print(crayons.red('file not found, skipping: ' + path))
                logger.critical(f'File not found: {path}')
                continue

            processor_name = 'ffmpeg'

            if forced_profile:
                the_profile = self.configfile.get_profile(forced_profile)
                if not the_profile.is_ffmpeg:
                    processor_name = 'hbcli'

            processor = self.configfile.get_processor_by_name(processor_name)
            media_info = processor.fetch_details(path)

            if media_info is None:
                logger.critical(f'File not found: {path}')
                # print(crayons.red(f'File not found: {path}'))
                continue

            if media_info.valid:
                #logger.debug(str(media_info)) # todo need __str__ in to that class....

                if forced_profile is None:
                    rule = self.configfile.match_rule(media_info)
                    if rule is None:
                        # print(crayons.green(os.path.basename(path)), crayons.yellow(f'No matching profile found - skipped'))
                        logger.info(f'No matching profile found - skipped')
                        continue
                    if rule.is_skip():
                        # print(crayons.green(os.path.basename(path)), f'SKIPPED ({rule.name})')
                        logger.info(f'SKIPPED ({rule.name})')
                        self.complete.append((path, 0))
                        continue
                    profile_name = rule.profile
                else:
                    #
                    # looks good, add this file to the thread queue
                    #
                    profile_name = forced_profile

                the_profile = self.configfile.get_profile(profile_name)
                qname = the_profile.queue_name
                if pytranscoder.verbose:
                    print('Matched with profile {profile_name}')
                if qname is not None:
                    if not self.configfile.has_queue(the_profile.queue_name):
                        print(crayons.red(
                            f'Profile "{profile_name}" indicated queue "{qname}" that has not been defined')
                        )
                        logger.critical(f'Profile "{profile_name}" indicated queue "{qname}" that has not been defined')
                        sys.exit(1)
                    else:
                        self.queues[qname].put(LocalJob(path, the_profile, mixins, media_info))
                        number_files_added += 1
                        if pytranscoder.verbose:
                            print('Added to queue {qname}')
                            logger.debug(f'Added to queue {qname}')
                else:
                    self.queues['_default_'].put(LocalJob(path, the_profile, mixins, media_info))
                    number_files_added += 1
        return number_files_added


def cleanup_queuefile(queue_path: str, completed: Set):
    if not pytranscoder.dry_run and queue_path is not None:
        # pick up any newly added files
        files = set(files_from_file(queue_path))
        # subtract out the ones we've completed
        files = files - completed
        if len(files) > 0:
            # rewrite the queue file with just the pending ones
            with open(queue_path, 'w') as f:
                for path in files:
                    f.write(path + '\n')
        else:
            # processed them all, just remove the file
            try:
                os.remove(queue_path)
            except FileNotFoundError:
                pass


def install_sigint_handler():
    import signal
    import sys

    def signal_handler(signal, frame):
        print('Process terminated')
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)


def main(cmd_line=True, folder_path=None):
    if not cmd_line and folder_path:
        return start(folder_path)
    else:
        return start_cmd_line()

def start(path):
    install_sigint_handler()
    configfile = ConfigFile(DEFAULT_CONFIG)
    # files = get_files(path, configfile)
    queue_path = configfile.default_queue_file
    if not queue_path:
        queue_path = '/tmp/py_encoder.txt'
    if not configfile.colorize:
        crayons.disable()
    else:
        crayons.enable()
    total_orig_size, total_new_size, total_session_time, errors = host_start(configfile, path, queue_path)
    # assert total_orig_size ==
    return total_orig_size, total_new_size, total_session_time, errors



def start_cmd_line():
    # print(get_files('/home/gorilla/Scaricati', ConfigFile(DEFAULT_CONFIG)))
    if len(sys.argv) == 2 and sys.argv[1] == '-h':
        print(f'pytrancoder (ver {__version__})')
        print('usage: pytrancoder [OPTIONS]')
        print('  or   pytrancoder [OPTIONS] --from-file <filename>')
        print('  or   pytrancoder [OPTIONS] file ...')
        print('  or   pytrancoder -c <cluster> file... [--host <name>] -c <cluster> file...')
        print('No parameters indicates to process the default queue files using profile matching rules.')
        print(
            'The --from-file filename is a file containing a list of full paths to files for transcoding. ')
        print('OPTIONS:')
        print('  --host <name>  Name of a specific host in your cluster configuration to target, otherwise load-balanced')
        print('  -s         Process files sequentially even if configured for multiple concurrent jobs')
        print('  --dry-run  Run without actually transcoding or modifying anything, useful to test rules and profiles')
        print('  -v         Verbose output, helpful in debugging profiles and rules')
        print(
            '  -k         Keep source files after transcoding. If used, the transcoded file will have the same '
            'name and .tmp extension')
        print('  -y <file>  Full path to configuration file.  Default is ~/.transcode.yml')
        print('  -p         profile to use. If used with --from-file, applies to all listed media in <filename>')
        print('  -m         Add mixins to profile. Separate multiples with a comma')
        print('\n** PyPi Repo: https://pypi.org/project/pytranscoder-ffmpeg/')
        print('** Read the docs at https://pytranscoder.readthedocs.io/en/latest/')
        sys.exit(0)

    install_sigint_handler()
    files = list()
    profile = None
    mixins = None
    queue_path = None
    cluster = None
    configfile: Optional[ConfigFile] = None
    host_override = None
    if len(sys.argv) > 1:
        files = []
        arg = 1
        while arg < len(sys.argv):
            if sys.argv[arg] == '--from-file':          # load filenames to encode from given file
                queue_path = sys.argv[arg + 1]
                arg += 1
                tmpfiles = files_from_file(queue_path)
                if cluster is None:
                    files.extend([(f, profile) for f in tmpfiles])
                else:
                    files.extend([(f, cluster) for f in tmpfiles])
            elif sys.argv[arg] == '-p':                 # specific profile
                profile = sys.argv[arg + 1]
                arg += 1
            elif sys.argv[arg] == '-y':                 # specify yaml config file
                arg += 1
                configfile = ConfigFile(sys.argv[arg])
            elif sys.argv[arg] == '-k':                 # keep original
                pytranscoder.keep_source = True
            elif sys.argv[arg] == '--dry-run':
                pytranscoder.dry_run = True
            elif sys.argv[arg] == '--host':             # run all cluster encodes on specific host
                host_override = sys.argv[arg + 1]
                arg += 1
            elif sys.argv[arg] == '-v':                 # verbose
                pytranscoder.verbose = True
            elif sys.argv[arg] == '-c':                 # cluster
                cluster = sys.argv[arg + 1]
                arg += 1
            elif sys.argv[arg] == '-m':                 # mixins
                mixins = sys.argv[arg + 1].split(',')
                arg += 1
            else:
                if os.name == "nt":
                    expanded_files: List = glob.glob(sys.argv[arg])     # handle wildcards in Windows
                else:
                    expanded_files = [sys.argv[arg]]
                for f in expanded_files:
                    if cluster is None:
                        files.append((f, profile, mixins))
                    else:
                        files.append((f, cluster, profile, mixins))
            arg += 1

    if configfile is None:
        configfile = ConfigFile(DEFAULT_CONFIG)

    if not configfile.colorize:
        crayons.disable()
    else:
        crayons.enable()

    if len(files) == 0 and queue_path is None and configfile.default_queue_file is not None:
        #
        # load from list of files
        #
        tmpfiles = files_from_file(configfile.default_queue_file)
        queue_path = configfile.default_queue_file
        if cluster is None:
            files.extend([(f, profile, mixins) for f in tmpfiles])
        else:
            files.extend([(f, cluster, profile) for f in tmpfiles])

    if len(files) == 0:
        print(crayons.yellow(f'Nothing to do'))
        sys.exit(0)

    if cluster is not None:
        if host_override is not None:
            # disable all other hosts in-memory only - to force encodes to the designated host
            cluster_config = configfile.settings['clusters']
            for cluster in cluster_config.values():
                for name, this_config in cluster.items():
                    if name != host_override:
                        this_config['status'] = 'disabled'
        completed: List = manage_clusters(files, configfile)
        if len(completed) > 0:
            qpath = queue_path if queue_path is not None else configfile.default_queue_file
            pathlist = [p for p, _ in completed]
            cleanup_queuefile(qpath, set(pathlist))
            dump_stats(completed)
        sys.exit(0)


    total_orig_size, total_new_size, total_session_time, errors = host_start(configfile, files, queue_path)
    return total_orig_size, total_new_size, total_session_time, errors

def host_start(configfile, path, queue_path):
    if os.path.isdir(path):
        files = get_files(path, configfile)
        logger = logging.getLogger(f'Folder: {path}')
    else:
        logger = logging.getLogger(f'From cmd line')
    host = LocalHost(configfile)
    number_files_added = host.enqueue_files(files)
    logger.info(f'Number of files {number_files_added}')
    #
    # start all threads and wait for work to complete
    #
    host.start()
    if len(host.complete) > 0:
        completed_paths = [p for p, _ in host.complete]
        cleanup_queuefile(queue_path, set(completed_paths))
        dump_stats(host.complete)
    return host.total_orig_size, host.total_new_size, host.total_session_time, host.errors
    # os.system("stty sane")


if __name__ == '__main__':
    start_cmd_line()
