"""Owned resources for disposable Blue-Green V2 integration tests."""

import json
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


QA_DIRECTORY = Path(__file__).resolve().parent
DEPLOY_DIRECTORY = QA_DIRECTORY.parent
PROJECT_DIRECTORY = DEPLOY_DIRECTORY.parents[1]
OWNER_LABEL = "io.tecnocontrol.bgv2.qa-run"
POSTGRES_IMAGE = "sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
CADDY_IMAGE = "sha256:6aeddd44c3078b0f9a35206472a11420648a79c184603ef95957d0a20044cb2b"
RELEASES = {
    "BLUE": {
        "revision": "4d320b4b1206f1e65eb8fec9e3b183d441271575",
        "tree": "cb00486dd94a3b03ceb69febe43ddca5c549af2d",
        "images": {
            "api": "sha256:921b569ca0f74e34837c7969ec7cbbad457334094bf3a3f3165f23b6d3c119cc",
            "frontend": "sha256:ac2ce3bd0c61b3310add083403be6f6fd8eb52ffc8c1547fb2f674c7f8f62257",
        },
    },
    "GREEN": {
        "revision": "f348076acc84d5e94c9ac261c45310f1a3b57376",
        "tree": "5fea1d24b761ec0b0a66c581053157bb1b6378ab",
        "images": {
            "api": "sha256:8d2be84863addb35b7ad0bbfbe390e14c4e8bcf606232dce6398af89c2fc047f",
            "frontend": "sha256:31d7ef25a56400cb70eb7470a4139daa21da393ca1d094e461e3004fb031798a",
        },
    },
}


