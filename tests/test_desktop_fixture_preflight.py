"""Reject invalid synthetic credentials before a desktop can open an alert."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desktop_multi_instance_acceptance import require_fresh_synthetic_auth
from native_multi_instance_acceptance import synthetic_jwt


class DesktopFixturePreflight(unittest.TestCase):
    def test_existing_fresh_jwt_fixture_is_accepted(self):
        require_fresh_synthetic_auth({'tokens': {'access_token': synthetic_jwt()}})

    def test_opaque_placeholder_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, 'desktop launch blocked'):
            require_fresh_synthetic_auth({'tokens': {'access_token': 'SYNTHETIC_A'}})

    def test_expired_fixture_is_rejected(self):
        token = synthetic_jwt()
        with patch('official_auth.time.time', return_value=10**12):
            with self.assertRaisesRegex(AssertionError, 'desktop launch blocked'):
                require_fresh_synthetic_auth({'tokens': {'access_token': token}})


if __name__ == '__main__':
    unittest.main()
