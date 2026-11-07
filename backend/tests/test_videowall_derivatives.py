"""Actual FFmpeg/FFprobe media QA; isolated private files, no DB/server/hardware."""
from fractions import Fraction
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from unittest import TestCase, skipUnless
from unittest.mock import patch

from app.services.videowall_derivatives import (
    DerivativeError, _run, prepare_node_slices, probe_media,
)


TOOLS_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
COLORS = ((255, 0, 0, 255), (0, 255, 0, 128), (0, 0, 255, 0))
MEDIA_QA_OBSERVATIONS = []


def ffmpeg(*args, input_bytes=None):
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *map(str, args)],
                            input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return result.stdout


@skipUnless(TOOLS_AVAILABLE, "Requires installed FFmpeg/FFprobe; mandatory Linux media QA runs these unchanged")
class VideowallDerivativeMediaTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_directory = TemporaryDirectory(prefix="vw1-master-fixtures-")
        cls.addClassCleanup(cls.fixture_directory.cleanup)
        cls.root = Path(cls.fixture_directory.name)
        cls.images = {}
        cls.videos = {}
        for name, width, height, frames in (("fhd", 5760, 1080, 55), ("4k", 11520, 2160, 3)):
            image = cls.root / (name + ".png")
            row = b"".join(bytes(color) * (width // 3) for color in COLORS)
            ffmpeg("-f", "rawvideo", "-pixel_format", "rgba", "-video_size", f"{width}x{height}",
                   "-i", "pipe:0", "-frames:v", "1", "-threads", "1", image, input_bytes=row * height)
            cls.images[name] = image
            video = cls.root / (name + ".mkv")
            ffmpeg("-f", "image2", "-loop", "1", "-framerate", "25", "-i", image,
                   "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
                   "-frames:v", frames, "-t", str(float(Fraction(frames, 25))), "-c:v", "ffv1",
                   "-level", "3", "-pix_fmt", "yuv420p", "-threads", "1", "-c:a", "pcm_s16le", video)
            cls.videos[name] = video

    def setUp(self):
        self.temporary = self.enterContext(TemporaryDirectory(prefix="vw1-private-output-"))
        self.output = Path(self.temporary) / "slices"

    def image_case(self, name, width, height):
        result = prepare_node_slices(self.images[name], self.output, canvas_width=width,
            canvas_height=height, content_type="image", image_duration_ms=12500)
        self.assertEqual(result.kind, "image")
        self.assertEqual(result.duration_ms, 12500)
        self.assertEqual([slice.position_index for slice in result.slices], [1, 2, 3])
        self.assertEqual(result.source_sha256, hashlib.sha256(self.images[name].read_bytes()).hexdigest())
        for slice, color in zip(result.slices, COLORS):
            probe = probe_media(slice.path, content_type="image")
            self.assertEqual((probe.width, probe.height, probe.codec_name), (width // 3, height, "png"))
            self.assertEqual(slice.mime_type, "image/png")
            self.assertEqual(slice.duration_ms, 12500)
            self.assertEqual(slice.size_bytes, slice.path.stat().st_size)
            self.assertEqual(slice.sha256, hashlib.sha256(slice.path.read_bytes()).hexdigest())
            pixel = ffmpeg("-i", slice.path, "-vf", "crop=1:1:0:0", "-frames:v", "1",
                           "-pix_fmt", "rgba", "-f", "rawvideo", "pipe:1")
            self.assertEqual(pixel, bytes(color), "Exact source region and original alpha must be preserved")
        MEDIA_QA_OBSERVATIONS.append(dict(kind="image", canvas=[width, height], duration_ms=result.duration_ms,
            slices=[dict(position_index=item.position_index, width=item.width, height=item.height,
                         sha256=item.sha256, mime_type=item.mime_type) for item in result.slices],
            exact_rgba_samples=[list(color) for color in COLORS], no_resize=True))

    def video_case(self, name, width, height, frames):
        result = prepare_node_slices(self.videos[name], self.output, canvas_width=width,
            canvas_height=height, content_type="video")
        self.assertEqual((result.fps_num, result.fps_den, result.frame_count), (25, 1, frames))
        self.assertEqual(result.duration_ms, frames * 40)
        probes = [probe_media(slice.path, content_type="video") for slice in result.slices]
        for position, (slice, probe) in enumerate(zip(result.slices, probes), 1):
            self.assertEqual((probe.width, probe.height), (width // 3, height))
            self.assertEqual((probe.codec_name, probe.pixel_format), ("h264", "yuv420p"))
            self.assertEqual((probe.fps_num, probe.fps_den, probe.frame_count, probe.duration_ms),
                             (25, 1, frames, frames * 40))
            self.assertEqual(probe.start_time, 0)
            self.assertEqual(probe.has_audio, position == 1)
            self.assertEqual(slice.has_audio, position == 1)
            self.assertEqual(slice.sha256, hashlib.sha256(slice.path.read_bytes()).hexdigest())
            self.assertEqual(probe.keyframe_indices, probes[0].keyframe_indices)
            self.assertEqual(probe.keyframe_pts_ms, probes[0].keyframe_pts_ms)
        if frames > 50:
            self.assertEqual(probes[0].keyframe_indices, (0, 50))
            self.assertEqual(probes[0].keyframe_pts_ms, (0, 2000))
        MEDIA_QA_OBSERVATIONS.append(dict(kind="video", canvas=[width, height], duration_ms=result.duration_ms,
            fps=[result.fps_num, result.fps_den], frame_count=result.frame_count,
            slices=[dict(position_index=item.position_index, width=item.width, height=item.height,
                         sha256=item.sha256, mime_type=item.mime_type, has_audio=item.has_audio,
                         start_pts_ms=0, keyframe_pts_ms=list(item.keyframe_pts_ms)) for item in result.slices]))

    def test_fullhd_image_three_exact_crops_and_alpha(self):
        self.image_case("fhd", 5760, 1080)

    def test_four_k_image_three_exact_crops_and_alpha(self):
        self.image_case("4k", 11520, 2160)

    def test_fullhd_video_same_cfr_pts_duration_keyframes_and_leader_audio(self):
        self.video_case("fhd", 5760, 1080, 55)

    def test_four_k_video_same_cfr_pts_duration_and_leader_audio(self):
        self.video_case("4k", 11520, 2160, 3)

    def group_master(self, rows, columns, panel_width, panel_height, *, frames=None):
        colors = (*COLORS, (255, 255, 0, 64))
        width, height = columns * panel_width, rows * panel_height
        pixels = b"".join(b"".join(bytes(colors[(row * columns + col) % len(colors)]) * panel_width
                                   for col in range(columns)) * panel_height for row in range(rows))
        image = Path(self.temporary) / "group-master.png"
        ffmpeg("-f", "rawvideo", "-pixel_format", "rgba", "-video_size", f"{width}x{height}",
               "-i", "pipe:0", "-frames:v", "1", "-threads", "1", image, input_bytes=pixels)
        if frames is None:
            return image, colors[:rows * columns]
        video = Path(self.temporary) / "group-master.mkv"
        ffmpeg("-f", "image2", "-loop", "1", "-framerate", "25", "-i", image,
               "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
               "-frames:v", frames, "-t", str(float(Fraction(frames, 25))), "-c:v", "ffv1",
               "-level", "3", "-pix_fmt", "yuv420p", "-threads", "1", "-c:a", "pcm_s16le", video)
        return video, colors[:rows * columns]

    def group_image_case(self, rows, columns, panel_width, panel_height):
        source, colors = self.group_master(rows, columns, panel_width, panel_height)
        result = prepare_node_slices(source, self.output, rows=rows, columns=columns,
            canvas_width=columns * panel_width, canvas_height=rows * panel_height,
            content_type="image", image_duration_ms=12500)
        self.assertEqual([item.position_index for item in result.slices], list(range(1, rows * columns + 1)))
        self.assertEqual(result.source_sha256, hashlib.sha256(source.read_bytes()).hexdigest())
        for item, color in zip(result.slices, colors):
            probe = probe_media(item.path, content_type="image")
            self.assertEqual((probe.width, probe.height, probe.codec_name), (panel_width, panel_height, "png"))
            self.assertEqual((item.width, item.height, item.duration_ms), (panel_width, panel_height, 12500))
            pixel = ffmpeg("-i", item.path, "-vf", "crop=1:1:0:0", "-frames:v", "1",
                           "-pix_fmt", "rgba", "-f", "rawvideo", "pipe:1")
            self.assertEqual(pixel, bytes(color), "Group-local row/column must select the exact original RGBA region")
        MEDIA_QA_OBSERVATIONS.append(dict(kind="image", group_rows=rows, group_columns=columns,
            canvas=[columns * panel_width, rows * panel_height], slices=len(result.slices),
            exact_rgba_samples=[list(color) for color in colors], no_resize=True))

    def group_video_case(self, panel_width, panel_height, frames):
        source, _ = self.group_master(1, 2, panel_width, panel_height, frames=frames)
        result = prepare_node_slices(source, self.output, rows=1, columns=2,
            canvas_width=2 * panel_width, canvas_height=panel_height, content_type="video")
        self.assertEqual((result.fps_num, result.fps_den, result.frame_count, result.duration_ms),
                         (25, 1, frames, frames * 40))
        self.assertEqual([item.position_index for item in result.slices], [1, 2])
        probes = [probe_media(item.path, content_type="video") for item in result.slices]
        for position, probe in enumerate(probes, 1):
            self.assertEqual((probe.width, probe.height), (panel_width, panel_height))
            self.assertEqual((probe.codec_name, probe.pixel_format), ("h264", "yuv420p"))
            self.assertEqual((probe.fps_num, probe.fps_den, probe.frame_count, probe.duration_ms, probe.start_time),
                             (25, 1, frames, frames * 40, 0))
            self.assertEqual(probe.has_audio, position == 1)
            self.assertEqual(probe.keyframe_indices, (0, 50) if frames > 50 else (0,))
            self.assertEqual(probe.keyframe_indices, probes[0].keyframe_indices)
        MEDIA_QA_OBSERVATIONS.append(dict(kind="video", group_rows=1, group_columns=2,
            canvas=[2 * panel_width, panel_height], fps=[25, 1], frame_count=frames,
            duration_ms=result.duration_ms, audio_positions=[item.position_index for item in result.slices if item.has_audio]))

    def test_two_fullhd_image_crops_preserve_source_pixels_and_alpha(self):
        self.group_image_case(1, 2, 1920, 1080)

    def test_two_four_k_image_crops_preserve_source_pixels_and_alpha(self):
        self.group_image_case(1, 2, 3840, 2160)

    def test_two_by_two_fullhd_rectangle_uses_row_major_source_crops(self):
        self.group_image_case(2, 2, 1920, 1080)

    def test_two_fullhd_videos_preserve_shared_timeline_keyframes_and_local_lead_audio(self):
        self.group_video_case(1920, 1080, 55)

    def test_two_four_k_videos_preserve_shared_timeline_and_local_lead_audio(self):
        self.group_video_case(3840, 2160, 3)

    def test_unqualified_capacity_is_rejected_before_reading_master_or_creating_outputs(self):
        with self.assertRaises(DerivativeError) as error:
            prepare_node_slices(Path(self.temporary) / "not-read.png", self.output,
                rows=2, columns=2, canvas_width=7680, canvas_height=4320,
                content_type="image", image_duration_ms=2000)
        self.assertEqual(error.exception.code, "WALL_PROCESSING_CAPACITY_EXCEEDED")
        self.assertFalse(self.output.exists())

    def test_invalid_rectangular_dimensions_are_never_coerced_or_resized(self):
        for values in (dict(rows=True, columns=2, canvas_width=3840, canvas_height=1080),
                       dict(rows=1, columns=0, canvas_width=3840, canvas_height=1080),
                       dict(rows=1.0, columns=2, canvas_width=3840, canvas_height=1080),
                       dict(rows=1, columns=2, canvas_width=3839, canvas_height=1080),
                       dict(rows=1, columns=2, canvas_width=3840.0, canvas_height=1080)):
            with self.subTest(values=values), self.assertRaises(DerivativeError) as error:
                prepare_node_slices(self.images["fhd"], self.output, content_type="image",
                                    image_duration_ms=2000, **values)
            self.assertEqual(error.exception.code, "UNSUPPORTED_WALL_GEOMETRY")
            self.assertFalse(self.output.exists())

    def test_wrong_actual_master_dimensions_fail_without_creating_outputs(self):
        with self.assertRaises(DerivativeError) as error:
            prepare_node_slices(self.images["fhd"], self.output, canvas_width=11520,
                                canvas_height=2160, content_type="image", image_duration_ms=10000)
        self.assertEqual(error.exception.code, "MASTER_DIMENSIONS_MISMATCH")
        self.assertIn("actual 5760x1080", str(error.exception))
        self.assertFalse(self.output.exists())

    def test_existing_output_directory_is_never_overwritten(self):
        self.output.mkdir()
        existing = self.output / "position-1.png"
        existing.write_bytes(b"existing-private-result")
        with self.assertRaises(DerivativeError) as error:
            prepare_node_slices(self.images["fhd"], self.output, canvas_width=5760,
                canvas_height=1080, content_type="image", image_duration_ms=10000)
        self.assertEqual(error.exception.code, "PRIVATE_OUTPUT_UNAVAILABLE")
        self.assertEqual(existing.read_bytes(), b"existing-private-result")

    def test_vfr_master_is_rejected_from_actual_packet_timestamps(self):
        master = Path(self.temporary) / "vfr.mkv"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=96x32:rate=25",
               "-vf", "select='not(eq(n,1))'", "-fps_mode", "vfr", "-frames:v", "3",
               "-c:v", "ffv1", "-threads", "1", master)
        with self.assertRaises(DerivativeError) as error:
            probe_media(master, content_type="video")
        self.assertEqual(error.exception.code, "MASTER_VFR_UNSUPPORTED")

    def test_non_square_pixels_are_rejected(self):
        master = Path(self.temporary) / "sar.mkv"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=96x32:rate=25", "-vf", "setsar=2",
               "-frames:v", "3", "-c:v", "ffv1", "-threads", "1", master)
        with self.assertRaises(DerivativeError) as error:
            probe_media(master, content_type="video")
        self.assertEqual(error.exception.code, "MASTER_SAR_UNSUPPORTED")

    def test_ten_bit_hdr_master_is_not_silently_converted(self):
        master = Path(self.temporary) / "hdr.mkv"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=96x32:rate=25", "-pix_fmt", "yuv420p10le",
               "-color_trc", "smpte2084", "-frames:v", "3", "-c:v", "ffv1", "-threads", "1", master)
        with self.assertRaises(DerivativeError) as error:
            probe_media(master, content_type="video")
        self.assertEqual(error.exception.code, "MASTER_HDR_UNSUPPORTED")

    def test_rotation_metadata_is_rejected_before_cropping(self):
        original, rotated = Path(self.temporary) / "original.mp4", Path(self.temporary) / "rotated.mp4"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=96x32:rate=25", "-frames:v", "3",
               "-c:v", "libx264", "-threads", "1", original)
        ffmpeg("-display_rotation:v:0", "90", "-i", original, "-c", "copy", rotated)
        with self.assertRaises(DerivativeError) as error:
            probe_media(rotated, content_type="video")
        self.assertEqual(error.exception.code, "MASTER_ROTATION_UNSUPPORTED")

    def test_local_playlist_cannot_read_other_files_or_network_urls(self):
        playlist = Path(self.temporary) / "not-a-master.mp4"
        playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nhttps://example.invalid/secret.ts\n")
        with self.assertRaises(DerivativeError) as error:
            probe_media(playlist, content_type="video")
        self.assertEqual(error.exception.code, "MEDIA_PROCESS_FAILED")

    def test_probe_preserves_lost_lease_instead_of_masking_it_as_invalid_media(self):
        with self.assertRaises(DerivativeError) as error:
            probe_media(self.images["fhd"], content_type="image", heartbeat=lambda: False)
        self.assertEqual(error.exception.code, "LEASE_LOST")

    def test_content_type_cannot_disguise_an_image_as_a_video_or_the_reverse(self):
        for path, claimed in ((self.images["fhd"], "video"), (self.videos["fhd"], "image")):
            with self.subTest(claimed=claimed), self.assertRaises(DerivativeError) as error:
                probe_media(path, content_type=claimed)
            self.assertEqual(error.exception.code, "MEDIA_PROCESS_FAILED")

    def test_source_change_between_probe_and_hash_is_not_published_as_the_old_geometry(self):
        source = Path(self.temporary) / "changing.png"
        shutil.copyfile(self.images["fhd"], source)
        ticks = []
        def heartbeat():
            ticks.append(1)
            if len(ticks) == 2:  # FFprobe has returned its original header snapshot.
                source.write_bytes(self.images["4k"].read_bytes())
            return True
        with self.assertRaises(DerivativeError) as error:
            prepare_node_slices(source, self.output, canvas_width=5760, canvas_height=1080,
                                content_type="image", image_duration_ms=10000, heartbeat=heartbeat)
        self.assertEqual(error.exception.code, "MASTER_CHANGED")


class VideowallDerivativeLeaseTests(TestCase):
    def test_urls_are_not_accepted_as_local_inputs(self):
        with self.assertRaises(DerivativeError) as error:
            probe_media("https://example.invalid/master.mp4", content_type="video")
        self.assertEqual(error.exception.code, "LOCAL_MEDIA_REQUIRED")

    def test_lost_lease_kills_only_the_encoder_child(self):
        original_popen = subprocess.Popen
        processes = []
        ticks = []
        def child(*args, **kwargs):
            self.assertIs(kwargs["shell"], False)
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process
        def heartbeat():
            ticks.append(time.monotonic())
            return len(ticks) == 1
        started = time.monotonic()
        with patch("app.services.videowall_derivatives.subprocess.Popen", side_effect=child):
            with self.assertRaises(DerivativeError) as error:
                _run([sys.executable, "-c", "import time; time.sleep(30)"], heartbeat=heartbeat)
        self.assertEqual(error.exception.code, "LEASE_LOST")
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertLess(time.monotonic() - started, 12)


from test_videowall_publications import VideowallPublicationFixture


@skipUnless(TOOLS_AVAILABLE, "Requires real FFmpeg for durable worker integration QA")
class VideowallWorkerIntegrationTests(VideowallPublicationFixture):
    def run_worker(self, **kwargs):
        from sqlalchemy.orm import Session
        from app.workers.videowall import process_next_publication
        with patch("app.workers.videowall.get_settings", return_value=self.settings):
            return process_next_publication(session_factory=lambda: Session(self.engine, autoflush=False),
                worker_id="actual-qa-worker", work_root=Path(self.directory.name) / "private-processing", **kwargs)

    def test_actual_worker_prepares_all_three_owned_files_and_ready_atomically(self):
        from sqlalchemy import select
        from app.models.entities import ContentItem, VideowallPublication, VideowallPublicationAsset
        from app.services.media_references import media_owned_by
        publication_id = self.prepare().id
        self.assertTrue(self.run_worker())
        self.db.expire_all()
        publication = self.db.get(VideowallPublication, publication_id)
        self.assertEqual(publication.status, "READY")
        self.assertIsNone(publication.loop_start_epoch_ms)
        assets = list(self.db.scalars(select(VideowallPublicationAsset).where(
            VideowallPublicationAsset.publication_id == publication_id).order_by(VideowallPublicationAsset.position_index)))
        self.assertEqual([asset.position_index for asset in assets], [1, 2, 3])
        self.assertTrue(all(asset.validated for asset in assets))
        self.assertEqual(len({asset.derived_content_id for asset in assets}), 3)
        for asset in assets:
            content = self.db.get(ContentItem, asset.derived_content_id)
            self.assertEqual(content.client_id, self.client.id)
            path = self.root / content.file_path.removeprefix("/media/")
            self.assertTrue(media_owned_by(self.db, self.root, path, self.client.id))
            probe = probe_media(path, content_type="image")
            self.assertEqual((probe.width, probe.height), (1920, 1080))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), asset.checksum)
        self.assertFalse(self.run_worker(), "READY work is not claimed again")

    def test_actual_worker_prepares_two_group_local_slices_without_a_third_output(self):
        from sqlalchemy import select
        from app.models.entities import ContentItem, VideowallPublication, VideowallPublicationAsset
        from app.services.media_references import media_owned_by
        group = self.configure_group((1, 2))
        publication_id = self.prepare(group).id
        self.assertTrue(self.run_worker())
        self.db.expire_all()
        publication = self.db.get(VideowallPublication, publication_id)
        self.assertEqual((publication.status, publication.expected_asset_count, publication.playback_group_id),
                         ("READY", 2, group.id))
        assets = list(self.db.scalars(select(VideowallPublicationAsset).where(
            VideowallPublicationAsset.publication_id == publication_id).order_by(VideowallPublicationAsset.position_index)))
        self.assertEqual([(item.node_id, item.position_index) for item in assets],
                         [(self.nodes[1].id, 1), (self.nodes[2].id, 2)])
        self.assertEqual(len({item.derived_content_id for item in assets}), 2)
        for asset in assets:
            content = self.db.get(ContentItem, asset.derived_content_id)
            path = self.root / content.file_path.removeprefix("/media/")
            self.assertTrue(media_owned_by(self.db, self.root, path, self.client.id))
            probe = probe_media(path, content_type="image")
            self.assertEqual((probe.width, probe.height), (1920, 1080))

    def test_failed_processor_cannot_replace_previous_active_publication(self):
        from sqlalchemy import func, select
        from app.models.entities import ContentItem, VideowallPublication, VideowallPublicationAsset
        from app.services.videowall_publications import activate_publication
        previous = self.ready()
        activate_publication(self.db, self.wall, previous.id, self.actor)
        previous_id = previous.id
        previous_epoch = previous.loop_start_epoch_ms
        pending_id = self.prepare().id
        before_count = self.db.scalar(select(func.count()).select_from(ContentItem))
        with patch("app.workers.videowall.prepare_node_slices",
                   side_effect=DerivativeError("SLICE_TIMELINE_MISMATCH", "Synthetic rejected slice")):
            self.assertTrue(self.run_worker())
        self.db.expire_all()
        self.assertEqual(self.db.get(VideowallPublication, pending_id).status, "FAILED")
        self.assertEqual(self.wall.active_publication_id, previous_id)
        old = self.db.get(VideowallPublication, previous_id)
        self.assertEqual((old.status, old.loop_start_epoch_ms), ("ACTIVE", previous_epoch))
        assets = list(self.db.scalars(select(VideowallPublicationAsset).where(
            VideowallPublicationAsset.publication_id == pending_id)))
        self.assertTrue(all(asset.derived_content_id is None and not asset.validated for asset in assets))
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ContentItem)), before_count)

    def test_expired_worker_lease_recovers_and_stale_owner_cannot_change_ready_result(self):
        from datetime import UTC, datetime, timedelta
        from app.models.entities import VideowallPublication
        from app.services.videowall_publications import claim_publication, fail_publication, renew_publication_lease
        publication_id = self.prepare().id
        old = claim_publication(self.db, "old-qa-worker", lease_seconds=1)
        publication = self.db.get(VideowallPublication, publication_id)
        publication.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        self.db.commit()
        self.assertTrue(self.run_worker())
        self.db.expire_all()
        publication = self.db.get(VideowallPublication, publication_id)
        self.assertEqual((publication.status, publication.attempt_count), ("READY", 2))
        self.assertFalse(renew_publication_lease(self.db, publication_id, old.lease_token))
        self.assertFalse(fail_publication(self.db, publication_id, old.lease_token, "OLD_WORKER_FAILED"))
        self.db.refresh(publication)
        self.assertEqual(publication.status, "READY")


