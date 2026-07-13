import unittest

import file_p2p


class NativeExtensionSmokeTest(unittest.TestCase):
    def test_public_api_is_importable_without_a_device(self):
        for name in (
            "new_p2p_fd",
            "close_p2p_fd",
            "read_file",
            "read_file_batch",
            "drain_read",
        ):
            self.assertTrue(callable(getattr(file_p2p, name, None)), name)


if __name__ == "__main__":
    unittest.main()
