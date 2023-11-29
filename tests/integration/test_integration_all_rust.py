"""
Rust Connection and Endpoint Node Integration Tests
"""

import base64
import copy
import json
import logging
import os
import random
import signal
import socket
import string
import subprocess
import sys
import time
import uuid
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any
from unittest import SkipTest
from urllib.parse import urlparse

import bottle
import ecdsa
import psutil
import requests
import twisted.internet.base
import websocket
from cryptography.fernet import Fernet
from jose import jws
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet.threads import deferToThread
from twisted.trial import unittest

from .db import (
    DynamoDBResource,
    base64url_encode,
    create_message_table_ddb,
    get_router_table,
)

app = bottle.Bottle()
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

here_dir = os.path.abspath(os.path.dirname(__file__))
tests_dir = os.path.dirname(here_dir)
root_dir = os.path.dirname(tests_dir)

DDB_JAR = os.path.join(root_dir, "tests", "integration", "ddb", "DynamoDBLocal.jar")
DDB_LIB_DIR = os.path.join(root_dir, "tests", "integration", "ddb", "DynamoDBLocal_lib")
DDB_PROCESS: subprocess.Popen | None = None
BT_PROCESS: subprocess.Popen | None = None
BT_DB_SETTINGS: str | None = None

twisted.internet.base.DelayedCall.debug = True

ROUTER_TABLE = os.environ.get("ROUTER_TABLE", "router_int_test")
MESSAGE_TABLE = os.environ.get("MESSAGE_TABLE", "message_int_test")
MSG_LIMIT = 20

CRYPTO_KEY = os.environ.get("CRYPTO_KEY") or Fernet.generate_key().decode("utf-8")
CONNECTION_PORT = 9150
ENDPOINT_PORT = 9160
ROUTER_PORT = 9170
MP_CONNECTION_PORT = 9052
MP_ROUTER_PORT = 9072

CONNECTION_BINARY = os.environ.get("CONNECTION_BINARY", "autopush_rs")
CONNECTION_SETTINGS_PREFIX = os.environ.get("CONNECTION_SETTINGS_PREFIX", "autopush__")

CN_SERVER: subprocess.Popen | None = None
CN_MP_SERVER: subprocess.Popen | None = None
EP_SERVER: subprocess.Popen | None = None
MOCK_SERVER_THREAD = None
CN_QUEUES: list = []
EP_QUEUES: list = []
STRICT_LOG_COUNTS = True

modules = [
    "autoconnect",
    "autoconnect_common",
    "autoconnect_web",
    "autoconnect_ws",
    "autoconnect_ws_sm",
    "autoendpoint",
    "autopush",
    "autopush_common",
]
log_string = [f"{x}=trace" for x in modules]
RUST_LOG = ",".join(log_string) + ",error"


def get_free_port() -> int:
    port: int
    s = socket.socket(socket.AF_INET, type=socket.SOCK_STREAM)
    s.bind(("localhost", 0))
    address, port = s.getsockname()
    s.close()
    return port


"""Try reading the database settings from the environment

If that points to a file, read the settings from that file.
"""


def get_db_settings() -> str | dict[str, str | int | float] | None:
    env_var = os.environ.get("DB_SETTINGS")
    if env_var:
        if os.path.isfile(env_var):
            with open(env_var, "r") as f:
                return f.read()
        return env_var
    return json.dumps(
        dict(
            router_table=ROUTER_TABLE,
            message_table=MESSAGE_TABLE,
            current_message_month=MESSAGE_TABLE,
            table_name="projects/test/instances/test/tables/autopush",
            router_family="router",
            message_family="message",
            message_topic_family="message_topic",
        )
    )


MOCK_SERVER_PORT: Any = get_free_port()
MOCK_MP_SERVICES: dict = {}
MOCK_MP_TOKEN: str = "Bearer {}".format(uuid.uuid4().hex)
MOCK_MP_POLLED: Event = Event()
MOCK_SENTRY_QUEUE: Queue = Queue()

"""Connection Server Config:
For local test debugging, set `AUTOPUSH_CN_CONFIG=_url_` to override
creation of the local server.
"""
CONNECTION_CONFIG: dict[str, Any] = dict(
    hostname="localhost",
    port=CONNECTION_PORT,
    endpoint_hostname="localhost",
    endpoint_port=ENDPOINT_PORT,
    router_port=ROUTER_PORT,
    endpoint_scheme="http",
    router_tablename=ROUTER_TABLE,
    message_tablename=MESSAGE_TABLE,
    crypto_key="[{}]".format(CRYPTO_KEY),
    auto_ping_interval=30.0,
    auto_ping_timeout=10.0,
    close_handshake_timeout=5,
    max_connections=5000,
    human_logs="true",
    msg_limit=MSG_LIMIT,
    # new autoconnect
    db_dsn=os.environ.get("DB_DSN", "http://127.0.0.1:8000"),
    db_settings=get_db_settings(),
)

"""Connection Megaphone Config:
For local test debugging, set `AUTOPUSH_MP_CONFIG=_url_` to override
creation of the local server.
"""
MEGAPHONE_CONFIG: dict[str, str | int | float | None] = copy.deepcopy(CONNECTION_CONFIG)
MEGAPHONE_CONFIG.update(
    port=MP_CONNECTION_PORT,
    endpoint_port=ENDPOINT_PORT,
    router_port=MP_ROUTER_PORT,
    auto_ping_interval=0.5,
    auto_ping_timeout=10.0,
    close_handshake_timeout=5,
    max_connections=5000,
    megaphone_api_url="http://localhost:{port}/v1/broadcasts".format(port=MOCK_SERVER_PORT),
    megaphone_api_token=MOCK_MP_TOKEN,
    megaphone_poll_interval=1,
)

"""Endpoint Server Config:
For local test debugging, set `AUTOPUSH_EP_CONFIG=_url_` to override
creation of the local server.
"""
ENDPOINT_CONFIG = dict(
    host="localhost",
    port=ENDPOINT_PORT,
    router_table_name=ROUTER_TABLE,
    message_table_name=MESSAGE_TABLE,
    human_logs="true",
    crypto_keys="[{}]".format(CRYPTO_KEY),
)


