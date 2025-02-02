"""Sky job lib, backed by a sqlite database.

This is a remote utility module that provides job queue functionality.
"""
import enum
import json
import os
import pathlib
import shlex
import time
import typing
from typing import Any, Dict, List, Optional

import colorama
import filelock

from sky import sky_logging
from sky.skylet import constants
from sky.utils import common_utils
from sky.utils import db_utils
from sky.utils import log_utils

if typing.TYPE_CHECKING:
    from ray.dashboard.modules.job import pydantic_models as ray_pydantic

logger = sky_logging.init_logger(__name__)

_JOB_STATUS_LOCK = '~/.sky/locks/.job_{}.lock'


def _get_lock_path(job_id: int) -> str:
    lock_path = os.path.expanduser(_JOB_STATUS_LOCK.format(job_id))
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    return lock_path


class JobInfoLoc(enum.IntEnum):
    """Job Info's Location in the DB record"""
    JOB_ID = 0
    JOB_NAME = 1
    USERNAME = 2
    SUBMITTED_AT = 3
    STATUS = 4
    RUN_TIMESTAMP = 5
    START_AT = 6
    END_AT = 7
    RESOURCES = 8


_DB_PATH = os.path.expanduser('~/.sky/jobs.db')
os.makedirs(pathlib.Path(_DB_PATH).parents[0], exist_ok=True)


def create_table(cursor, conn):
    cursor.execute("""\
        CREATE TABLE IF NOT EXISTS jobs (
        job_id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_name TEXT,
        username TEXT,
        submitted_at FLOAT,
        status TEXT,
        run_timestamp TEXT CANDIDATE KEY,
        start_at FLOAT DEFAULT -1)""")

    db_utils.add_column_to_table(cursor, conn, 'jobs', 'end_at', 'FLOAT')
    db_utils.add_column_to_table(cursor, conn, 'jobs', 'resources', 'TEXT')

    conn.commit()


_DB = db_utils.SQLiteConn(_DB_PATH, create_table)
_CURSOR = _DB.cursor
_CONN = _DB.conn


class JobStatus(enum.Enum):
    """Job status"""

    # 3 in-flux states: each can transition to any state below it.
    # The `job_id` has been generated, but the generated ray program has
    # not started yet. skylet can transit the state from INIT to FAILED
    # directly, if the ray program fails to start.
    # In the 'jobs' table, the `submitted_at` column will be set to the current
    # time, when the job is firstly created (in the INIT state).
    INIT = 'INIT'
    # Running the user's setup script (only in effect if --detach-setup is
    # set). Our update_job_status() can temporarily (for a short period) set
    # the status to SETTING_UP, if the generated ray program has not set
    # the status to PENDING or RUNNING yet.
    SETTING_UP = 'SETTING_UP'
    # The job is waiting for the required resources. (`ray job status`
    # shows RUNNING as the generated ray program has started, but blocked
    # by the placement constraints.)
    PENDING = 'PENDING'
    # The job is running.
    # In the 'jobs' table, the `start_at` column will be set to the current
    # time, when the job is firstly transitioned to RUNNING.
    RUNNING = 'RUNNING'
    # 3 terminal states below: once reached, they do not transition.
    # The job finished successfully.
    SUCCEEDED = 'SUCCEEDED'
    # The job fails due to the user code or a system restart.
    FAILED = 'FAILED'
    # The job setup failed (only in effect if --detach-setup is set). It
    # needs to be placed after the `FAILED` state, so that the status
    # set by our generated ray program will not be overwritten by
    # ray's job status (FAILED).
    # This is for a better UX, so that the user can find out the reason
    # of the failure quickly.
    FAILED_SETUP = 'FAILED_SETUP'
    # The job is cancelled by the user.
    CANCELLED = 'CANCELLED'

    @classmethod
    def nonterminal_statuses(cls) -> List['JobStatus']:
        return [cls.INIT, cls.SETTING_UP, cls.PENDING, cls.RUNNING]

    def is_terminal(self):
        return self not in self.nonterminal_statuses()

    def __lt__(self, other):
        return list(JobStatus).index(self) < list(JobStatus).index(other)

    def colored_str(self):
        color = _JOB_STATUS_TO_COLOR[self]
        return f'{color}{self.value}{colorama.Style.RESET_ALL}'


