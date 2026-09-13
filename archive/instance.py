"""OS-released lock: prevents two processes from sharing an active job database."""
import os
from pathlib import Path


class InstanceLock:
    def __init__(self, root):
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.handle = (root / ".instance.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0, 2)
                if self.handle.tell() == 0:
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("This library is already open in another Archive process. Use its browser tab, or quit it before opening again.") from None

    def close(self):
        if self.handle.closed:
            return
        if os.name == "nt":
            import msvcrt
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        self.handle.close()