def wait_until(probe, *, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(0.1)
    raise RuntimeError("Disposable QA readiness deadline exceeded")


@dataclass(frozen=True)
class Resource:
    kind: str
    name: str
    identity: str


class LocalDocker:
    """Refuse remote Docker contexts and only remove resources created by this run."""

    def __init__(self):
        if sys.platform == "win32":
            self.command = ["docker", "--host", "npipe:////./pipe/dockerDesktopLinuxEngine"]
        elif sys.platform.startswith("linux"):
            self.command = ["docker", "--host", "unix:///var/run/docker.sock"]
        else:
            raise RuntimeError("QA requires a local Linux Docker Engine")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DOCKER_")}
        self.resources = []
        self.nonce = secrets.token_hex(6)
        self.name = f"bgv2-qa-{self.nonce}"

    def invoke(self, *args, input=None, extra_env=None, timeout=45):
        env = {**self.env, **(extra_env or {})}
        return subprocess.run(self.command + list(args), input=input, capture_output=True,
                              env=env, timeout=timeout)

    def run(self, *args, input=None, extra_env=None, timeout=45):
        proc = self.invoke(*args, input=input, extra_env=extra_env, timeout=timeout)
        if proc.returncode:
            raise RuntimeError(f"Local Docker {args[0]} failed with RC {proc.returncode}")
        return proc.stdout.decode("utf-8").strip()

    def inspect(self, kind, identity):
        value, = json.loads(self.run("inspect", "--type", kind, identity))
        return value

    def verify_inputs(self):
        limits = json.loads(self.run("info", "--format", "{{json .}}"))
        if limits["OSType"] != "linux" or limits["NCPU"] < 4 or limits["MemTotal"] < 7_500_000_000:
            raise RuntimeError("Disposable dual-slot QA requires Linux Docker, 4 CPU and 7.5 GB")
        for release in RELEASES.values():
            for image in release["images"].values():
                details = self.inspect("image", image)
                labels = details["Config"]["Labels"]
                if details["Id"] != image or (
                    labels.get("org.opencontainers.image.revision"), labels.get("io.tecnocontrol.git.tree")
                ) != (release["revision"], release["tree"]):
                    raise RuntimeError("The exact sealed release image/OCI labels are required")
        for image in (POSTGRES_IMAGE, CADDY_IMAGE):
            if self.inspect("image", image)["Id"] != image:
                raise RuntimeError("Expected cached QA infrastructure image")
        return {"docker_cpu_count": limits["NCPU"], "docker_memory_bytes": limits["MemTotal"]}

    def create_network(self, role, *, internal=False, subnet=None):
        name = f"{self.name}-{role}"
        args = ["network", "create", "--label", f"{OWNER_LABEL}={self.nonce}"]
        if internal:
            args += ["--internal"]
        if subnet:
            args += ["--subnet", subnet]
        identity = self.run(*args, name)
        self.resources.append(Resource("network", name, identity))
        return name

    def create_volume(self, role):
        name = f"{self.name}-{role}"
        self.run("volume", "create", "--label", f"{OWNER_LABEL}={self.nonce}", name)
        self.resources.append(Resource("volume", name, name))
        return name

    def create_container(self, role, image, *command, args=(), env=None, memory="256m", name=None,
                         cpus="0-3"):
        name = name or f"{self.name}-{role}"
        env = env or {}
        environment = [part for key in env for part in ("--env", key)]
        identity = self.run("create", "--pull=never", "--name", name, "--cpuset-cpus", cpus,
                            "--memory", memory, "--memory-swap", memory, "--restart", "no",
                            "--label", f"{OWNER_LABEL}={self.nonce}", *environment,
                            *args, image, *command, extra_env=env)
        self.resources.append(Resource("container", name, identity))
        return identity

    def execute(self, container, *command, input=None, timeout=45):
        stdin = ("-i",) if input is not None else ()
        return self.invoke("exec", *stdin, container, *command, input=input, timeout=timeout)

    def read(self, container, *command, input=None, timeout=45):
        stdin = ("-i",) if input is not None else ()
        return self.run("exec", *stdin, container, *command, input=input, timeout=timeout)

    def identity(self, container):
        value = self.inspect("container", container)
        return (value["Id"], value["State"]["Pid"], value["State"]["StartedAt"],
                value["RestartCount"], value["State"]["OOMKilled"])

    def cleanup(self):
        errors = []
        for item in reversed(self.resources):
            try:
                value = self.inspect(item.kind, item.identity)
                labels = value["Config"]["Labels"] if item.kind == "container" else value["Labels"]
                if labels.get(OWNER_LABEL) != self.nonce or value["Name"].lstrip("/") != item.name:
                    raise RuntimeError("Cleanup ownership mismatch")
                if item.kind == "container":
                    self.run("container", "rm", "--force", item.identity)
                else:
                    self.run(item.kind, "rm", item.identity)
            except (RuntimeError, KeyError, subprocess.TimeoutExpired) as exc:
                errors.append(f"{item.kind} {item.name}: {exc}")
        if errors:
            raise RuntimeError("QA cleanup could not prove removal of own resources: " + ", ".join(errors))


class DatabaseFixture:
    def __init__(self, docker, app_net, uploads):
        self.docker = docker
        self.app_net = app_net
        self.uploads = uploads
        self.db_name = f"qa_bgv2_{docker.nonce}"
        self.pg_name = f"{docker.name}-postgres"
        self.password = secrets.token_urlsafe(32)
        self.app_password = secrets.token_urlsafe(32)
        self.jwt_secret = secrets.token_urlsafe(48)
        self.player_tokens = [secrets.token_urlsafe(36) for _ in range(4)]
        self.db_url = f"postgresql+psycopg://bgv2qa:{self.password}@{self.pg_name}:5432/{self.db_name}"

    def start(self):
        self.pg = self.docker.create_container("postgres", POSTGRES_IMAGE, "postgres",
            "-c", f"bgv2.qa_run={self.docker.nonce}", "-c", "max_connections=100",
            args=("--network", self.app_net, "--tmpfs", "/var/lib/postgresql/data:rw,nosuid,nodev,size=512m",
                  "--health-cmd", f"pg_isready -U bgv2qa -d {self.db_name}", "--health-interval", "1s"),
            env={"POSTGRES_USER": "bgv2qa", "POSTGRES_PASSWORD": self.password,
                 "POSTGRES_DB": self.db_name}, memory="1536m")
        self.docker.run("start", self.pg)
        wait_until(lambda: self.docker.execute(self.pg, "pg_isready", "-h", "127.0.0.1",
                                               "-d", self.db_name, "-U", "bgv2qa").returncode == 0)
        wait_until(lambda: self.docker.inspect("container", self.pg)["State"]["Health"]["Status"] == "healthy")
        seed = (QA_DIRECTORY / "qa_seed.py").read_bytes()
        env = {"APP_ENV": "production", "AUTO_SEED": "false", "SECRET_KEY": self.jwt_secret,
               "DATABASE_URL": self.db_url, "PYTHONPATH": "/app", "BGV2_QA_NONCE": self.docker.nonce,
               "BGV2_QA_PASSWORD": self.app_password}
        env.update({f"BGV2_QA_PLAYER_{i}": token for i, token in enumerate(self.player_tokens)})
        environment = [part for key in env for part in ("--env", key)]
        output = self.docker.run("run", "--rm", "-i", "--pull=never", "--network", self.app_net,
            "--label", f"{OWNER_LABEL}={self.docker.nonce}", "--workdir", "/app", *environment,
            "--entrypoint", "python", RELEASES["BLUE"]["images"]["api"], "-B", "-",
            input=seed, extra_env=env, timeout=90)
        self.fixture = json.loads(output)
        # Only this synthetic, empty PostgreSQL has been initialized. Create an
        # equivalent test fixture index, then verify it before and after QA.
        self.query("CREATE INDEX ix_player_heartbeats_channel_received_at_id ON "
                   "player_heartbeats (channel_id, received_at DESC, id DESC)")
        self.index = self.index_proof()
        self.identity = self.docker.identity(self.pg)
        return self.fixture

    def query(self, sql):
        return self.docker.read(self.pg, "psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-U", "bgv2qa",
                                "-d", self.db_name, input=sql.encode())

    def readonly(self, sql):
        value = self.query("BEGIN READ ONLY;\n" + sql + ";\nROLLBACK;").splitlines()
        if len(value) < 2 or value[0] != "BEGIN" or value[-1] != "ROLLBACK":
            raise RuntimeError("Disposable database read-only transaction evidence missing")
        return value[1:-1]

    def index_proof(self):
        value, = self.readonly(
            "SELECT indexrelid, indisvalid, indisready, indislive, pg_get_indexdef(indexrelid) "
            "FROM pg_index WHERE indexrelid='ix_player_heartbeats_channel_received_at_id'::regclass"
        )
        return value

    def signature(self):
        value, = self.readonly(
            "SELECT md5(string_agg(concat_ws(':',table_name,column_name,data_type,is_nullable,"
            "column_default),',' ORDER BY table_name, ordinal_position)) "
            "FROM information_schema.columns WHERE table_schema='public'"
        )
        return value
