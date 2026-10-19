"""Agent notes under concurrency: the host runs many brains for one agent at once, each with its own
Toolbox on the same notes file, so writes must neither tear the file nor drop each other's keys."""

import threading

from pacific.agents.tools import Toolbox


def test_concurrent_notes_writes_and_reads_lose_nothing(tmp_path):
    path = tmp_path / "agents" / "a1" / "notes.json"
    writers, per_writer, readers = 8, 40, 4
    errors: list[BaseException] = []
    done = threading.Event()
    start = threading.Barrier(writers + readers)

    def write(w: int):
        box = Toolbox(None, path, "a1", [])
        start.wait()
        try:
            for i in range(per_writer):
                box.call("notes_write", key=f"w{w}-{i}", text=f"note {w}/{i} " * 20)
        except BaseException as e:
            errors.append(e)

    def read():
        box = Toolbox(None, path, "a1", [])
        start.wait()
        try:
            while not done.is_set():
                keys = box.call("notes_read")
                if keys:
                    assert box.call("notes_read", key=keys[0]) is not None
        except BaseException as e:
            errors.append(e)

    write_threads = [threading.Thread(target=write, args=(w,)) for w in range(writers)]
    read_threads = [threading.Thread(target=read) for _ in range(readers)]
    for t in write_threads + read_threads:
        t.start()
    for t in write_threads:
        t.join()
    done.set()
    for t in read_threads:
        t.join()

    assert not errors, errors
    keys = Toolbox(None, path, "a1", []).call("notes_read")
    assert sorted(keys) == sorted(f"w{w}-{i}" for w in range(writers) for i in range(per_writer))
    assert not list(path.parent.glob(".notes-*")), "temp files left behind"


def test_unreadable_notes_read_as_empty(tmp_path):
    path = tmp_path / "notes.json"
    path.write_text("")
    box = Toolbox(None, path, "a1", [])
    assert box.call("notes_read") == []
    box.call("notes_write", key="k", text="v")
    assert box.call("notes_read", key="k") == "v"
