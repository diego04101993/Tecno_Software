from copy import deepcopy
import json
from unittest import TestCase
from unittest.mock import patch

from gates import BACKEND_ARGV, REVISION_LABEL, TREE_LABEL
from qualify_r3 import R3_CADDY_ARGV, R3_IMAGES, qualify, qualify_role
from release_state import Stop


RELEASE = {"revision": "a" * 40, "tree": "b" * 40, "images": R3_IMAGES}
IDS = {"api": "c" * 64, "frontend": "d" * 64}


def fixture(role):
    service = "backend" if role == "api" else "frontend"
    command = BACKEND_ARGV if role == "api" else R3_CADDY_ARGV
    image = {"Id": R3_IMAGES[role], "Os": "linux", "Architecture": "amd64", "RepoDigests": None,
             "Config": {"Entrypoint": None, "Cmd": command[:-2] if role == "api" else command,
                        "Env": ["SECRET_KEY=should_never_be_in_a_receipt"],
                        "Labels": {REVISION_LABEL: RELEASE["revision"], TREE_LABEL: RELEASE["tree"]}},
             "RootFS": {"Type": "layers", "Layers": ["sha256:" + "e" * 64]}}
    container = {"Id": IDS[role], "Name": "/tecnocontrol-" + service + "-1", "Image": image["Id"],
                 "Created": "2026-11-10T00:00:00Z", "RestartCount": 0, "Path": command[0], "Args": command[1:],
                 "Config": {**image["Config"], "Image": image["Id"], "Cmd": command,
                            "Labels": {**image["Config"]["Labels"], "com.docker.compose.project": "tecnocontrol",
                                       "com.docker.compose.service": service}},
                 "HostConfig": {}, "Mounts": [], "NetworkSettings": {"Networks": {}},
                 "State": {"Running": True, "Status": "running", "OOMKilled": False, "Paused": False,
                           "Dead": False, "Restarting": False, "StartedAt": "2026-11-11T00:00:00Z", "Pid": 123}}
    if role == "api":
        container["State"]["Health"] = {"Status": "healthy"}
    return container, image


class R3QualificationTests(TestCase):
    def test_exact_live_ids_images_commands_and_layer_receipt(self):
        observations = {role: fixture(role) for role in IDS}
        caddyfile = b"test fixture"
        from hashlib import sha256

        calls = []

        def read(*args):
            calls.append(args)
            if args[0] == "container":
                role = next(key for key, value in IDS.items() if value == args[2])
                return json.dumps([observations[role][0]]).encode()
            if args[0] == "image":
                role = next(key for key, value in R3_IMAGES.items() if value == args[2])
                return json.dumps([observations[role][1]]).encode()
            if args[-1] == "/etc/caddy/Caddyfile":
                return caddyfile
            command = BACKEND_ARGV if args[1] == IDS["api"] else R3_CADDY_ARGV
            return ("\0".join(command) + "\0").encode()

        with patch("qualify_r3.R3_CADDYFILE_SHA256", sha256(caddyfile).hexdigest()):
            result = qualify(RELEASE, IDS, read)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["roles"]["api"]["repo_digests"], [])
        self.assertEqual(result["roles"]["frontend"]["image_id"], R3_IMAGES["frontend"])
        self.assertEqual(len(result["roles"]["api"]["rootfs_layers"]), 1)
        self.assertNotIn("SECRET_KEY", json.dumps(result))
        self.assertTrue(all(call[:2] in (("image", "inspect"), ("container", "inspect"))
                            or call[:1] == ("exec",) and call[2] == "cat" for call in calls))

    def test_rejects_wrong_image_before_any_runtime_exec(self):
        container, _ = fixture("api")
        container["Image"] = "sha256:" + "f" * 64
        def read(*args):
            self.assertEqual(args, ("container", "inspect", IDS["api"]))
            return json.dumps([container]).encode()
        with self.assertRaisesRegex(Stop, "R3_CONTAINER_IMAGE_IDENTITY_MISMATCH"):
            qualify(RELEASE, IDS, read)

    def test_native_frontend_requires_project_provenance_labels(self):
        container, image = fixture("frontend")
        image["Config"]["Labels"].pop(TREE_LABEL)
        container["Config"]["Labels"].pop(TREE_LABEL)
        with self.assertRaisesRegex(Stop, "R3_OCI_PROVENANCE_MISMATCH"):
            qualify_role("frontend", RELEASE, IDS["frontend"], container, container, image, R3_CADDY_ARGV)

    def test_legacy_api_accepts_exactly_absent_oci_labels(self):
        container, image = fixture("api")
        for label in (REVISION_LABEL, TREE_LABEL):
            container["Config"]["Labels"].pop(label)
            image["Config"]["Labels"].pop(label)
        result = qualify_role("api", RELEASE, IDS["api"], container, container, image, BACKEND_ARGV)
        self.assertIsNone(result["image_oci_revision"])

    def test_changed_epoch_or_wrong_runtime_is_rejected(self):
        before, image = fixture("api")
        after = deepcopy(before)
        after["State"]["Pid"] = 124
        with self.assertRaisesRegex(Stop, "R3_RUNTIME_IDENTITY_CHANGED_OR_UNHEALTHY"):
            qualify_role("api", RELEASE, IDS["api"], before, after, image, BACKEND_ARGV)
        with self.assertRaisesRegex(Stop, "R3_LIVE_COMMAND_MISMATCH"):
            qualify_role("api", RELEASE, IDS["api"], before, before, image, BACKEND_ARGV[:-2])

    def test_wrong_architecture_layer_or_command_is_rejected(self):
        for mutate, code in ((lambda image: image.update(Architecture="arm64"), "R3_IMAGE_PLATFORM_MISMATCH"),
                             (lambda image: image["RootFS"].update(Layers=[]), "R3_ROOTFS_INVALID"),
                             (lambda image: image["Config"].update(Cmd=["different"]), "R3_COMMAND_MISMATCH")):
            with self.subTest(code=code):
                container, image = fixture("api")
                mutate(image)
                with self.assertRaisesRegex(Stop, code):
                    qualify_role("api", RELEASE, IDS["api"], container, container, image, BACKEND_ARGV)