_JOB_STATUS_TO_COLOR = {
    JobStatus.INIT: colorama.Fore.BLUE,
    JobStatus.SETTING_UP: colorama.Fore.BLUE,
    JobStatus.PENDING: colorama.Fore.BLUE,
    JobStatus.RUNNING: colorama.Fore.GREEN,
    JobStatus.SUCCEEDED: colorama.Fore.GREEN,
    JobStatus.FAILED: colorama.Fore.RED,
    JobStatus.FAILED_SETUP: colorama.Fore.RED,
    JobStatus.CANCELLED: colorama.Fore.YELLOW,
}

_RAY_TO_JOB_STATUS_MAP = {
    # These are intentionally set to one status before, because:
    # 1. when the ray status indicates the job is PENDING the generated
    # python program should not be started yet, i.e. the job should be INIT.
    # 2. when the ray status indicates the job is RUNNING the job can be in
    # setup or resources may not be allocated yet, i.e. the job should be
    # SETTING_UP.
    # For case 2, update_job_status() would compare this mapped SETTING_UP to
    # the status in our jobs DB and take the max. This is because the job's
    # generated ray program is the only place that can determine a job has
    # reserved resources and actually started running: it will set the
    # status in the DB to RUNNING.
    # If there is no setup specified in the task, as soon as it is started
    # (ray's status becomes RUNNING), i.e. it will be very rare that the job
    # will be set to SETTING_UP by the update_job_status, as our generated
    # ray program will set the status to PENDING immediately.
    'PENDING': JobStatus.INIT,
    'RUNNING': JobStatus.SETTING_UP,
    'SUCCEEDED': JobStatus.SUCCEEDED,
    'FAILED': JobStatus.FAILED,
    'STOPPED': JobStatus.CANCELLED,
}


def _create_ray_job_submission_client():
    """Import the ray job submission client."""
    try:
        import ray  # pylint: disable=import-outside-toplevel
    except ImportError:
        logger.error('Failed to import ray')
        raise
    try:
        from ray import job_submission  # pylint: disable=import-outside-toplevel
    except ImportError:
        logger.error(
            f'Failed to import job_submission with ray=={ray.__version__}')
        raise
    port = get_job_submission_port()
    return job_submission.JobSubmissionClient(
        address=f'http://127.0.0.1:{port}')


def make_ray_job_id(sky_job_id: int, job_owner: str) -> str:
    return f'{sky_job_id}-{job_owner}'


def make_job_command_with_user_switching(username: str,
                                         command: str) -> List[str]:
    return ['sudo', '-H', 'su', '--login', username, '-c', command]


def add_job(job_name: str, username: str, run_timestamp: str,
            resources_str: str) -> int:
    """Atomically reserve the next available job id for the user."""
    job_submitted_at = time.time()
    # job_id will autoincrement with the null value
    _CURSOR.execute('INSERT INTO jobs VALUES (null, ?, ?, ?, ?, ?, ?, null, ?)',
                    (job_name, username, job_submitted_at, JobStatus.INIT.value,
                     run_timestamp, None, resources_str))
    _CONN.commit()
    rows = _CURSOR.execute('SELECT job_id FROM jobs WHERE run_timestamp=(?)',
                           (run_timestamp,))
    for row in rows:
        job_id = row[0]
    assert job_id is not None
    return job_id


