import hashlib
import json
import os
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "plugins" / "fit-to-upload" / "skills" / "fit-to-upload" / "scripts" / "fit_to_upload.py"


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
class FitToUploadCharacterizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(["ffmpeg", "-hide_banner", "-h", "encoder=libx264"],
                                capture_output=True, text=True, check=False)
        if result.returncode != 0 or "Encoder libx264" not in (result.stdout + result.stderr):
            raise unittest.SkipTest("ffmpeg with libx264 is required for the encode fixtures")

    def test_encode_meets_exact_byte_cap_preserves_audio_and_original(self):
        with tempfile.TemporaryDirectory(prefix="fit-to-upload-test-") as directory:
            root = Path(directory)
            source = root / "synthetic.mp4"
            output = root / "compressed.mp4"
            self._make_source(source)
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            cap = source.stat().st_size - 1

            completed = subprocess.run(
                [sys.executable, str(CLI), "encode", "--input", str(source), "--output", str(output), "--max-bytes", str(cap)],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["tool"], "fit-to-upload")
            self.assertEqual(result["status"], "ok")
            self.assertLessEqual(output.stat().st_size, cap)
            self.assertEqual(result["data"]["actual_bytes"], output.stat().st_size)
            self.assertEqual(result["data"]["max_bytes"], cap)
            self.assertTrue(result["data"]["validation"]["within_cap"])
            self.assertTrue(result["data"]["validation"]["duration_preserved"])
            self.assertEqual(result["data"]["fidelity"]["audio"], "copied every source audio track")
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
            receipt_path = root / "compressed.mp4.receipt.json"
            self.assertTrue(receipt_path.is_file())
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["data"]["actual_bytes"], output.stat().st_size)

            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)],
                capture_output=True,
                text=True,
                check=True,
            )
            streams = json.loads(probe.stdout)["streams"]
            self.assertTrue(any(stream["codec_type"] == "video" for stream in streams))
            self.assertTrue(any(stream["codec_type"] == "audio" for stream in streams))
            self.assertAlmostEqual(float(result["data"]["duration_seconds"]), 2.0, delta=0.15)

            source_audio = self._audio_packet_evidence(source)
            output_audio = self._audio_packet_evidence(output)
            self.assertTrue(source_audio["packets"])
            self.assertEqual(
                [(p["data_hash"], p["pts_time"], p["duration_time"]) for p in output_audio["packets"]],
                [(p["data_hash"], p["pts_time"], p["duration_time"]) for p in source_audio["packets"]],
                "copied AAC packet payloads and timestamps must match the source",
            )
            self.assertAlmostEqual(output_audio["duration_seconds"], source_audio["duration_seconds"], delta=0.01)

    def test_probe_and_already_fitting_file_are_preserved_byte_for_byte(self):
        with tempfile.TemporaryDirectory(prefix="fit-to-upload-copy-") as directory:
            root = Path(directory)
            source = root / "synthetic.mp4"
            output = root / "same.mp4"
            self._make_source(source)
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()

            probed = self._run_cli("probe", "--input", str(source))
            self.assertEqual(probed.returncode, 0, probed.stderr)
            probe_result = json.loads(probed.stdout)
            self.assertEqual(probe_result["status"], "ok")
            self.assertEqual({stream["type"] for stream in probe_result["data"]["streams"]}, {"video", "audio"})

            cap = source.stat().st_size
            completed = self._run_cli("encode", "--input", str(source), "--output", str(output), "--max-bytes", str(cap))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["data"]["operation"], "copy")
            self.assertEqual(result["data"]["actual_bytes"], cap)
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), original_hash)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)

    def test_decimal_mb_and_binary_mib_have_distinct_exact_byte_caps(self):
        with tempfile.TemporaryDirectory(prefix="fit-to-upload-units-") as directory:
            root = Path(directory)
            source = root / "synthetic.mp4"
            self._make_source(source)
            common = ["plan", "--input", str(source), "--output", str(root / "planned.mp4")]
            mb = self._run_cli(*common, "--max-mb", "1.5")
            mib = self._run_cli(*common, "--max-mib", "1.5")
            self.assertEqual(mb.returncode, 0, mb.stderr)
            self.assertEqual(mib.returncode, 0, mib.stderr)
            self.assertEqual(json.loads(mb.stdout)["data"]["max_bytes"], 1_500_000)
            self.assertEqual(json.loads(mib.stdout)["data"]["max_bytes"], 1_572_864)
            invalid = self._run_cli(*common, "--max-mb", "1", "--max-mib", "1")
            self.assertEqual(invalid.returncode, 2)
            self.assertEqual(json.loads(invalid.stdout)["data"]["error_code"], "invalid_arguments")

    def test_impossible_minimum_cap_returns_audio_fallback_and_no_files(self):
        with tempfile.TemporaryDirectory(prefix="fit-to-upload-small-cap-") as directory:
            root = Path(directory)
            source = root / "synthetic.mp4"
            output = root / "too-small.mp4"
            self._make_source(source)
            completed = self._run_cli("encode", "--input", str(source), "--output", str(output), "--max-bytes", "1")
            self.assertEqual(completed.returncode, 1)
            result = json.loads(completed.stdout)
            self.assertEqual(result["data"]["error_code"], "cap_impossible")
            self.assertIn("--audio", result["data"]["recommendation"])
            self.assertFalse(output.exists())
            self.assertFalse(Path(str(output) + ".receipt.json").exists())
            self.assertEqual(list(root.glob(".too-small.mp4.fit-to-upload-*")), [])

    def test_existing_output_is_refused_and_kept_intact(self):
        with tempfile.TemporaryDirectory(prefix="fit-to-upload-conflict-") as directory:
            root = Path(directory)
            source = root / "synthetic.mp4"
            output = root / "existing.mp4"
            sentinel = b"keep this existing destination"
            self._make_source(source)
            output.write_bytes(sentinel)
            completed = self._run_cli("encode", "--input", str(source), "--output", str(output), "--max-bytes", "1000000")
            self.assertEqual(completed.returncode, 1)
            result = json.loads(completed.stdout)
            self.assertEqual(result["data"]["error_code"], "output_exists")
            self.assertEqual(output.read_bytes(), sentinel)
            self.assertFalse(Path(str(output) + ".receipt.json").exists())

    def test_native_ffmpeg_geometry_error_does_not_publish_partial_files(self):
        with tempfile.TemporaryDirectory(prefix="fit-to-upload-ffmpeg-error-") as directory:
            root = Path(directory)
            source = root / "synthetic-odd-dimensions.mkv"
            output = root / "failed.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=161x121:rate=15:duration=1",
                    "-vf", "format=yuv444p", "-c:v", "ffv1", "-pix_fmt", "yuv444p", "-an",
                    "-f", "matroska", str(source),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            cap = source.stat().st_size - 1
            completed = self._run_cli("encode", "--input", str(source), "--output", str(output), "--max-bytes", str(cap))
            self.assertEqual(completed.returncode, 1, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["data"]["error_code"], "ffmpeg_failed", result)
            self.assertIn("ffmpeg", result["summary"].lower())
            self.assertFalse(output.exists())
            self.assertFalse(Path(str(output) + ".receipt.json").exists())
            self.assertEqual(list(root.glob(".failed.mp4.fit-to-upload-*")), [])

    @unittest.skipUnless(os.name == "posix", "the cancellation test sends SIGINT to a local process")
    def test_cancelled_ffmpeg_process_is_terminated_and_staging_is_removed(self):
        with tempfile.TemporaryDirectory(prefix="fit-to-upload-cancel-") as directory:
            root = Path(directory)
            source = root / "synthetic.mp4"
            output = root / "cancelled.mp4"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_ffmpeg = fake_bin / "ffmpeg"
            fake_ffmpeg.write_text(
                "#!/usr/bin/env python3\n"
                "import sys, time\n"
                "if '-h' in sys.argv:\n"
                "    print('Encoder libx264 [test stub]')\n"
                "    raise SystemExit(0)\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            fake_ffmpeg.chmod(0o755)
            self._make_source(source)
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
            process = subprocess.Popen(
                [sys.executable, str(CLI), "encode", "--input", str(source), "--output", str(output),
                 "--max-bytes", str(source.stat().st_size - 1)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            phase_started = False
            deadline = time.monotonic() + 10
            try:
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([process.stderr], [], [], max(0, deadline - time.monotonic()))
                    if not ready:
                        break
                    line = process.stderr.readline()
                    if "pass 1/2, attempt 1 started" in line:
                        phase_started = True
                        break
                self.assertTrue(phase_started, "the CLI did not reach the ffmpeg pass")
                process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            self.assertEqual(process.returncode, 1, stderr)
            result = json.loads(stdout)
            self.assertEqual(result["data"]["error_code"], "cancelled")
            self.assertFalse(output.exists())
            self.assertFalse(Path(str(output) + ".receipt.json").exists())
            self.assertEqual(list(root.glob(".cancelled.mp4.fit-to-upload-*")), [])

    @staticmethod
    def _run_cli(*arguments):
        return subprocess.run([sys.executable, str(CLI), *arguments], capture_output=True, text=True, check=False)

    @staticmethod
    def _audio_packet_evidence(path):
        packets = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_packets", "-show_data_hash", "sha256",
             "-show_entries", "packet=data_hash,pts_time,duration_time", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        stream = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration",
             "-of", "json", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        packet_data = json.loads(packets.stdout)
        stream_data = json.loads(stream.stdout)["streams"][0]
        return {"packets": packet_data["packets"], "duration_seconds": float(stream_data["duration"])}

    @staticmethod
    def _make_source(path):
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100:duration=2",
                "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