class VideowallCleanupReferenceTests(VideowallPublicationFixture):
    def test_cleanup_preserves_master_derivatives_and_campaign_for_every_publication_state(self):
        from datetime import UTC, datetime
        from sqlalchemy import select
        from app.models.entities import Campaign, ContentItem, ContentType, VideowallPublicationAsset
        from app.services.cleanup_retention import _campaign_has_live_references, _content_has_live_references
        publication = self.ready()
        # The current playlist can change independently of its immutable snapshot.
        self.db.delete(self.item)
        unused = ContentItem(client_id=self.client.id, name="Unreferenced", type=ContentType.IMAGE)
        unused_campaign = Campaign(client_id=self.client.id, name="Unreferenced campaign")
        self.db.add_all([unused, unused_campaign])
        self.db.commit()
        derived = [self.db.get(ContentItem, identity) for identity in self.db.scalars(
            select(VideowallPublicationAsset.derived_content_id).where(
                VideowallPublicationAsset.publication_id == publication.id))]
        for status in ("PENDING", "PROCESSING", "READY", "ACTIVE", "FAILED"):
            publication.status = status
            if status == "ACTIVE":
                publication.loop_start_epoch_ms = 1793793605000
                publication.activated_at = datetime.now(UTC)
            self.db.commit()
            with self.subTest(status=status):
                self.assertTrue(_content_has_live_references(self.master, self.db,
                                                           removing_campaign_ids=(self.campaign.id,)))
                for content in derived:
                    self.assertTrue(_content_has_live_references(content, self.db))
                self.assertTrue(_campaign_has_live_references(self.campaign, self.db))
                self.assertFalse(_content_has_live_references(unused, self.db))
                self.assertFalse(_campaign_has_live_references(unused_campaign, self.db))

    def test_delete_guard_rejects_referenced_master_and_derived_before_mutation(self):
        from fastapi import HTTPException
        from sqlalchemy import func, select
        from app.models.entities import ContentItem, VideowallPublicationAsset
        from app.services.tenant_mutations import delete_guard
        publication = self.ready()
        derived_id = self.db.scalar(select(VideowallPublicationAsset.derived_content_id).where(
            VideowallPublicationAsset.publication_id == publication.id).limit(1))
        before_count = self.db.scalar(select(func.count()).select_from(ContentItem))
        for identity in (self.master.id, derived_id):
            content = self.db.get(ContentItem, identity)
            path = self.root / content.file_path.removeprefix("/media/")
            original = path.read_bytes()
            entered = False
            with self.assertRaises(HTTPException) as error:
                with delete_guard(self.db, (content,), actor=self.actor, operation="delete_content"):
                    entered = True
            self.assertEqual(error.exception.status_code, 409)
            self.assertFalse(entered)
            self.db.rollback()
            self.assertIsNotNone(self.db.get(ContentItem, identity))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(self.db.scalar(select(func.count()).select_from(ContentItem)), before_count)

    def test_master_type_mutation_cannot_invalidate_a_processing_publication(self):
        from fastapi import HTTPException
        from app.models.entities import ContentItem, ContentType
        from app.services.tenant_mutations import MutationIntent, write_guard
        from app.services.tenant_ownership import RowRef
        from app.services.videowall_publications import claim_publication
        self.prepare()
        self.assertIsNotNone(claim_publication(self.db, "source-change-guard-qa"))
        identity = self.master.id
        intent = MutationIntent("UPDATE", RowRef("ContentItem", identity), (("type", ContentType.VIDEO),))
        entered = False
        with self.assertRaises(HTTPException) as error:
            with write_guard(self.db, actor=self.actor, client_id=self.client.id,
                             operation="move_content_to_folder", intents=(intent,)):
                entered = True
        self.assertEqual(error.exception.status_code, 409)
        self.assertFalse(entered)
        self.db.rollback()
        self.assertEqual(self.db.get(ContentItem, identity).type, ContentType.IMAGE)