class Client(object):
    """Test Client"""

    def __init__(self, url) -> None:
        self.url: str = url
        self.uaid: uuid.UUID | None = None
        self.ws: websocket.WebSocket | None = None
        self.use_webpush: bool = True
        self.channels: dict[str, str] = {}
        self.messages: dict[str, str] = {}
        self.notif_response: requests.Response | None = None
        self._crypto_key: str = """\
keyid="http://example.org/bob/keys/123";salt="XZwpw6o37R-6qoZjw6KwAw=="\
"""
        self.headers: dict[str, str] = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.13; rv:61.0) "
            "Gecko/20100101 Firefox/61.0"
        }

    def __getattribute__(self, name: str):
        # Python fun to turn all functions into deferToThread functions
        f = object.__getattribute__(self, name)
        if name.startswith("__"):
            return f

        if callable(f):
            return lambda *args, **kwargs: deferToThread(f, *args, **kwargs)
        else:
            return f

    def connect(self, connection_port: int | None = None):
        url = self.url
        if connection_port:  # pragma: nocover
            url = "ws://localhost:{}/".format(connection_port)
        self.ws = websocket.create_connection(url, header=self.headers)
        return self.ws.connected

    def hello(self, uaid: str | None = None, services: list[str] | None = None):
        if not self.ws:
            raise Exception("WebSocket client not available as expected")

        if self.channels:
            chans = list(self.channels.keys())
        else:
            chans = []
        hello_dict: dict[str, Any] = dict(messageType="hello", use_webpush=True, channelIDs=chans)
        if uaid or self.uaid:
            hello_dict["uaid"] = uaid or self.uaid
        if services:  # pragma: nocover
            hello_dict["broadcasts"] = services
        msg = json.dumps(hello_dict)
        log.debug("Send: %s", msg)
        self.ws.send(msg)
        reply = self.ws.recv()
        log.debug(f"Recv: {reply} ({len(reply)})")
        result = json.loads(reply)
        assert result["status"] == 200
        assert "-" not in result["uaid"]
        if self.uaid and self.uaid != result["uaid"]:  # pragma: nocover
            log.debug(
                "Mismatch on re-using uaid. Old: %s, New: %s",
                self.uaid,
                result["uaid"],
            )
            self.channels = {}
        self.uaid = result["uaid"]
        return result

    def broadcast_subscribe(self, services: list[str]):
        if not self.ws:
            raise Exception("WebSocket client not available as expected")

        msg = json.dumps(dict(messageType="broadcast_subscribe", broadcasts=services))
        log.debug("Send: %s", msg)
        self.ws.send(msg)

    def register(self, chid: str | None = None, key=None, status=200):
        if not self.ws:
            raise Exception("WebSocket client not available as expected")

        chid = chid or str(uuid.uuid4())
        msg = json.dumps(dict(messageType="register", channelID=chid, key=key))
        log.debug("Send: %s", msg)
        self.ws.send(msg)
        rcv = self.ws.recv()
        result = json.loads(rcv)
        log.debug("Recv: %s", result)
        assert result["status"] == status
        assert result["channelID"] == chid
        if status == 200:
            self.channels[chid] = result["pushEndpoint"]
        return result

    def unregister(self, chid):
        msg = json.dumps(dict(messageType="unregister", channelID=chid))
        log.debug("Send: %s", msg)
        self.ws.send(msg)
        result = json.loads(self.ws.recv())
        log.debug("Recv: %s", result)
        return result

    def delete_notification(self, channel, message=None, status=204):
        messages = self.messages[channel]
        if not message:
            message = random.choice(messages)

        log.debug("Delete: %s", message)
        url = urlparse(message)
        resp = requests.delete(url=url.geturl())
        assert resp.status_code == status

    def send_notification(
        self,
        channel=None,
        version=None,
        data=None,
        use_header=True,
        status=None,
        ttl=200,
        timeout=0.2,
        vapid=None,
        endpoint=None,
        topic=None,
        headers=None,
    ):
        if not channel:
            channel = random.choice(list(self.channels.keys()))

        endpoint = endpoint or self.channels[channel]
        url = urlparse(endpoint)

        headers = {}
        if ttl is not None:
            headers = {"TTL": str(ttl)}
        if use_header:
            headers.update(
                {
                    "Content-Type": "application/octet-stream",
                    "Content-Encoding": "aesgcm",
                    "Encryption": self._crypto_key,
                    "Crypto-Key": 'keyid="a1"; dh="JcqK-OLkJZlJ3sJJWstJCA"',
                }
            )
        if vapid:
            headers.update({"Authorization": "Bearer " + vapid.get("auth")})
            ckey = 'p256ecdsa="' + vapid.get("crypto-key") + '"'
            headers.update({"Crypto-Key": headers.get("Crypto-Key", "") + ";" + ckey})
        if topic:
            headers["Topic"] = topic
        body = data or ""
        method = "POST"
        # 202 status reserved for yet to be implemented push w/ reciept.
        status = status or 201

        log.debug("%s body: %s", method, body)
        log.debug("  headers: %s", headers)
        resp = requests.request(method=method, url=url.geturl(), data=body, headers=headers)
        log.debug("%s Response (%s): %s", method, resp.status_code, resp.text)
        assert resp.status_code == status, "Expected %d, got %d" % (
            status,
            resp.status_code,
        )
        self.notif_response = resp
        location = resp.headers.get("Location", None)
        log.debug("Response Headers: %s", resp.headers)
        if status >= 200 and status < 300:
            assert location is not None
        if status == 201 and ttl is not None:
            ttl_header = resp.headers.get("TTL")
            assert ttl_header == str(ttl)
        if ttl != 0 and status == 201:
            assert location is not None
            if channel in self.messages:
                self.messages[channel].append(location)
            else:
                self.messages[channel] = [location]
        # Pull the notification if connected
        if self.ws and self.ws.connected:
            return object.__getattribute__(self, "get_notification")(timeout)
        else:
            return resp

    def get_notification(self, timeout=1):
        orig_timeout = self.ws.gettimeout()
        self.ws.settimeout(timeout)
        try:
            d = self.ws.recv()
            log.debug("Recv: %s", d)
            return json.loads(d)
        except Exception:
            return None
        finally:
            self.ws.settimeout(orig_timeout)

    def get_broadcast(self, timeout=1):  # pragma: nocover
        orig_timeout = self.ws.gettimeout()
        self.ws.settimeout(timeout)
        try:
            d = self.ws.recv()
            log.debug("Recv: %s", d)
            result = json.loads(d)
            assert result.get("messageType") == "broadcast"
            return result
        except Exception as ex:  # pragma: nocover
            log.error("Error: {}".format(ex))
            return None
        finally:
            self.ws.settimeout(orig_timeout)

    def ping(self):
        log.debug("Send: %s", "{}")
        self.ws.send("{}")
        result = self.ws.recv()
        log.debug("Recv: %s", result)
        assert result == "{}"
        return result

    def ack(self, channel, version):
        msg = json.dumps(
            dict(
                messageType="ack",
                updates=[dict(channelID=channel, version=version)],
            )
        )
        log.debug("Send: %s", msg)
        self.ws.send(msg)

    def disconnect(self):
        self.ws.close()

    def sleep(self, duration: int):  # pragma: nocover
        time.sleep(duration)

    def wait_for(self, func):
        """Waits several seconds for a function to return True"""
        times = 0
        while not func():  # pragma: nocover
            time.sleep(1)
            times += 1
            if times > 9:  # pragma: nocover
                break


