import json
import threading

from pacific.agents.tools import Toolbox


def test_concurrent_notes_writes_lose_nothing(tmp_path):
    """Tasks for the same agent run at once; their notes must not clobber or corrupt each other."""
    path = tmp_path / "agents" / "a" / "notes.json"
    boxes = [Toolbox(None, path, "a", []) for _ in range(8)]
    errors = []

    def write(box, i):
        try:
            for j in range(25):
                box.call("notes_write", key=f"t{i}-{j}", text="x" * 200)
                box.call("notes_read")
        except Exception as e:  # a torn read would surface as JSONDecodeError here
            errors.append(e)

    threads = [threading.Thread(target=write, args=(b, i)) for i, b in enumerate(boxes)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(json.loads(path.read_text())) == 8 * 25
