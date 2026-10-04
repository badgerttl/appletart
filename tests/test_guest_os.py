import unittest

from appletart.guest_os import parse_release


class GuestOSMetadataTests(unittest.TestCase):
    def test_reads_guest_release_independently_of_requested_image_version(self):
        metadata = parse_release('ID=kali\nNAME="Kali GNU/Linux"\nVERSION_ID="2026.1"\nPRETTY_NAME="Kali GNU/Linux Rolling"\n')
        self.assertEqual(metadata["version_id"], "2026.1")
        self.assertEqual(metadata["name"], "Kali GNU/Linux")

    def test_release_data_is_parsed_without_shell_execution_and_is_bounded(self):
        metadata = parse_release('NAME="$(touch /tmp/not-executed)"\nVERSION_ID="unfinished\nUNKNOWN=ignored\n')
        self.assertEqual(metadata, {"name": "$(touch /tmp/not-executed)"})
        self.assertEqual(parse_release('NAME=' + 'x' * 65536), {})