def _get_vapid(
    key: ecdsa.SigningKey | None = None,
    payload: dict[str, str | int] | None = None,
    endpoint: str | None = None,
) -> dict[str, str | bytes]:
    global CONNECTION_CONFIG

    if endpoint is None:
        endpoint = "{}://{}:{}".format(
            CONNECTION_CONFIG.get("endpoint_scheme"),
            CONNECTION_CONFIG.get("endpoint_hostname"),
            CONNECTION_CONFIG.get("endpoint_port"),
        )
    if not payload:
        payload = {
            "aud": endpoint,
            "exp": int(time.time()) + 86400,
            "sub": "mailto:admin@example.com",
        }
    if not payload.get("aud"):
        payload["aud"] = endpoint
    if not key:
        key = ecdsa.SigningKey.generate(curve=ecdsa.NIST256p)
    vk: ecdsa.VerifyingKey = key.get_verifying_key()
    auth: str = jws.sign(payload, key, algorithm="ES256").strip("=")
    crypto_key: str = base64url_encode((b"\4" + vk.to_string()))
    return {"auth": auth, "crypto-key": crypto_key, "key": key}


def enqueue_output(out, queue):
    for line in iter(out.readline, b""):
        queue.put(line)
    out.close()


def print_lines_in_queues(queues, prefix):
    for queue in queues:
        is_empty = False
        while not is_empty:
            try:
                line = queue.get_nowait()
            except Empty:
                is_empty = True
            else:
                sys.stdout.write(prefix + line)


def process_logs(testcase):
    """Process (print) the testcase logs (in tearDown).

    Ensures a maximum level of logs allowed to be emitted when running
    w/ a `--release` mode connection/endpoint node

    """
    conn_count = sum(queue.qsize() for queue in CN_QUEUES)
    endpoint_count = sum(queue.qsize() for queue in EP_QUEUES)

    print_lines_in_queues(CN_QUEUES, f"{CONNECTION_BINARY.upper()}: ")
    print_lines_in_queues(EP_QUEUES, "AUTOENDPOINT: ")

    if not STRICT_LOG_COUNTS:
        return

    msg = "endpoint node emitted excessive log statements, count: {} > max: {}"
    # Give an extra to endpoint for potential startup log messages
    # (e.g. when running tests individually)
    max_endpoint_logs = testcase.max_endpoint_logs + 1
    assert endpoint_count <= max_endpoint_logs, msg.format(endpoint_count, max_endpoint_logs)

    msg = "conn node emitted excessive log statements, count: {} > max: {}"
    assert conn_count <= testcase.max_conn_logs, msg.format(conn_count, testcase.max_conn_logs)


def max_logs(endpoint=None, conn=None):
    """Adjust max_endpoint/conn_logs values for individual test cases.

    They're utilized by the process_logs function

    """

    def max_logs_decorator(func):
        def wrapper(self, *args, **kwargs):
            if endpoint is not None:
                self.max_endpoint_logs = endpoint
            if conn is not None:
                self.max_conn_logs = conn
            return func(self, *args, **kwargs)

        return wrapper

    return max_logs_decorator


@app.get("/v1/broadcasts")
def broadcast_handler():
    assert bottle.request.headers["Authorization"] == MOCK_MP_TOKEN
    MOCK_MP_POLLED.set()
    return dict(broadcasts=MOCK_MP_SERVICES)


@app.post("/api/1/envelope/")
def sentry_handler():
    headers, item_headers, payload = bottle.request.body.read().splitlines()
    MOCK_SENTRY_QUEUE.put(json.loads(payload))
    return {"id": "fc6d8c0c43fc4630ad850ee518f1b9d0"}


class CustomClient(Client):
    def send_bad_data(self):
        self.ws.send("bad-data")


def kill_process(process):
    # This kinda sucks, but its the only way to nuke the child procs
    if process is None:
        return
    proc = psutil.Process(pid=process.pid)
    child_procs = proc.children(recursive=True)
    for p in [proc] + child_procs:
        os.kill(p.pid, signal.SIGTERM)
    process.wait()


def get_rust_binary_path(binary):
    global STRICT_LOG_COUNTS

    rust_bin = root_dir + "/target/release/{}".format(binary)
    possible_paths = [
        "/target/debug/{}".format(binary),
        "/{0}/target/release/{0}".format(binary),
        "/{0}/target/debug/{0}".format(binary),
    ]
    while possible_paths and not os.path.exists(rust_bin):  # pragma: nocover
        rust_bin = root_dir + possible_paths.pop(0)

    if "release" not in rust_bin:
        # disable checks for chatty debug mode binaries
        STRICT_LOG_COUNTS = False

    return rust_bin


def write_config_to_env(config, prefix):
    for key, val in config.items():
        new_key = prefix + key
        log.debug("✍ config {} => {}".format(new_key, val))
        os.environ[new_key.upper()] = str(val)


def capture_output_to_queue(output_stream):
    log_queue = Queue()
    t = Thread(target=enqueue_output, args=(output_stream, log_queue))
    t.daemon = True  # thread dies with the program
    t.start()
    return log_queue


def setup_bt():
    global BT_PROCESS, BT_DB_SETTINGS
    BT_PROCESS = subprocess.Popen("gcloud beta emulators bigtable start".split(" "))
    os.environ["BIGTABLE_EMULATOR_HOST"] = "localhost:8086"
    try:
        BT_DB_SETTINGS = os.environ.get(
            "BT_DB_SETTINGS",
            json.dumps(
                {
                    "table_name": "projects/test/instances/test/tables/autopush",
                }
            ),
        )
        # Note: This will produce an emulator that runs on DB_DSN="grpc://localhost:8086"
        # using a Table Name of "projects/test/instances/test/tables/autopush"
        log.debug("🐍🟢 Starting bigtable emulator")
        cmd_start = "cbt -project test -instance test".split(" ")
        vv = subprocess.call(
            cmd_start + "createtable autopush".split(" "), stderr=subprocess.STDOUT
        )
        vv = subprocess.call(cmd_start + "createfamily autopush message".split(" "))
        vv = subprocess.call(cmd_start + "createfamily autopush message_topic".split(" "))
        vv = subprocess.call(cmd_start + "createfamily autopush router".split(" "))
        vv = subprocess.call(cmd_start + "setgcpolicy autopush message maxage=1s".split(" "))
        vv = subprocess.call(
            cmd_start + "setgcpolicy autopush message_topic maxversions=1".split(" ")
        )
        vv = subprocess.call(cmd_start + "setgcpolicy autopush router maxversions=1".split(" "))
        log.debug(vv)
    except Exception as e:
        log.error("Bigtable Setup Error {}", e)
        raise


def setup_dynamodb():
    global DDB_PROCESS

    log.debug("🐍🟢 Starting dynamodb")
    if os.getenv("AWS_LOCAL_DYNAMODB") is None:
        cmd = " ".join(
            [
                "java",
                "-Djava.library.path=%s" % DDB_LIB_DIR,
                "-jar",
                DDB_JAR,
                "-sharedDb",
                "-inMemory",
            ]
        )
        DDB_PROCESS = subprocess.Popen(cmd, shell=True, env=os.environ)
        os.environ["AWS_LOCAL_DYNAMODB"] = "http://127.0.0.1:8000"
    else:
        print("Using existing DynamoDB instance")

    # Setup the necessary tables
    boto_resource = DynamoDBResource()
    create_message_table_ddb(boto_resource, MESSAGE_TABLE)
    get_router_table(boto_resource, ROUTER_TABLE)


