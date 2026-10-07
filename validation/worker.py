"""Run validation jobs.  python -m validation.worker [--threads N]

Run any number of these, on any machines that share DATABASE_URL. Each thread repeatedly claims the next group of
unfinished requests from any job, validates it and records it, so every free worker helps with every batch. The web
app runs VALIDATION_INPROCESS_WORKERS of these threads itself (default 2), so one process is enough locally; in
production set it to 0 and scale workers separately. On shutdown, groups still in progress are handed back at once.
"""
import argparse
import signal
import threading

from validation import jobs

POLL_S = 2.0
_held = {}                  # thread's worker id -> token of the group it is running


def loop(worker, stop):
    while not stop.is_set():
        got = jobs.claim(worker)
        if not got:
            stop.wait(POLL_S)
            continue
        job_id, token, group = got
        _held[worker] = token
        try:
            jobs.run_group(job_id, token, group)
        finally:
            _held.pop(worker, None)


def start(n, stop):
    """Start n worker threads in this process; they stop claiming when `stop` is set."""
    for i in range(n):
        threading.Thread(target=loop, args=("%s-t%d" % (jobs.WORKER, i), stop), daemon=True,
                         name="validation-worker-%d" % i).start()


def release_all():
    """Hand back every group this process is still working on, so other workers take them over immediately."""
    for token in list(_held.values()):
        jobs.release(token)


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
