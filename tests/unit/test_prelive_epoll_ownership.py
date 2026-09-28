import errno
import gc
import os
import select
import unittest


@unittest.skipUnless(os.name == "posix" and hasattr(select, "epoll"),
                     "Linux epoll semantics required")
class EpollOwnershipLabTests(unittest.TestCase):
    """Disposable compatibility checks for the wrapper-owned close boundary."""

    def test_wrapper_close_invalidates_owner_before_fd_reuse(self):
        poller = select.epoll()
        original = poller.fileno()
        poller.close()
        self.assertTrue(poller.closed)
        with self.assertRaises(ValueError):
            poller.fileno()

        read_fd, write_fd = os.pipe()
        try:
            self.assertEqual(read_fd, original)
            poller.close()
            del poller
            gc.collect()
            os.fstat(read_fd)
            os.write(write_fd, b"x")
            self.assertEqual(os.read(read_fd, 1), b"x")
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_anchor_keeps_epoll_ofd_alive_after_wrapper_close(self):
        poller = select.epoll()
        primary = poller.fileno()
        anchor = os.dup(primary)
        try:
            poller.close()
            self.assertTrue(poller.closed)
            os.fstat(anchor)
            with self.assertRaises(ValueError):
                poller.fileno()
        finally:
            os.close(anchor)

    def test_raw_close_leaves_wrapper_able_to_close_recycled_fd(self):
        poller = select.epoll()
        raw = poller.fileno()
        os.close(raw)
        read_fd, write_fd = os.pipe()
        self.assertEqual(read_fd, raw)
        try:
            poller.close()
            self.assertTrue(poller.closed)
            with self.assertRaises(OSError) as raised:
                os.fstat(read_fd)
            self.assertEqual(raised.exception.errno, errno.EBADF)
        finally:
            try:
                os.close(read_fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
            os.close(write_fd)


if __name__ == "__main__":
    unittest.main()