def setup_mock_server():
    global MOCK_SERVER_THREAD

    MOCK_SERVER_THREAD = Thread(target=app.run, kwargs=dict(port=MOCK_SERVER_PORT, debug=True))
    MOCK_SERVER_THREAD.daemon = True
    MOCK_SERVER_THREAD.start()

    # Sentry API mock
    os.environ["SENTRY_DSN"] = "http://foo:bar@localhost:{}/1".format(MOCK_SERVER_PORT)


def setup_connection_server(connection_binary):
    global CN_SERVER, BT_PROCESS, DDB_PROCESS

    # NOTE:
    # due to a change in Config, autopush uses a double
    # underscore as a separator (e.g. "AUTOEND__FCM__MIN_TTL" ==
    # `settings.fcm.min_ttl`)
    url = os.getenv("AUTOPUSH_CN_SERVER")
    if url is not None:
        parsed = urlparse(url)
        CONNECTION_CONFIG["hostname"] = parsed.hostname
        CONNECTION_CONFIG["port"] = parsed.port
        CONNECTION_CONFIG["endpoint_scheme"] = parsed.scheme
        write_config_to_env(CONNECTION_CONFIG, CONNECTION_SETTINGS_PREFIX)
        return
    else:
        write_config_to_env(CONNECTION_CONFIG, CONNECTION_SETTINGS_PREFIX)
    cmd = [connection_binary]
    log.debug(f"🐍🟢 Starting Connection server: {' '.join(cmd)}")
    CN_SERVER = subprocess.Popen(
        cmd,
        shell=True,
        env=os.environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )

    # Spin up the readers to dump the output from stdout/stderr
    out_q = capture_output_to_queue(CN_SERVER.stdout)
    err_q = capture_output_to_queue(CN_SERVER.stderr)
    CN_QUEUES.extend([out_q, err_q])


def setup_megaphone_server(connection_binary):
    global CN_MP_SERVER

    url = os.getenv("AUTOPUSH_MP_SERVER")
    if url is not None:
        parsed = urlparse(url)
        MEGAPHONE_CONFIG["hostname"] = parsed.hostname
        MEGAPHONE_CONFIG["port"] = parsed.port
        MEGAPHONE_CONFIG["endpoint_scheme"] = parsed.scheme
        url = os.getenv("AUTOPUSH_EP_SERVER")
        if url is not None:
            parsed = urlparse(url)
            MEGAPHONE_CONFIG["endpoint_port"] = parsed.port
        write_config_to_env(MEGAPHONE_CONFIG, CONNECTION_SETTINGS_PREFIX)
        return
    else:
        write_config_to_env(MEGAPHONE_CONFIG, CONNECTION_SETTINGS_PREFIX)
    cmd = [connection_binary]
    log.debug("🐍🟢 Starting Megaphone server: {}".format(" ".join(cmd)))
    CN_MP_SERVER = subprocess.Popen(cmd, shell=True, env=os.environ)


def setup_endpoint_server():
    global CONNECTION_CONFIG, EP_SERVER, BT_PROCESS

    # Set up environment
    # NOTE:
    # due to a change in Config, autoendpoint uses a double
    # underscore as a separator (e.g. "AUTOEND__FCM__MIN_TTL" ==
    # `settings.fcm.min_ttl`)
    url = os.getenv("AUTOPUSH_EP_SERVER")
    ENDPOINT_CONFIG["db_dsn"] = CONNECTION_CONFIG["db_dsn"]
    ENDPOINT_CONFIG["db_settings"] = CONNECTION_CONFIG["db_settings"]
    if url is not None:
        parsed = urlparse(url)
        ENDPOINT_CONFIG["hostname"] = parsed.hostname
        ENDPOINT_CONFIG["port"] = parsed.port
        ENDPOINT_CONFIG["endpoint_scheme"] = parsed.scheme
        return
    else:
        write_config_to_env(ENDPOINT_CONFIG, "autoend__")

    # Run autoendpoint
    cmd = [get_rust_binary_path("autoendpoint")]

    log.debug("🐍🟢 Starting Endpoint server: {}".format(" ".join(cmd)))
    EP_SERVER = subprocess.Popen(
        cmd,
        shell=True,
        env=os.environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )

    # Spin up the readers to dump the output from stdout/stderr
    out_q = capture_output_to_queue(EP_SERVER.stdout)
    err_q = capture_output_to_queue(EP_SERVER.stderr)
    EP_QUEUES.extend([out_q, err_q])