def _set_status_no_lock(job_id: int, status: JobStatus) -> None:
    """Setting the status of the job in the database."""
    assert status != JobStatus.RUNNING, (
        'Please use set_job_started() to set job status to RUNNING')
    if status.is_terminal():
        end_at = time.time()
        # status does not need to be set if the end_at is not null, since
        # the job must be in a terminal state already.
        # Don't check the end_at for FAILED_SETUP, so that the generated
        # ray program can overwrite the status.
        check_end_at_str = ' AND end_at IS NULL'
        if status != JobStatus.FAILED_SETUP:
            check_end_at_str = ''
        _CURSOR.execute(
            'UPDATE jobs SET status=(?), end_at=(?) '
            f'WHERE job_id=(?) {check_end_at_str}',
            (status.value, end_at, job_id))
    else:
        _CURSOR.execute(
            'UPDATE jobs SET status=(?), end_at=NULL '
            'WHERE job_id=(?)', (status.value, job_id))
    _CONN.commit()


def set_status(job_id: int, status: JobStatus) -> None:
    # TODO(mraheja): remove pylint disabling when filelock version updated
    # pylint: disable=abstract-class-instantiated
    with filelock.FileLock(_get_lock_path(job_id)):
        _set_status_no_lock(job_id, status)


def set_job_started(job_id: int) -> None:
    # TODO(mraheja): remove pylint disabling when filelock version updated.
    # pylint: disable=abstract-class-instantiated
    with filelock.FileLock(_get_lock_path(job_id)):
        _CURSOR.execute(
            'UPDATE jobs SET status=(?), start_at=(?), end_at=NULL '
            'WHERE job_id=(?)', (JobStatus.RUNNING.value, time.time(), job_id))
        _CONN.commit()


def get_status_no_lock(job_id: int) -> Optional[JobStatus]:
    """Get the status of the job with the given id.

    This function can return a stale status if there is a concurrent update.
    Make sure the caller will not be affected by the stale status, e.g. getting
    the status in a while loop as in `log_lib._follow_job_logs`. Otherwise, use
    `get_status`.
    """
    rows = _CURSOR.execute('SELECT status FROM jobs WHERE job_id=(?)',
                           (job_id,))
    for (status,) in rows:
        if status is None:
            return None
        return JobStatus(status)
    return None


def get_status(job_id: int) -> Optional[JobStatus]:
    # TODO(mraheja): remove pylint disabling when filelock version updated.
    # pylint: disable=abstract-class-instantiated
    with filelock.FileLock(_get_lock_path(job_id)):
        return get_status_no_lock(job_id)


def get_statuses_payload(job_ids: List[Optional[int]]) -> str:
    # Per-job lock is not required here, since the staled job status will not
    # affect the caller.
    query_str = ','.join(['?'] * len(job_ids))
    rows = _CURSOR.execute(
        f'SELECT job_id, status FROM jobs WHERE job_id IN ({query_str})',
        job_ids)
    statuses = {job_id: None for job_id in job_ids}
    for (job_id, status) in rows:
        statuses[job_id] = status
    return common_utils.encode_payload(statuses)


def load_statuses_payload(
        statuses_payload: str) -> Dict[Optional[int], Optional[JobStatus]]:
    statuses = common_utils.decode_payload(statuses_payload)
    for job_id, status in statuses.items():
        if status is not None:
            statuses[job_id] = JobStatus(status)
    return statuses


def get_latest_job_id() -> Optional[int]:
    rows = _CURSOR.execute(
        'SELECT job_id FROM jobs ORDER BY job_id DESC LIMIT 1')
    for (job_id,) in rows:
        return job_id
    return None


def get_job_submitted_or_ended_timestamp_payload(job_id: int,
                                                 get_ended_time: bool) -> str:
    """Get the job submitted/ended timestamp.

    This function should only be called by the spot controller,
    which is ok to use `submitted_at` instead of `start_at`,
    because the spot job duration need to include both setup
    and running time and the job will not stay in PENDING
    state.

    The normal job duration will use `start_at` instead of
    `submitted_at` (in `format_job_queue()`), because the job
    may stay in PENDING if the cluster is busy.
    """
    field = 'end_at' if get_ended_time else 'submitted_at'
    rows = _CURSOR.execute(f'SELECT {field} FROM jobs WHERE job_id=(?)',
                           (job_id,))
    for (timestamp,) in rows:
        return common_utils.encode_payload(timestamp)
    return common_utils.encode_payload(None)


