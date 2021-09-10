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
from pytranscoder.utils import get_files, filter_threshold, files_from_file, calculate_progress, dump_stats, init_logger_strm


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
logger_progress_stream = init_logger_strm()
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

    @property
    def lock(self):
        return self._manager.lock

    def complete(self, path: Path, elapsed_seconds):
        self._manager.complete.append((str(path), elapsed_seconds))

    def start_test(self):
        self.go()

    def run(self):
        self.go()

    def log(self, logger, message, flush=False, only_console=False):
        self.lock.acquire()

        if only_console:
            file_handlers= []
            for hand in logger.handlers:
                if isinstance(hand, logging.FileHandler):
                    file_handlers.append(hand)
            for hand in file_handlers:
                logger.removeHandler(hand)
            logger(message)
            for hand in file_handlers:
                logger.addHandler(hand)
        else:
            logger(message)
        if flush:
            sys.stdout.flush()
        self.lock.release()

    def go(self):

        while not self.queue.empty():
            try:
                job: LocalJob = self.queue.get()
                input_opt = job.profile.input_options.as_shell_params()
                output_opt = self.config.output_from_profile(job.profile, job.mixins)
                logger = logging.getLogger(f'Processing {job.inpath.name}')

                fls = False
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

                #
                # display useful information
                #
                # self.lock.acquire()  # used to synchronize threads so multiple threads don't create a jumble of output
                try:
                    self.log(logger.info, f"Filename : {crayons.green(os.path.basename(str(job.inpath)))}")
                    self.log(logger.info, f"Profile  : {job.profile.name} {'{:<6}   : '.format(job.profile.processor) + ' '.join(cli)}")
                except Exception as err:
                    self.log(logger.critical, f'{err}')
                # finally:
                #     self.lock.release()


                if pytranscoder.dry_run:
                    continue

                self.basename = basename = job.inpath.name

                def log_callback(stats):
                    pct_done, pct_comp = calculate_progress(job.info, stats)
                    pytranscoder.status_queue.put({ 'host': 'local',
                                                    'file': basename,
                                                    'speed': stats['speed'],
                                                    'comp': pct_comp,
                                                    'done': pct_done})

                    self.log(logger_progress_stream.info, f'{basename}: speed: {stats["speed"]}x, comp: {pct_comp}%, done: {pct_done:3}%')
                    if pct_comp < 0:
                        self.log(logger.warning,
                                 f'Encoding of {basename} cancelled and skipped due negative compression ratio')
                        return True
                    if job.profile.threshold_check < 100:
                        if pct_done >= job.profile.threshold_check and pct_comp < job.profile.threshold:
                            # compression goal (threshold) not met, kill the job and waste no more time...
                            self.log(logger.warning, f'Encoding of {basename} cancelled and skipped due to threshold not met')
                            return True
                    return False

                def hbcli_callback(stats):
                    self.log(logger.info, f'{basename}: avg fps: {stats["fps"]}, ETA: {stats["eta"]}')
                    return False

                def add_processed_suffix(_output):
                    base, ext = os.path.splitext(_output)
                    suffix = self.config.settings.get('completed_suffix', DEFAULT_PROCESSED_SUFFIX)
                    base += suffix

                    return PurePath(base + ext)


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
                        self.log(logger.warning, f'Transcoded file {job.inpath} did not meet minimum savings threshold, skipped')
                        self.complete(job.inpath, (job_stop - job_start).seconds)
                        self.log(logger.info, f'completed: {job.inpath} in {(job_stop - job_start).seconds}')
                        os.unlink(str(outpath))
                        self.log(logger.info, f'{outpath} removed')
                        continue

                    self.complete(job.inpath, elapsed.seconds)
                    destination = self.config.dest_dir()
                    if destination:
                        try:
                            os.makedirs(destination,exist_ok=True)
                        except Exception as err:
                            self.log(logger.error,str(err))
                            self.log(logger.warning, f'The destination folder {destination} does not exist and can not be created')
                            self.log(logger.info, f'Changing the invalid destination folder to the temp output {self.config.tmp_dir()}')
                            destination = outpath.parent
                        if keep_orig and destination == os.path.dirname(job.inpath):
                            completed_path = add_processed_suffix(os.path.join(destination, os.path.basename(
                                job.inpath.with_suffix(job.profile.extension))))
                        else:
                            completed_path = os.path.join(destination,
                                                  os.path.basename(job.inpath.with_suffix(job.profile.extension)))
                    else:
                        completed_path = job.inpath.with_suffix(job.profile.extension)

                    shutil.move(outpath, completed_path)
                    self.log(logger.info, f'{outpath} moved to {completed_path}')
                            # outpath.rename(job.inpath.with_suffix(job.profile.extension))
                    if not keep_orig:
                        job.inpath.unlink()
                        self.log(logger.info, f'{job.inpath} removed')
                    self.log(logger.info, crayons.yellow(f'Finished {outpath}, {"original file unchanged" if keep_orig else ""}'))

                elif code is not None:
                    self.log(logger.critical, f' Did not complete normally: {processor.last_command}')
                    self.log(logger.info, f'Output can be found in {processor.log_path}')
                    try:
                        outpath.unlink()
                        self.log(logger.info, f'{outpath} removed')

                    except Exception as err:
                        self.log(logger.warning, f'{outpath} NOT removed')
                        self.log(logger.error, f'{err}')
            finally:
                self.queue.task_done()