def setup_module():
    global CN_SERVER, CN_QUEUES, CN_MP_SERVER, MOCK_SERVER_THREAD, STRICT_LOG_COUNTS, RUST_LOG

    if "SKIP_INTEGRATION" in os.environ:  # pragma: nocover
        raise SkipTest("Skipping integration tests")

    for name in ("boto", "boto3", "botocore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)

    if CONNECTION_CONFIG.get("db_dsn").startswith("grpc"):
        log.debug("Setting up BigTable")
        setup_bt()
    elif CONNECTION_CONFIG.get("db_dsn").startswith("dual"):
        setup_bt()
        setup_dynamodb()
    else:
        setup_dynamodb()

    pool = reactor.getThreadPool()
    pool.adjustPoolsize(minthreads=pool.max)

    setup_mock_server()

    log.debug(f"🐍🟢 Rust Log: {RUST_LOG}")
    os.environ["RUST_LOG"] = RUST_LOG
    connection_binary = get_rust_binary_path(CONNECTION_BINARY)
    setup_connection_server(connection_binary)
    setup_megaphone_server(connection_binary)
    setup_endpoint_server()
    time.sleep(2)


def teardown_module():
    if DDB_PROCESS:
        os.unsetenv("AWS_LOCAL_DYNAMODB")
        log.debug("🐍🔴 Stopping dynamodb")
        kill_process(DDB_PROCESS)
    if BT_PROCESS:
        os.unsetenv("BIGTABLE_EMULATOR_HOST")
        log.debug("🐍🔴 Stopping bigtable")
        kill_process(BT_PROCESS)
    log.debug("🐍🔴 Stopping connection server")
    kill_process(CN_SERVER)
    log.debug("🐍🔴 Stopping megaphone server")
    kill_process(CN_MP_SERVER)
    log.debug("🐍🔴 Stopping endpoint server")
    kill_process(EP_SERVER)


class TestRustWebPush(unittest.TestCase):
    # Max log lines allowed to be emitted by each node type
    max_endpoint_logs = 8
    max_conn_logs = 3
    vapid_payload = {
        "exp": int(time.time()) + 86400,
        "sub": "mailto:admin@example.com",
    }

    def tearDown(self):
        process_logs(self)
        while not MOCK_SENTRY_QUEUE.empty():
            MOCK_SENTRY_QUEUE.get_nowait()

    def host_endpoint(self, client):
        parsed = urlparse(list(client.channels.values())[0])
        return "{}://{}".format(parsed.scheme, parsed.netloc)

    @inlineCallbacks
    def quick_register(self):
        log.debug("🐍#### Connecting to ws://localhost:{}/".format(CONNECTION_PORT))
        client = Client("ws://localhost:{}/".format(CONNECTION_PORT))
        yield client.connect()
        yield client.hello()
        yield client.register()
        log.debug("🐍 Connected")
        returnValue(client)

    @inlineCallbacks
    def shut_down(self, client=None):
        if client:
            yield client.disconnect()

    @property
    def _ws_url(self):
        return "ws://localhost:{}/".format(CONNECTION_PORT)

    @inlineCallbacks
    @max_logs(conn=4)
    def test_sentry_output_autoconnect(self):
        if os.getenv("SKIP_SENTRY"):
            SkipTest("Skipping sentry test")
            return
        # Ensure bad data doesn't throw errors
        client = CustomClient(self._ws_url)
        yield client.connect()
        yield client.hello()
        yield client.send_bad_data()
        yield self.shut_down(client)

        # LogCheck does throw an error every time
        requests.get("http://localhost:{}/v1/err/crit".format(CONNECTION_PORT))
        event1 = MOCK_SENTRY_QUEUE.get(timeout=5)
        # new autoconnect emits 2 events
        try:
            MOCK_SENTRY_QUEUE.get(timeout=1)
        except Empty:
            pass
        assert event1["exception"]["values"][0]["value"] == "LogCheck"

    @inlineCallbacks
    @max_logs(endpoint=1)
    def test_sentry_output_autoendpoint(self):
        if os.getenv("SKIP_SENTRY"):
            SkipTest("Skipping sentry test")
            return

        client = yield self.quick_register()
        endpoint = self.host_endpoint(client)
        yield self.shut_down(client)

        requests.get("{}/__error__".format(endpoint))
        # 2 events excpted: 1 from a panic and 1 from a returned Error
        event1 = MOCK_SENTRY_QUEUE.get(timeout=5)
        event2 = MOCK_SENTRY_QUEUE.get(timeout=1)
        values = (
            event1["exception"]["values"][0]["value"],
            event2["exception"]["values"][0]["value"],
        )
        assert sorted(values) == ["ERROR:Success", "LogCheck"]

    @max_logs(conn=4)
    def test_no_sentry_output(self):
        if os.getenv("SKIP_SENTRY"):
            SkipTest("Skipping sentry test")
            return
        ws_url = urlparse(self._ws_url)._replace(scheme="http").geturl()
        try:
            requests.get(ws_url)
        except requests.exceptions.ConnectionError:
            pass
        try:
            data = MOCK_SENTRY_QUEUE.get(timeout=1)
            assert not data
        except Empty:
            pass

    @inlineCallbacks
    def test_hello_echo(self):
        client = Client(self._ws_url)
        yield client.connect()
        result = yield client.hello()
        assert result != {}
        assert result["use_webpush"] is True
        yield self.shut_down(client)

    @inlineCallbacks
    def test_hello_with_bad_prior_uaid(self):
        non_uaid = uuid.uuid4().hex
        client = Client(self._ws_url)
        yield client.connect()
        result = yield client.hello(uaid=non_uaid)
        assert result != {}
        assert result["uaid"] != non_uaid
        assert result["use_webpush"] is True
        yield self.shut_down(client)

    @inlineCallbacks
    def test_basic_delivery(self):
        data = str(uuid.uuid4())
        client: Client = yield self.quick_register()
        result = yield client.send_notification(data=data)
        # the following presumes that only `salt` is padded.
        clean_header = client._crypto_key.replace('"', "").rstrip("=")
        assert result["headers"]["encryption"] == clean_header
        assert result["data"] == base64url_encode(bytes(data, "utf-8"))
        assert result["messageType"] == "notification"
        yield self.shut_down(client)

    @inlineCallbacks
    def test_topic_basic_delivery(self):
        data = str(uuid.uuid4())
        client = yield self.quick_register()
        result = yield client.send_notification(data=data, topic="Inbox")
        # the following presumes that only `salt` is padded.
        clean_header = client._crypto_key.replace('"', "").rstrip("=")
        assert result["headers"]["encryption"] == clean_header
        assert result["data"] == base64url_encode(data)
        assert result["messageType"] == "notification"
        yield self.shut_down(client)

    @inlineCallbacks
    def test_topic_replacement_delivery(self):
        data = str(uuid.uuid4())
        data2 = str(uuid.uuid4())
        client = yield self.quick_register()
        yield client.disconnect()
        yield client.send_notification(data=data, topic="Inbox", status=201)
        yield client.send_notification(data=data2, topic="Inbox", status=201)
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification()
        log.debug("get_notification result:", result)
        # the following presumes that only `salt` is padded.
        clean_header = client._crypto_key.replace('"', "").rstrip("=")
        assert result["headers"]["encryption"] == clean_header
        assert result["data"] == base64url_encode(data2)
        assert result["messageType"] == "notification"
        result = yield client.get_notification()
        assert result is None
        yield self.shut_down(client)

    @inlineCallbacks
    @max_logs(conn=4)
    def test_topic_no_delivery_on_reconnect(self):
        data = str(uuid.uuid4())
        client = yield self.quick_register()
        yield client.disconnect()
        yield client.send_notification(data=data, topic="Inbox", status=201)
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification(timeout=10)
        # the following presumes that only `salt` is padded.
        clean_header = client._crypto_key.replace('"', "").rstrip("=")
        assert result["headers"]["encryption"] == clean_header
        assert result["data"] == base64url_encode(data)
        assert result["messageType"] == "notification"
        yield client.ack(result["channelID"], result["version"])
        yield client.disconnect()
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification()
        assert result is None
        yield client.disconnect()
        yield client.connect()
        yield client.hello()
        yield self.shut_down(client)

    @inlineCallbacks
    def test_basic_delivery_with_vapid(self):
        data = str(uuid.uuid4())
        client = yield self.quick_register()
        vapid_info = _get_vapid(payload=self.vapid_payload)
        result = yield client.send_notification(data=data, vapid=vapid_info)
        # the following presumes that only `salt` is padded.
        clean_header = client._crypto_key.replace('"', "").rstrip("=")
        assert result["headers"]["encryption"] == clean_header
        assert result["data"] == base64url_encode(data)
        assert result["messageType"] == "notification"
        yield self.shut_down(client)

    @inlineCallbacks
    def test_basic_delivery_with_invalid_vapid(self):
        data = str(uuid.uuid4())
        client = yield self.quick_register()
        vapid_info = _get_vapid(payload=self.vapid_payload, endpoint=self.host_endpoint(client))
        vapid_info["crypto-key"] = "invalid"
        yield client.send_notification(data=data, vapid=vapid_info, status=401)
        yield self.shut_down(client)

    @inlineCallbacks
    def test_basic_delivery_with_invalid_vapid_exp(self):
        data = str(uuid.uuid4())
        client = yield self.quick_register()
        vapid_info = _get_vapid(
            payload={
                "aud": self.host_endpoint(client),
                "exp": "@",
                "sub": "mailto:admin@example.com",
            }
        )
        vapid_info["crypto-key"] = "invalid"
        yield client.send_notification(data=data, vapid=vapid_info, status=401)
        yield self.shut_down(client)

    @inlineCallbacks
    def test_basic_delivery_with_invalid_vapid_auth(self):
        data = str(uuid.uuid4())
        client = yield self.quick_register()
        vapid_info = _get_vapid(
            payload=self.vapid_payload,
            endpoint=self.host_endpoint(client),
        )
        vapid_info["auth"] = ""
        yield client.send_notification(data=data, vapid=vapid_info, status=401)
        yield self.shut_down(client)

    @inlineCallbacks
    def test_basic_delivery_with_invalid_signature(self):
        data = str(uuid.uuid4())
        client = yield self.quick_register()
        vapid_info = _get_vapid(
            payload={
                "aud": self.host_endpoint(client),
                "sub": "mailto:admin@example.com",
            }
        )
        vapid_info["auth"] = vapid_info["auth"][:-3] + "bad"
        yield client.send_notification(data=data, vapid=vapid_info, status=401)
        yield self.shut_down(client)

    @inlineCallbacks
    def test_basic_delivery_with_invalid_vapid_ckey(self):
        data = str(uuid.uuid4())
        client = yield self.quick_register()
        vapid_info = _get_vapid(payload=self.vapid_payload, endpoint=self.host_endpoint(client))
        vapid_info["crypto-key"] = "invalid|"
        yield client.send_notification(data=data, vapid=vapid_info, status=401)
        yield self.shut_down(client)

    @inlineCallbacks
    def test_delivery_repeat_without_ack(self):
        data = str(uuid.uuid4())
        client = yield self.quick_register()
        yield client.disconnect()
        assert client.channels
        yield client.send_notification(data=data, status=201)
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification()
        assert result is not None
        assert result["data"] == base64url_encode(data)

        yield client.disconnect()
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification()
        assert result != {}
        assert result["data"] == base64url_encode(data)
        yield self.shut_down(client)

    @inlineCallbacks
    def test_repeat_delivery_with_disconnect_without_ack(self):
        data = str(uuid.uuid4())
        client = yield self.quick_register()
        result = yield client.send_notification(data=data)
        assert result != {}
        assert result["data"] == base64url_encode(data)
        yield client.disconnect()
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification()
        assert result != {}
        assert result["data"] == base64url_encode(data)
        yield self.shut_down(client)

    @inlineCallbacks
    def test_multiple_delivery_repeat_without_ack(self):
        data = str(uuid.uuid4())
        data2 = str(uuid.uuid4())
        client = yield self.quick_register()
        yield client.disconnect()
        assert client.channels
        yield client.send_notification(data=data, status=201)
        yield client.send_notification(data=data2, status=201)
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification()
        assert result != {}
        assert result["data"] in map(base64url_encode, [data, data2])
        result = yield client.get_notification()
        assert result != {}
        assert result["data"] in map(base64url_encode, [data, data2])

        yield client.disconnect()
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification()
        assert result != {}
        assert result["data"] in map(base64url_encode, [data, data2])
        result = yield client.get_notification()
        assert result != {}
        assert result["data"] in map(base64url_encode, [data, data2])
        yield self.shut_down(client)

    @inlineCallbacks
    def test_topic_expired(self):
        data = str(uuid.uuid4())
        client = yield self.quick_register()
        yield client.disconnect()
        assert client.channels
        yield client.send_notification(data=data, ttl=1, topic="test", status=201)
        yield client.sleep(2)
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification(timeout=0.5)
        assert result is None
        result = yield client.send_notification(data=data, topic="test")
        assert result != {}
        assert result["data"] == base64url_encode(data)
        yield self.shut_down(client)

    @inlineCallbacks
    @max_logs(conn=4)
    def test_multiple_delivery_with_single_ack(self):
        data = b"\x16*\xec\xb4\xc7\xac\xb1\xa8\x1e" + str(uuid.uuid4()).encode()
        data2 = b":\xd8^\xac\xc7\xac\xb1\xa8\x1e" + str(uuid.uuid4()).encode()
        client = yield self.quick_register()
        yield client.disconnect()
        assert client.channels
        yield client.send_notification(data=data, status=201)
        yield client.send_notification(data=data2, status=201)
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification(timeout=0.5)
        assert result != {}
        assert result["data"] == base64url_encode(data)
        result2 = yield client.get_notification(timeout=0.5)
        assert result2 != {}
        assert result2["data"] == base64url_encode(data2)
        yield client.ack(result["channelID"], result["version"])

        yield client.disconnect()
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification(timeout=0.5)
        assert result != {}
        assert result["data"] == base64url_encode(data)
        assert result["messageType"] == "notification"
        result2 = yield client.get_notification()
        assert result2 != {}
        assert result2["data"] == base64url_encode(data2)
        yield client.ack(result["channelID"], result["version"])
        yield client.ack(result2["channelID"], result2["version"])

        # Verify no messages are delivered
        yield client.disconnect()
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification(timeout=0.5)
        assert result is None
        yield self.shut_down(client)

    @inlineCallbacks
    def test_multiple_delivery_with_multiple_ack(self):
        data = b"\x16*\xec\xb4\xc7\xac\xb1\xa8\x1e" + str(uuid.uuid4()).encode()  # "FirstMessage"
        data2 = b":\xd8^\xac\xc7\xac\xb1\xa8\x1e" + str(uuid.uuid4()).encode()  # "OtherMessage"
        client = yield self.quick_register()
        yield client.disconnect()
        assert client.channels
        yield client.send_notification(data=data, status=201)
        yield client.send_notification(data=data2, status=201)
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification(timeout=0.5)
        assert result != {}
        assert result["data"] in map(base64url_encode, [data, data2])
        log.debug("🟩🟩 Result:: {}".format(result["data"]))
        result2 = yield client.get_notification()
        assert result2 != {}
        assert result2["data"] in map(base64url_encode, [data, data2])
        yield client.ack(result2["channelID"], result2["version"])
        yield client.ack(result["channelID"], result["version"])

        yield client.disconnect()
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification(timeout=0.5)
        assert result is None
        yield self.shut_down(client)

    @inlineCallbacks
    def test_no_delivery_to_unregistered(self):
        data = str(uuid.uuid4())
        client: Client = yield self.quick_register()
        assert client.channels
        chan = list(client.channels.keys())[0]

        result = yield client.send_notification(data=data)
        assert result["channelID"] == chan
        assert result["data"] == base64url_encode(data)
        yield client.ack(result["channelID"], result["version"])

        yield client.unregister(chan)
        result = yield client.send_notification(data=data, status=410)

        # Verify cache-control
        assert client.notif_response.headers.get("Cache-Control") == "max-age=86400"

        assert result is None
        yield self.shut_down(client)

    @inlineCallbacks
    def test_ttl_0_connected(self):
        data = str(uuid.uuid4())
        client = yield self.quick_register()
        result = yield client.send_notification(data=data, ttl=0)
        assert result is not None
        # the following presumes that only `salt` is padded.
        clean_header = client._crypto_key.replace('"', "").rstrip("=")
        assert result["headers"]["encryption"] == clean_header
        assert result["data"] == base64url_encode(data)
        assert result["messageType"] == "notification"
        yield self.shut_down(client)

    @inlineCallbacks
    def test_ttl_0_not_connected(self):
        data = str(uuid.uuid4())
        client = yield self.quick_register()
        yield client.disconnect()
        yield client.send_notification(data=data, ttl=0, status=201)
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification(timeout=0.5)
        assert result is None
        yield self.shut_down(client)

    @inlineCallbacks
    def test_ttl_expired(self):
        data = str(uuid.uuid4())
        client = yield self.quick_register()
        yield client.disconnect()
        yield client.send_notification(data=data, ttl=1, status=201)
        time.sleep(1)
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification(timeout=0.5)
        assert result is None
        yield self.shut_down(client)

    @inlineCallbacks
    @max_logs(endpoint=28)
    def test_ttl_batch_expired_and_good_one(self):
        data = str(uuid.uuid4()).encode()
        data2 = base64.urlsafe_b64decode("0012") + str(uuid.uuid4()).encode()
        print(data2)
        client = yield self.quick_register()
        yield client.disconnect()
        for x in range(0, 12):
            prefix = base64.urlsafe_b64decode("{:04d}".format(x))
            yield client.send_notification(data=prefix + data, ttl=1, status=201)

        yield client.send_notification(data=data2, status=201)
        time.sleep(1)
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification(timeout=4)
        assert result is not None
        # the following presumes that only `salt` is padded.
        clean_header = client._crypto_key.replace('"', "").rstrip("=")
        assert result["headers"]["encryption"] == clean_header
        assert result["data"] == base64url_encode(data2)
        assert result["messageType"] == "notification"
        result = yield client.get_notification(timeout=0.5)
        assert result is None
        yield self.shut_down(client)

    @inlineCallbacks
    @max_logs(endpoint=28)
    def test_ttl_batch_partly_expired_and_good_one(self):
        data = str(uuid.uuid4())
        data1 = str(uuid.uuid4())
        data2 = str(uuid.uuid4())
        client = yield self.quick_register()
        yield client.disconnect()
        for x in range(0, 6):
            yield client.send_notification(data=data, status=201)

        for x in range(0, 6):
            yield client.send_notification(data=data1, ttl=1, status=201)

        yield client.send_notification(data=data2, status=201)
        time.sleep(1)
        yield client.connect()
        yield client.hello()

        # Pull out and ack the first
        for x in range(0, 6):
            result = yield client.get_notification(timeout=4)
            assert result is not None
            assert result["data"] == base64url_encode(data)
            yield client.ack(result["channelID"], result["version"])

        # Should have one more that is data2, this will only arrive if the
        # other six were acked as that hits the batch size
        result = yield client.get_notification(timeout=4)
        assert result is not None
        assert result["data"] == base64url_encode(data2)

        # No more
        result = yield client.get_notification()
        assert result is None
        yield self.shut_down(client)

    @inlineCallbacks
    def test_message_without_crypto_headers(self):
        data = str(uuid.uuid4())
        client = yield self.quick_register()
        result = yield client.send_notification(data=data, use_header=False, status=400)
        assert result is None
        yield self.shut_down(client)

    @inlineCallbacks
    def test_empty_message_without_crypto_headers(self):
        client = yield self.quick_register()
        result = yield client.send_notification(use_header=False)
        assert result is not None
        assert result["messageType"] == "notification"
        assert "headers" not in result
        assert "data" not in result
        yield client.ack(result["channelID"], result["version"])

        yield client.disconnect()
        yield client.send_notification(use_header=False, status=201)
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification()
        assert result is not None
        assert "headers" not in result
        assert "data" not in result
        yield client.ack(result["channelID"], result["version"])

        yield self.shut_down(client)

    @inlineCallbacks
    def test_empty_message_with_crypto_headers(self):
        client = yield self.quick_register()
        result = yield client.send_notification()
        assert result is not None
        assert result["messageType"] == "notification"
        assert "headers" not in result
        assert "data" not in result

        result2 = yield client.send_notification()
        # We shouldn't store headers for blank messages.
        assert result2 is not None
        assert result2["messageType"] == "notification"
        assert "headers" not in result2
        assert "data" not in result2

        yield client.ack(result["channelID"], result["version"])
        yield client.ack(result2["channelID"], result2["version"])

        yield client.disconnect()
        yield client.send_notification(status=201)
        yield client.connect()
        yield client.hello()
        result3 = yield client.get_notification()
        assert result3 is not None
        assert "headers" not in result3
        assert "data" not in result3
        yield client.ack(result3["channelID"], result3["version"])

        yield self.shut_down(client)

    @inlineCallbacks
    def test_big_message(self):
        """Test that we accept a large message.

        Using pywebpush I encoded a 4096 block
        of random data into a 4216b block. B64 encoding that produced a
        block that was 5624 bytes long. We'll skip the binary bit for a
        4216 block of "text" we then b64 encode to send.
        """
        import base64

        client = yield self.quick_register()
        bulk = "".join(
            random.choice(string.ascii_letters + string.digits + string.punctuation)
            for _ in range(0, 4216)
        )
        data = base64.urlsafe_b64encode(bytes(bulk, "utf-8"))
        result = yield client.send_notification(data=data)
        dd = result.get("data")
        dh = base64.b64decode(dd + "==="[: len(dd) % 4])
        assert dh == data

    # Need to dig into this test a bit more. I'm not sure it's structured
    # correctly since we resolved a bug about returning 202 v. 201, and
    # it's using a dependent library to do the Client calls. In short,
    # this test will fail in `send_notification()` because the response
    # will be a 202 instead of 201, and Client.send_notification will
    # fail to record the message into it's internal message array, which will
    # cause Client.delete_notification to fail.

    # Skipping test for now.
    """
    @inlineCallbacks
    def test_delete_saved_notification(self):
        client = yield self.quick_register()
        yield client.disconnect()
        assert client.channels
        chan = client.channels.keys()[0]
        yield client.send_notification()
        yield client.delete_notification(chan, status=204)
        yield client.connect()
        yield client.hello()
        result = yield client.get_notification()
        assert result is None
        yield self.shut_down(client)
    # """

    @inlineCallbacks
    def test_with_key(self):
        private_key = ecdsa.SigningKey.generate(curve=ecdsa.NIST256p)
        claims = {
            "aud": "http://localhost:{}".format(ENDPOINT_PORT),
            "exp": int(time.time()) + 86400,
            "sub": "a@example.com",
        }
        vapid = _get_vapid(private_key, claims)
        pk_hex = vapid["crypto-key"]
        chid = str(uuid.uuid4())
        client = Client("ws://localhost:{}/".format(CONNECTION_PORT))
        yield client.connect()
        yield client.hello()
        yield client.register(chid=chid, key=pk_hex)

        # Send an update with a properly formatted key.
        yield client.send_notification(vapid=vapid)

        # now try an invalid key.
        new_key = ecdsa.SigningKey.generate(curve=ecdsa.NIST256p)
        vapid = _get_vapid(new_key, claims)

        yield client.send_notification(vapid=vapid, status=401)

        yield self.shut_down(client)

    @inlineCallbacks
    def test_with_bad_key(self):
        chid = str(uuid.uuid4())
        client = Client("ws://localhost:{}/".format(CONNECTION_PORT))
        yield client.connect()
        yield client.hello()
        result = yield client.register(chid=chid, key="af1883%&!@#*(", status=400)
        assert result["status"] == 400

        yield self.shut_down(client)

    @inlineCallbacks
    @max_logs(endpoint=44)
    def test_msg_limit(self):
        self.skipTest("known broken")
        client = yield self.quick_register()
        uaid = client.uaid
        yield client.disconnect()
        for i in range(MSG_LIMIT + 1):
            yield client.send_notification(status=201)
        yield client.connect()
        yield client.hello()
        assert client.uaid == uaid
        for i in range(MSG_LIMIT):
            result = yield client.get_notification()
            assert result is not None, f"failed at {i}"
            yield client.ack(result["channelID"], result["version"])
        yield client.disconnect()
        yield client.connect()
        yield client.hello()
        assert client.uaid != uaid
        yield self.shut_down(client)

    @inlineCallbacks
    def test_can_ping(self):
        client = yield self.quick_register()
        yield client.ping()
        assert client.ws.connected
        try:
            yield client.ping()
        except AssertionError:
            # pinging too quickly should disconnect without a valid ping
            # repsonse
            pass
        assert not client.ws.connected
        yield self.shut_down(client)

    @inlineCallbacks
    def test_internal_endpoints(self):
        """Ensure an internal router endpoint isn't exposed on the public CONNECTION_PORT"""
        client = yield self.quick_register()
        parsed = (
            urlparse(self._ws_url)._replace(scheme="http")._replace(path=f"/notif/{client.uaid}")
        )

        # We can't determine an AUTOPUSH_CN_SERVER's ROUTER_PORT
        if not os.getenv("AUTOPUSH_CN_SERVER"):
            url = parsed._replace(netloc=f"{parsed.hostname}:{ROUTER_PORT}").geturl()
            # First ensure the endpoint we're testing for on the public port exists where
            # we expect it on the internal ROUTER_PORT
            requests.put(url).raise_for_status()

        try:
            requests.put(parsed.geturl()).raise_for_status()
        except requests.exceptions.ConnectionError:
            pass
        except requests.exceptions.HTTPError as e:
            assert e.response.status_code == 404
        else:
            assert False


class TestRustWebPushBroadcast(unittest.TestCase):
    max_endpoint_logs = 4
    max_conn_logs = 1

    def tearDown(self):
        process_logs(self)

    @inlineCallbacks
    def quick_register(self, connection_port=None):
        conn_port = connection_port or MP_CONNECTION_PORT
        client = Client("ws://localhost:{}/".format(conn_port))
        yield client.connect()
        yield client.hello()
        yield client.register()
        returnValue(client)

    @inlineCallbacks
    def shut_down(self, client=None):
        if client:
            yield client.disconnect()

    @property
    def _ws_url(self):
        return "ws://localhost:{}/".format(MP_CONNECTION_PORT)

    @inlineCallbacks
    def test_broadcast_update_on_connect(self):
        global MOCK_MP_SERVICES
        MOCK_MP_SERVICES = {"kinto:123": "ver1"}
        MOCK_MP_POLLED.clear()
        MOCK_MP_POLLED.wait(timeout=5)

        old_ver = {"kinto:123": "ver0"}
        client = Client(self._ws_url)
        yield client.connect()
        result = yield client.hello(services=old_ver)
        assert result != {}
        assert result["use_webpush"] is True
        assert result["broadcasts"]["kinto:123"] == "ver1"

        MOCK_MP_SERVICES = {"kinto:123": "ver2"}
        MOCK_MP_POLLED.clear()
        MOCK_MP_POLLED.wait(timeout=5)

        result = yield client.get_broadcast(2)
        assert result["broadcasts"]["kinto:123"] == "ver2"

        yield self.shut_down(client)

    @inlineCallbacks
    def test_broadcast_update_on_connect_with_errors(self):
        global MOCK_MP_SERVICES
        MOCK_MP_SERVICES = {"kinto:123": "ver1"}
        MOCK_MP_POLLED.clear()
        MOCK_MP_POLLED.wait(timeout=5)

        old_ver = {"kinto:123": "ver0", "kinto:456": "ver1"}
        client = Client(self._ws_url)
        yield client.connect()
        result = yield client.hello(services=old_ver)
        assert result != {}
        assert result["use_webpush"] is True
        assert result["broadcasts"]["kinto:123"] == "ver1"
        assert result["broadcasts"]["errors"]["kinto:456"] == "Broadcast not found"
        yield self.shut_down(client)

    @inlineCallbacks
    def test_broadcast_subscribe(self):
        global MOCK_MP_SERVICES
        MOCK_MP_SERVICES = {"kinto:123": "ver1"}
        MOCK_MP_POLLED.clear()
        MOCK_MP_POLLED.wait(timeout=5)

        old_ver = {"kinto:123": "ver0"}
        client = Client(self._ws_url)
        yield client.connect()
        result = yield client.hello()
        assert result != {}
        assert result["use_webpush"] is True
        assert result["broadcasts"] == {}

        client.broadcast_subscribe(old_ver)
        result = yield client.get_broadcast()
        assert result["broadcasts"]["kinto:123"] == "ver1"

        MOCK_MP_SERVICES = {"kinto:123": "ver2"}
        MOCK_MP_POLLED.clear()
        MOCK_MP_POLLED.wait(timeout=5)

        result = yield client.get_broadcast(2)
        assert result["broadcasts"]["kinto:123"] == "ver2"

        yield self.shut_down(client)

    @inlineCallbacks
    def test_broadcast_subscribe_with_errors(self):
        global MOCK_MP_SERVICES
        MOCK_MP_SERVICES = {"kinto:123": "ver1"}
        MOCK_MP_POLLED.clear()
        MOCK_MP_POLLED.wait(timeout=5)

        old_ver = {"kinto:123": "ver0", "kinto:456": "ver1"}
        client = Client(self._ws_url)
        yield client.connect()
        result = yield client.hello()
        assert result != {}
        assert result["use_webpush"] is True
        assert result["broadcasts"] == {}

        client.broadcast_subscribe(old_ver)
        result = yield client.get_broadcast()
        assert result["broadcasts"]["kinto:123"] == "ver1"
        assert result["broadcasts"]["errors"]["kinto:456"] == "Broadcast not found"

        yield self.shut_down(client)

    @inlineCallbacks
    def test_broadcast_no_changes(self):
        global MOCK_MP_SERVICES
        MOCK_MP_SERVICES = {"kinto:123": "ver1"}
        MOCK_MP_POLLED.clear()
        MOCK_MP_POLLED.wait(timeout=5)

        old_ver = {"kinto:123": "ver1"}
        client = Client(self._ws_url)
        yield client.connect()
        result = yield client.hello(services=old_ver)
        assert result != {}
        assert result["use_webpush"] is True
        assert result["broadcasts"] == {}

        yield self.shut_down(client)