def get_ray_port():
    """Get the port Skypilot-internal Ray cluster uses.

    If the port file does not exist, the cluster was launched before #1790,
    return the default port.
    """
    port_path = os.path.expanduser(constants.SKY_REMOTE_RAY_PORT_FILE)
    if not os.path.exists(port_path):
        return 6379
    port = json.load(open(port_path))['ray_port']
    return port


def get_job_submission_port():
    """Get the dashboard port Skypilot-internal Ray cluster uses.

    If the port file does not exist, the cluster was launched before #1790,
    return the default port.
    """
    port_path = os.path.expanduser(constants.SKY_REMOTE_RAY_PORT_FILE)
    if not os.path.exists(port_path):
        return 8265
    port = json.load(open(port_path))['ray_dashboard_port']
    return port


def _get_records_from_rows(rows) -> List[Dict[str, Any]]:
    records = []
    for row in rows:
        if row[0] is None:
            break
        # TODO: use namedtuple instead of dict
        records.append({
            'job_id': row[JobInfoLoc.JOB_ID.value],
            'job_name': row[JobInfoLoc.JOB_NAME.value],
            'username': row[JobInfoLoc.USERNAME.value],
            'submitted_at': row[JobInfoLoc.SUBMITTED_AT.value],
            'status': JobStatus(row[JobInfoLoc.STATUS.value]),
            'run_timestamp': row[JobInfoLoc.RUN_TIMESTAMP.value],
            'start_at': row[JobInfoLoc.START_AT.value],
            'end_at': row[JobInfoLoc.END_AT.value],
            'resources': row[JobInfoLoc.RESOURCES.value],
        })
    return records


def _get_jobs(username: Optional[str],
              status_list: Optional[List[JobStatus]] = None,
              submitted_gap_sec: int = 0) -> List[Dict[str, Any]]:
    if status_list is None:
        status_list = list(JobStatus)
    status_str_list = [status.value for status in status_list]
    if username is None:
        rows = _CURSOR.execute(
            f"""\
            SELECT * FROM jobs
            WHERE status IN ({','.join(['?'] * len(status_list))})
            AND submitted_at <= (?)
            ORDER BY job_id DESC""",
            (*status_str_list, time.time() - submitted_gap_sec),
        )
    else:
        rows = _CURSOR.execute(
            f"""\
            SELECT * FROM jobs
            WHERE status IN ({','.join(['?'] * len(status_list))})
            AND username=(?) AND submitted_at <= (?)
            ORDER BY job_id DESC""",
            (*status_str_list, username, time.time() - submitted_gap_sec),
        )

    records = _get_records_from_rows(rows)
    return records


def _get_jobs_by_ids(job_ids: List[int]) -> List[Dict[str, Any]]:
    rows = _CURSOR.execute(
        f"""\
        SELECT * FROM jobs
        WHERE job_id IN ({','.join(['?'] * len(job_ids))})
        ORDER BY job_id DESC""",
        (*job_ids,),
    )
    records = _get_records_from_rows(rows)
    return records