class LocalHost:
    """Encapsulates functionality for local encoding"""

    lock:       Lock = Lock()
    complete:   List = list()            # list of completed files, shared across threads

    def __init__(self, configfile: ConfigFile):
        self.queues = dict()
        self.configfile = configfile

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

        # wait for all queues to drain and all jobs to complete
#        for _, queue in self.queues.items():
#            queue.join()

    def enqueue_files(self, files: list):
        """Add requested files to the appropriate queue

        :param files: list of (path,profile) tuples
        :return:
        """

        for path, forced_profile, mixins in files:
            #
            # do some prechecks...
            #
            if forced_profile is not None and not self.configfile.has_profile(forced_profile):
                print(f'profile "{forced_profile}" referenced from command line not found')
                sys.exit(1)

            if len(path) == 0:
                continue

            if not os.path.isfile(path):
                print(crayons.red('file not found, skipping: ' + path))
                continue

            processor_name = 'ffmpeg'

            if forced_profile:
                the_profile = self.configfile.get_profile(forced_profile)
                if not the_profile.is_ffmpeg:
                    processor_name = 'hbcli'

            processor = self.configfile.get_processor_by_name(processor_name)
            media_info = processor.fetch_details(path)

            if media_info is None:
                print(crayons.red(f'File not found: {path}'))
                continue

            if media_info.valid:

                if pytranscoder.verbose:
                    print(str(media_info))

                if forced_profile is None:
                    rule = self.configfile.match_rule(media_info)
                    if rule is None:
                        print(crayons.green(os.path.basename(path)), crayons.yellow(f'No matching profile found - skipped'))
                        continue
                    if rule.is_skip():
                        print(crayons.green(os.path.basename(path)), f'SKIPPED ({rule.name})')
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
                        sys.exit(1)
                    else:
                        self.queues[qname].put(LocalJob(path, the_profile, mixins, media_info))
                        if pytranscoder.verbose:
                            print('Added to queue {qname}')
                else:
                    self.queues['_default_'].put(LocalJob(path, the_profile, mixins, media_info))


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
        start(folder_path)
    else:
        start_cmd_line()

def start(path):
    install_sigint_handler()
    configfile = ConfigFile(DEFAULT_CONFIG)
    files = get_files(path, configfile)
    queue_path = configfile.default_queue_file
    if not queue_path:
        queue_path = '/tmp/py_encoder.txt'
    if not configfile.colorize:
        crayons.disable()
    else:
        crayons.enable()
    host_start(configfile, files, queue_path)




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


    host_start(configfile, files, queue_path)


def host_start(configfile, files, queue_path):
    host = LocalHost(configfile)
    host.enqueue_files(files)
    #
    # start all threads and wait for work to complete
    #
    host.start()
    if len(host.complete) > 0:
        completed_paths = [p for p, _ in host.complete]
        cleanup_queuefile(queue_path, set(completed_paths))
        dump_stats(host.complete)

    # os.system("stty sane")


if __name__ == '__main__':
    start_cmd_line()
