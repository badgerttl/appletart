from pathlib import Path
import subprocess
import tempfile
import unittest
import threading
from unittest.mock import patch

from appletart.deployment import DeploymentError
from appletart.image_picker import cancel_picker, choose_directory, choose_image


class ImagePickerTests(unittest.TestCase):
    def test_browser_cancel_terminates_the_pending_process_and_releases_the_picker(self):
        from appletart import image_picker
        import sys
        started = threading.Event()
        popen = subprocess.Popen
        processes = []
        def sleeper(*args, **kwargs):
            process = popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            processes.append(process)
            started.set()
            return process
        results = []
        with patch("appletart.image_picker.sys.platform", "darwin"), patch.object(image_picker.subprocess, "Popen", side_effect=sleeper):
            worker = threading.Thread(target=lambda: results.append(choose_directory(picker_id="test-cancel")))
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                self.assertFalse(cancel_picker("other-request"))
                with self.assertRaisesRegex(DeploymentError, "already open"):
                    choose_image("cloud")
                self.assertTrue(cancel_picker("test-cancel"))
            finally:
                cancel_picker("test-cancel")
                worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(results, [{"cancelled": True, "path": ""}])
        self.assertIsNotNone(processes[0].returncode)
        with patch("appletart.image_picker.sys.platform", "darwin"), patch.object(image_picker, "_run_picker", return_value=None):
            self.assertEqual(choose_directory(picker_id="next-request"), {"cancelled": True, "path": ""})

    def test_cancel_arriving_before_browse_does_not_open_a_panel(self):
        cancel_picker("early-cancel")
        with patch("appletart.image_picker.sys.platform", "darwin"), patch("appletart.image_picker._run_picker") as native:
            self.assertEqual(choose_image("cloud", picker_id="early-cancel"), {"cancelled": True, "path": ""})
            native.assert_not_called()

    def test_native_panel_cancel_returns_no_selection(self):
        with patch("appletart.image_picker.sys.platform", "darwin"), patch("appletart.image_picker._run_picker", return_value=subprocess.CompletedProcess([], 0, "\n", "")):
            self.assertEqual(choose_directory(), {"cancelled": True, "path": ""})

    def test_chooser_returns_a_local_path_including_spaces_without_reading_the_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cloud "$(not-a-command)" arm64.qcow2'
            path.write_bytes(b"disk")
            with patch("appletart.image_picker.sys.platform", "darwin"), patch("appletart.image_picker._run_picker", return_value=subprocess.CompletedProcess([], 0, str(path) + "\n", "")) as run:
                result = choose_image("cloud")
            self.assertEqual(result, {"cancelled": False, "path": str(path.resolve())})
            self.assertNotIn(str(path), run.call_args.args[0])
            self.assertEqual(path.read_bytes(), b"disk")

    def test_cancelled_picker_returns_no_selection_and_releases_its_lock(self):
        cancelled = subprocess.CompletedProcess([], 1, "", "execution error: User canceled. (-128)")
        with patch("appletart.image_picker.sys.platform", "darwin"), patch("appletart.image_picker._run_picker", return_value=cancelled):
            self.assertEqual(choose_image("cloud"), {"cancelled": True, "path": ""})
            self.assertEqual(choose_image("iso"), {"cancelled": True, "path": ""})

    def test_wrong_file_type_empty_files_and_timeout_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch("appletart.image_picker.sys.platform", "darwin"):
            path = Path(directory) / "cloud.raw"
            path.write_bytes(b"disk")
            with patch("appletart.image_picker._run_picker", return_value=subprocess.CompletedProcess([], 0, str(path) + "\n", "")):
                with self.assertRaisesRegex(DeploymentError, "iso installer"):
                    choose_image("iso")
                path.write_bytes(b"")
                with self.assertRaisesRegex(DeploymentError, "nonempty"):
                    choose_image("cloud")
            with patch("appletart.image_picker._run_picker", side_effect=subprocess.TimeoutExpired("osascript", 600)):
                with self.assertRaisesRegex(DeploymentError, "timed out"):
                    choose_image("cloud")

    def test_directory_chooser_returns_a_folder_path_without_copying_its_contents(self):
        with tempfile.TemporaryDirectory(prefix="shared folder ") as directory:
            path = Path(directory)
            (path / "keep.txt").write_text("untouched")
            with patch("appletart.image_picker.sys.platform", "darwin"), patch("appletart.image_picker._run_picker", return_value=subprocess.CompletedProcess([], 0, str(path) + "/\n", "")) as run:
                result = choose_directory()
            self.assertEqual(result, {"cancelled": False, "path": str(path.resolve())})
            self.assertEqual(run.call_args.args[0], "directory")
            self.assertNotIn(str(path), run.call_args.args[0])
            self.assertEqual((path / "keep.txt").read_text(), "untouched")

    def test_directory_picker_cancel_and_invalid_share_paths(self):
        with tempfile.TemporaryDirectory() as directory, patch("appletart.image_picker.sys.platform", "darwin"):
            with patch("appletart.image_picker._run_picker", return_value=subprocess.CompletedProcess([], 1, "", "User canceled. (-128)")):
                self.assertEqual(choose_directory(), {"cancelled": True, "path": ""})
            file = Path(directory) / "file.txt"
            file.write_text("file")
            for selection in (str(file), str(Path(directory) / "missing"), str(Path(directory) / "invalid:share")):
                with self.subTest(selection=selection), patch("appletart.image_picker._run_picker", return_value=subprocess.CompletedProcess([], 0, selection + "\n", "")):
                    with self.assertRaises(DeploymentError):
                        choose_directory()


if __name__ == "__main__":
    unittest.main()
