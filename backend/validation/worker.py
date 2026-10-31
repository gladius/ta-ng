"""Run the queue: background tasks (core/tasks.py) and validation jobs.  python -m validation.worker [--threads N]

Run any number, on any machines sharing DATABASE_URL: each thread claims the next task, else the next group of any
job, so every free worker helps with every batch. The web app runs VALIDATION_INPROCESS_WORKERS threads itself (default 2).
"""
import argparse
import signal
import threading
import traceback

from app.services.llm_client import Unreachable
from core import schedule, tasks
from validation import jobs

POLL_S = 2.0
UNREACHABLE_WAIT_S = 30     # the model endpoint is down (its group went back to the queue): retry after this
ERROR_WAIT_S = 10           # anything else (e.g. the database dropped): logged, then the thread carries on
_held = {}                  # worker id -> (release, token) of the task or group it runs, handed back on shutdown


def loop(worker, stop):
    while not stop.is_set():
        try:
            schedule.tick()                            # queue any due scheduled job (one instance wins it)
            task = tasks.claim(worker)                 # first: sampling and analyses are short and unblock the rest
            if task:
                _held[worker] = (tasks.release, task[1])
                try:
                    tasks.run(*task)
                finally:
                    _held.pop(worker, None)
                continue
            got = jobs.claim(worker)
            if not got:
                stop.wait(POLL_S)
                continue
            job_id, token, group = got
            _held[worker] = (jobs.release, token)
            try:
                jobs.run_group(job_id, token, group)
            finally:
                _held.pop(worker, None)
        except Unreachable as e:
            print("validation worker %s: model endpoint unreachable (%s); retrying in %ds" % (worker, e, UNREACHABLE_WAIT_S),
                  flush=True)
            stop.wait(UNREACHABLE_WAIT_S)
        except Exception:                          # a group it held goes stale and is taken over; the thread lives on
            print("validation worker %s: unexpected error; retrying in %ds" % (worker, ERROR_WAIT_S), flush=True)
            traceback.print_exc()
            stop.wait(ERROR_WAIT_S)


def start(n, stop):
    """Start n worker threads; they stop claiming when `stop` is set."""
    for i in range(n):
        threading.Thread(target=loop, args=("%s-t%d" % (jobs.WORKER, i), stop), daemon=True,
                         name="validation-worker-%d" % i).start()


def release_all():
    """Hand back every task and group this process still holds, so other workers take them over at once."""
    for release, token in list(_held.values()):
        release(token)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--threads", type=int, default=2, help="groups this process works on at once (default 2)")
    args = p.parse_args()
    import credentials
    credentials.load()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    start(args.threads, stop)
    print("validation worker %s: %d thread(s), polling every %.0fs" % (jobs.WORKER, args.threads, POLL_S), flush=True)
    try:
        stop.wait()
    except KeyboardInterrupt:
        stop.set()
    release_all()


if __name__ == "__main__":
    main()