def update_job_status(job_owner: str,
                      job_ids: List[int],
                      silent: bool = False) -> List[JobStatus]:
    """Updates and returns the job statuses matching our `JobStatus` semantics.

    This function queries `ray job status` and processes those results to match
    our semantics.

    Though we update job status actively in the generated ray program and
    during job cancelling, we still need this to handle the staleness problem,
    caused by instance restarting and other corner cases (if any).

    This function should only be run on the remote instance with ray==2.4.0.
    """
    if len(job_ids) == 0:
        return []

    # TODO: if too slow, directly query against redis.
    ray_job_ids = [make_ray_job_id(job_id, job_owner) for job_id in job_ids]

    job_client = _create_ray_job_submission_client()

    # In ray 2.4.0, job_client.list_jobs returns a list of JobDetails,
    # which contains the job status (str) and submission_id (str).
    job_detail_lists: List['ray_pydantic.JobDetails'] = job_client.list_jobs()

    job_details = {}
    ray_job_ids_set = set(ray_job_ids)
    for job_detail in job_detail_lists:
        if job_detail.submission_id in ray_job_ids_set:
            job_details[job_detail.submission_id] = job_detail
    job_statuses: List[Optional[JobStatus]] = [None] * len(ray_job_ids)
    for i, ray_job_id in enumerate(ray_job_ids):
        if ray_job_id in job_details:
            ray_status = job_details[ray_job_id].status
            job_statuses[i] = _RAY_TO_JOB_STATUS_MAP[ray_status]

    assert len(job_statuses) == len(job_ids), (job_statuses, job_ids)

    statuses = []
    for job_id, status in zip(job_ids, job_statuses):
        # Per-job status lock is required because between the job status
        # query and the job status update, the job status in the databse
        # can be modified by the generated ray program.
        # TODO(mraheja): remove pylint disabling when filelock version
        # updated
        # pylint: disable=abstract-class-instantiated
        with filelock.FileLock(_get_lock_path(job_id)):
            original_status = get_status_no_lock(job_id)
            assert original_status is not None, (job_id, status)
            if status is None:
                status = original_status
                if (original_status is not None and
                        not original_status.is_terminal()):
                    # The job may be stale, when the instance is restarted
                    # (the ray redis is volatile). We need to reset the
                    # status of the task to FAILED if its original status
                    # is RUNNING or PENDING.
                    status = JobStatus.FAILED
                    _set_status_no_lock(job_id, status)
                    if not silent:
                        logger.info(f'Updated job {job_id} status to {status}')
            else:
                # Taking max of the status is necessary because:
                # 1. It avoids race condition, where the original status has
                # already been set to later state by the job. We skip the
                # update.
                # 2. _RAY_TO_JOB_STATUS_MAP would map `ray job status`'s
                # `RUNNING` to our JobStatus.SETTING_UP; if a job has already
                # been set to JobStatus.PENDING or JobStatus.RUNNING by the
                # generated ray program, `original_status` (job status from our
                # DB) would already have that value. So we take the max here to
                # keep it at later status.
                status = max(status, original_status)
                if status != original_status:  # Prevents redundant update.
                    _set_status_no_lock(job_id, status)
                    if not silent:
                        logger.info(f'Updated job {job_id} status to {status}')
        statuses.append(status)
    return statuses


def fail_all_jobs_in_progress() -> None:
    in_progress_status = [
        status.value for status in JobStatus.nonterminal_statuses()
    ]
    _CURSOR.execute(
        f"""\
        UPDATE jobs SET status=(?)
        WHERE status IN ({','.join(['?'] * len(in_progress_status))})
        """, (JobStatus.FAILED.value, *in_progress_status))
    _CONN.commit()


def update_status(job_owner: str, submitted_gap_sec: int = 0) -> None:
    # This will be called periodically by the skylet to update the status
    # of the jobs in the database, to avoid stale job status.
    # NOTE: there might be a INIT job in the database set to FAILED by this
    # function, as the ray job status does not exist due to the app
    # not submitted yet. It will be then reset to PENDING / RUNNING when the
    # app starts.
    nonterminal_jobs = _get_jobs(username=None,
                                 status_list=JobStatus.nonterminal_statuses(),
                                 submitted_gap_sec=submitted_gap_sec)
    nonterminal_job_ids = [job['job_id'] for job in nonterminal_jobs]

    update_job_status(job_owner, nonterminal_job_ids)


def is_cluster_idle() -> bool:
    """Returns if the cluster is idle (no in-flight jobs)."""
    in_progress_status = [
        status.value for status in JobStatus.nonterminal_statuses()
    ]
    rows = _CURSOR.execute(
        f"""\
        SELECT COUNT(*) FROM jobs
        WHERE status IN ({','.join(['?'] * len(in_progress_status))})
        """, in_progress_status)
    for (count,) in rows:
        return count == 0
    assert False, 'Should not reach here'


