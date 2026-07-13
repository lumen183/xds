import errno
import os
import tempfile
import unittest

import file_p2p


class FileP2PMockTest(unittest.TestCase):
    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(delete=False)
        self.temp_file.write(b"0123456789abcdefghijklmnopqrstuvwxyz")
        self.temp_file.close()
        self.fd = file_p2p.new_p2p_fd()

    def tearDown(self):
        file_p2p.close_p2p_fd(self.fd)
        os.unlink(self.temp_file.name)

    def test_single_read_is_visible_after_drain(self):
        address = 0x1000
        result = file_p2p.read_file(
            self.fd, self.temp_file.name, "/dev/mock", 3, address, 6, 0, 0
        )

        self.assertEqual(result, 0)
        self.assertIsNone(file_p2p.get_buffer(address))
        self.assertEqual(file_p2p.drain_read(self.fd), 0)
        self.assertEqual(file_p2p.get_buffer(address), b"345678")

    def test_batch_read(self):
        first_address = 0x2000
        second_address = 0x3000
        result = file_p2p.read_file_batch(
            self.fd,
            self.temp_file.name,
            "/dev/mock",
            [(0, first_address, 4), (10, second_address, 5)],
        )

        self.assertEqual(result, 0)
        self.assertEqual(file_p2p.drain_read(self.fd), 0)
        self.assertEqual(file_p2p.get_buffer(first_address), b"0123")
        self.assertEqual(file_p2p.get_buffer(second_address), b"abcde")

    def test_invalid_lifecycle_and_read_errors(self):
        closed_fd = file_p2p.new_p2p_fd()
        file_p2p.close_p2p_fd(closed_fd)
        self.assertEqual(file_p2p.drain_read(closed_fd), -errno.EBADF)
        self.assertEqual(
            file_p2p.read_file(closed_fd, self.temp_file.name, "/dev/mock", 0, 0x4000, 1, 0, 0),
            -errno.EBADF,
        )
        self.assertEqual(
            file_p2p.read_file(self.fd, "/does/not/exist", "/dev/mock", 0, 0x4000, 1, 0, 0),
            -errno.ENOENT,
        )
        self.assertEqual(
            file_p2p.read_file(self.fd, self.temp_file.name, "/dev/mock", 35, 0x4000, 2, 0, 0),
            -errno.ERANGE,
        )


if __name__ == "__main__":
    unittest.main()
