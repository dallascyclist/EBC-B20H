import unittest


class VendoredTests(unittest.TestCase):
    def test_pyserial_is_the_pinned_version(self):
        import serial
        self.assertEqual(serial.__version__, "3.5")

    def test_pyserial_is_loaded_from_the_tree(self):
        import serial
        self.assertIn("/python/lib/serial/", serial.__file__.replace("\\", "/"))

    def test_ebctool_package_imports(self):
        import ebctool
        self.assertEqual(ebctool.__version__, "0.1.0")


if __name__ == "__main__":
    unittest.main()