def format_job_queue(jobs: List[Dict[str, Any]]):
    """Format the job queue for display.

    Usage:
        jobs = get_job_queue()
        print(format_job_queue(jobs))
    """
    job_table = log_utils.create_table([
        'ID', 'NAME', 'SUBMITTED', 'STARTED', 'DURATION', 'RESOURCES', 'STATUS',
        'LOG'
    ])
    for job in jobs:
        job_table.add_row([
            job['job_id'],
            job['job_name'],
            log_utils.readable_time_duration(job['submitted_at']),
            log_utils.readable_time_duration(job['start_at']),
            log_utils.readable_time_duration(job['start_at'],
                                             job['end_at'],
                                             absolute=True),
            job['resources'],
            job['status'].colored_str(),
            job['log_path'],
        ])
    return job_table


def dump_job_queue(username: Optional[str], all_jobs: bool) -> str:
    """Get the job queue in encoded json format.

    Args:
        username: The username to show jobs for. Show all the users if None.
        all_jobs: Whether to show all jobs, not just the pending/running ones.
    """
    status_list: Optional[List[JobStatus]] = [
        JobStatus.SETTING_UP, JobStatus.PENDING, JobStatus.RUNNING
    ]
    if all_jobs:
        status_list = None

    jobs = _get_jobs(username, status_list=status_list)
    for job in jobs:
        job['status'] = job['status'].value
        job['log_path'] = os.path.join(constants.SKY_LOGS_DIRECTORY,
                                       job.pop('run_timestamp'))
    return common_utils.encode_payload(jobs)


def load_job_queue(payload: str) -> List[Dict[str, Any]]:
    """Load the job queue from encoded json format.

    Args:
        payload: The encoded payload string to load.
    """
    jobs = common_utils.decode_payload(payload)
    for job in jobs:
        job['status'] = JobStatus(job['status'])
    return jobs


def cancel_jobs(job_owner: str, jobs: Optional[List[int]]) -> None:
    """Cancel the jobs.

    Args:
        jobs: The job ids to cancel. If None, cancel all the jobs.
    """
    # Update the status of the jobs to avoid setting the status of stale
    # jobs to CANCELLED.
    if jobs is None:
        job_records = _get_jobs(
            None, [JobStatus.SETTING_UP, JobStatus.PENDING, JobStatus.RUNNING])
    else:
        job_records = _get_jobs_by_ids(jobs)

    # TODO(zhwu): `job_client.stop_job` will wait for the jobs to be killed, but
    # when the memory is not enough, this will keep waiting.
    job_client = _create_ray_job_submission_client()

    # Sequentially cancel the jobs to avoid the resource number bug caused by
    # ray cluster (tracked in #1262).
    for job in job_records:
        job_id = make_ray_job_id(job['job_id'], job_owner)
        try:
            job_client.stop_job(job_id)
        except RuntimeError as e:
            # If the job does not exist or if the request to the
            # job server fails.
            logger.warning(str(e))
            continue

        if job['status'] in [
                JobStatus.SETTING_UP, JobStatus.PENDING, JobStatus.RUNNING
        ]:
            set_status(job['job_id'], JobStatus.CANCELLED)


def get_run_timestamp(job_id: Optional[int]) -> Optional[str]:
    """Returns the relative path to the log file for a job."""
    _CURSOR.execute(
        """\
            SELECT * FROM jobs
            WHERE job_id=(?)""", (job_id,))
    row = _CURSOR.fetchone()
    if row is None:
        return None
    run_timestamp = row[JobInfoLoc.RUN_TIMESTAMP.value]
    return run_timestamp


def run_timestamp_with_globbing_payload(job_ids: List[Optional[str]]) -> str:
    """Returns the relative paths to the log files for job with globbing."""
    query_str = ' OR '.join(['job_id GLOB (?)'] * len(job_ids))
    _CURSOR.execute(
        f"""\
            SELECT * FROM jobs
            WHERE {query_str}""", job_ids)
    rows = _CURSOR.fetchall()
    run_timestamps = {}
    for row in rows:
        job_id = row[JobInfoLoc.JOB_ID.value]
        run_timestamp = row[JobInfoLoc.RUN_TIMESTAMP.value]
        run_timestamps[str(job_id)] = run_timestamp
    return common_utils.encode_payload(run_timestamps)


class JobLibCodeGen:
    """Code generator for job utility functions.

    Usage:

      >> codegen = JobLibCodeGen.add_job(...)
    """

    _PREFIX = ['import os', 'from sky.skylet import job_lib, log_lib']

    @classmethod
    def add_job(cls, job_name: Optional[str], username: str, run_timestamp: str,
                resources_str: str) -> str:
        if job_name is None:
            job_name = '-'
        code = [
            'job_id = job_lib.add_job('
            f'{job_name!r}, '
            f'{username!r}, '
            f'{run_timestamp!r}, '
            f'{resources_str!r})',
            'print("Job ID: " + str(job_id), flush=True)',
        ]
        return cls._build(code)

    @classmethod
    def update_status(cls, job_owner: str) -> str:
        code = [
            f'job_lib.update_status({job_owner!r})',
        ]
        return cls._build(code)

    @classmethod
    def get_job_queue(cls, username: Optional[str], all_jobs: bool) -> str:
        code = [
            'job_queue = job_lib.dump_job_queue('
            f'{username!r}, {all_jobs})', 'print(job_queue, flush=True)'
        ]
        return cls._build(code)

    @classmethod
    def cancel_jobs(cls, job_owner: str, job_ids: Optional[List[int]]) -> str:
        code = [f'job_lib.cancel_jobs({job_owner!r},{job_ids!r})']
        return cls._build(code)

    @classmethod
    def fail_all_jobs_in_progress(cls) -> str:
        # Used only for restarting a cluster.
        code = ['job_lib.fail_all_jobs_in_progress()']
        return cls._build(code)

    @classmethod
    def tail_logs(cls,
                  job_owner: str,
                  job_id: Optional[int],
                  spot_job_id: Optional[int],
                  follow: bool = True) -> str:
        # pylint: disable=line-too-long
        code = [
            f'job_id = {job_id} if {job_id} is not None else job_lib.get_latest_job_id()',
            'run_timestamp = job_lib.get_run_timestamp(job_id)',
            f'log_dir = None if run_timestamp is None else os.path.join({constants.SKY_LOGS_DIRECTORY!r}, run_timestamp)',
            f'log_lib.tail_logs({job_owner!r}, job_id, log_dir, {spot_job_id!r}, follow={follow})',
        ]
        return cls._build(code)

    @classmethod
    def get_job_status(cls, job_ids: Optional[List[int]] = None) -> str:
        # Prints "Job <id> <status>" for UX; caller should parse the last token.
        code = [
            f'job_ids = {job_ids} if {job_ids} is not None '
            'else [job_lib.get_latest_job_id()]',
            'job_statuses = job_lib.get_statuses_payload(job_ids)',
            'print(job_statuses, flush=True)',
        ]
        return cls._build(code)

    @classmethod
    def get_job_submitted_or_ended_timestamp_payload(
            cls,
            job_id: Optional[int] = None,
            get_ended_time: bool = False) -> str:
        code = [
            f'job_id = {job_id} if {job_id} is not None '
            'else job_lib.get_latest_job_id()',
            'job_time = '
            'job_lib.get_job_submitted_or_ended_timestamp_payload('
            f'job_id, {get_ended_time})',
            'print(job_time, flush=True)',
        ]
        return cls._build(code)

    @classmethod
    def get_run_timestamp_with_globbing(cls,
                                        job_ids: Optional[List[str]]) -> str:
        code = [
            f'job_ids = {job_ids} if {job_ids} is not None '
            'else [job_lib.get_latest_job_id()]',
            'log_dirs = job_lib.run_timestamp_with_globbing_payload(job_ids)',
            'print(log_dirs, flush=True)',
        ]
        return cls._build(code)

    @classmethod
    def _build(cls, code: List[str]) -> str:
        code = cls._PREFIX + code
        code = ';'.join(code)
        return f'python3 -u -c {shlex.quote(code)}'
